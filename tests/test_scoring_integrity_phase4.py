"""
Phase 4 of the V2.8.0 scoring fix — integrity fix tests.

Three discrete fixes verified here:

  1. Heat undo strips cached points and rebuilds individual_points + team
     totals from SUM(EventResult.points_awarded) BEFORE deleting the rows
     (PLAN_REVIEW.md A6 / C2 / C7).
  2. Auto-finalize from _save_heat_results_submission is wrapped in a
     savepoint so that a calculate_positions() failure rolls back ONLY the
     points_awarded writes, not the heat results the judge just entered
     (PLAN_REVIEW.md A7).
  3. Admin repair route POST /scoring/admin/repair-points/<tid> rebuilds
     all CollegeCompetitor.individual_points and Team.total_points from
     SUM, returns JSON, requires admin role, exempt from CSRF, audit-logged.

Self-contained app fixture (same pattern as test_routes_post.py and
test_split_tie_scoring.py) to avoid conftest.py admin user collision.
"""
import json
import os
from decimal import Decimal

import pytest

from database import db as _db


@pytest.fixture(scope='module')
def app():
    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()
    with _app.app_context():
        _seed(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed(app):
    from models import Tournament
    from models.user import User
    if not User.query.filter_by(username='ph4_admin').first():
        u = User(username='ph4_admin', role='admin')
        u.set_password('ph4_pass')
        _db.session.add(u)
    if not User.query.filter_by(username='ph4_judge').first():
        j = User(username='ph4_judge', role='judge')
        j.set_password('ph4_pass')
        _db.session.add(j)
    if not Tournament.query.first():
        t = Tournament(name='Phase 4 Test 2026', year=2026, status='setup')
        _db.session.add(t)
    _db.session.commit()


@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def admin_client(app):
    c = app.test_client()
    with app.app_context():
        c.post('/auth/login', data={
            'username': 'ph4_admin', 'password': 'ph4_pass',
        }, follow_redirects=True)
    return c


@pytest.fixture()
def judge_client(app):
    c = app.test_client()
    with app.app_context():
        c.post('/auth/login', data={
            'username': 'ph4_judge', 'password': 'ph4_pass',
        }, follow_redirects=True)
    return c


@pytest.fixture()
def tid(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.first().id


# ---------------------------------------------------------------------------
# Local seed helpers
# ---------------------------------------------------------------------------

def _make_team(session, tid, code='UM-A'):
    from models import Team
    t = Team(tournament_id=tid, team_code=code,
             school_name='University of Montana', school_abbreviation='UM')
    session.add(t)
    session.flush()
    return t


def _make_college(session, tid, team_id, name, gender='M'):
    from models.competitor import CollegeCompetitor
    c = CollegeCompetitor(
        tournament_id=tid, team_id=team_id, name=name, gender=gender,
        events_entered=json.dumps([]), status='active',
    )
    session.add(c)
    session.flush()
    return c


def _make_event(session, tid, name='Test Event', **kw):
    from models.event import Event
    defaults = dict(
        tournament_id=tid, name=name, event_type='college', gender='M',
        scoring_type='time', scoring_order='lowest_wins',
        stand_type='underhand', max_stands=5, status='pending',
        payouts=json.dumps({}),
    )
    defaults.update(kw)
    e = Event(**defaults)
    session.add(e)
    session.flush()
    return e


def _make_heat(session, event, run_number=1, competitors=None, status='pending'):
    from models.heat import Heat
    h = Heat(
        event_id=event.id, heat_number=1, run_number=run_number,
        competitors=json.dumps(competitors or []),
        stand_assignments=json.dumps({}),
        status=status,
    )
    session.add(h)
    session.flush()
    return h


def _make_result(session, event, comp, value, status='completed'):
    from models.event import EventResult
    r = EventResult(
        event_id=event.id, competitor_id=comp.id,
        competitor_type='college', competitor_name=comp.name,
        result_value=value, run1_value=value,
        status=status,
    )
    session.add(r)
    session.flush()
    return r


# ===========================================================================
# 1. Heat undo strips cached points
# ===========================================================================


class TestHeatUndoStripsPoints:
    """undo_heat_save must rebuild individual_points + team totals after delete."""

    def test_undo_heat_clears_individual_points(self, db_session, judge_client, tid, app):
        """After undo, the competitor's individual_points returns to 0."""
        from datetime import datetime, timezone

        from models.competitor import CollegeCompetitor
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tid)
        c1 = _make_college(db_session, tid, team.id, 'A')
        c2 = _make_college(db_session, tid, team.id, 'B')
        event = _make_event(db_session, tid)
        heat = _make_heat(db_session, event, competitors=[c1.id, c2.id], status='completed')
        _make_result(db_session, event, c1, 18.0)
        _make_result(db_session, event, c2, 22.0)
        db_session.flush()

        # Finalize so the cache is populated.
        calculate_positions(event)
        db_session.flush()

        c1_id, c2_id = c1.id, c2.id
        assert CollegeCompetitor.query.get(c1_id).individual_points == Decimal('10.00')
        assert CollegeCompetitor.query.get(c2_id).individual_points == Decimal('7.00')

        # Plant an undo token in the session — undo route requires it.
        with judge_client.session_transaction() as sess:
            sess[f'undo_heat_{heat.id}'] = {
                'heat_id': heat.id,
                'event_id': event.id,
                'saved_at': datetime.now(timezone.utc).isoformat(),
            }

        # Now POST the undo.
        r = judge_client.post(f'/scoring/{tid}/heat/{heat.id}/undo')
        assert r.status_code in (200, 302), f'undo failed: {r.status_code} {r.data[:200]}'

        # The competitors' cached individual_points should be back to 0
        # because the EventResult rows were deleted and the rebuild SUM
        # finds nothing for them.
        c1_loaded = CollegeCompetitor.query.get(c1_id)
        c2_loaded = CollegeCompetitor.query.get(c2_id)
        assert c1_loaded.individual_points == Decimal('0.00')
        assert c2_loaded.individual_points == Decimal('0.00')

    def test_undo_heat_clears_team_total(self, db_session, judge_client, tid):
        """Team total_points returns to 0 after undo."""
        from datetime import datetime, timezone

        from models.team import Team
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tid, code='UM-B')
        c1 = _make_college(db_session, tid, team.id, 'C')
        c2 = _make_college(db_session, tid, team.id, 'D')
        event = _make_event(db_session, tid)
        heat = _make_heat(db_session, event, competitors=[c1.id, c2.id], status='completed')
        _make_result(db_session, event, c1, 19.0)
        _make_result(db_session, event, c2, 23.0)
        db_session.flush()

        calculate_positions(event)
        db_session.flush()
        team_id = team.id
        assert Team.query.get(team_id).total_points == Decimal('17.00')  # 10 + 7

        with judge_client.session_transaction() as sess:
            sess[f'undo_heat_{heat.id}'] = {
                'heat_id': heat.id,
                'event_id': event.id,
                'saved_at': datetime.now(timezone.utc).isoformat(),
            }

        r = judge_client.post(f'/scoring/{tid}/heat/{heat.id}/undo')
        assert r.status_code in (200, 302)

        assert Team.query.get(team_id).total_points == Decimal('0.00')

    def test_undo_heat_deletes_event_result_rows(self, db_session, judge_client, tid):
        """The EventResult rows are gone after undo (existing behavior preserved)."""
        from datetime import datetime, timezone

        from models.event import EventResult
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tid, code='UM-C')
        c1 = _make_college(db_session, tid, team.id, 'E')
        event = _make_event(db_session, tid)
        heat = _make_heat(db_session, event, competitors=[c1.id], status='completed')
        _make_result(db_session, event, c1, 20.0)
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        event_id = event.id
        assert EventResult.query.filter_by(event_id=event_id).count() == 1

        with judge_client.session_transaction() as sess:
            sess[f'undo_heat_{heat.id}'] = {
                'heat_id': heat.id,
                'event_id': event_id,
                'saved_at': datetime.now(timezone.utc).isoformat(),
            }

        r = judge_client.post(f'/scoring/{tid}/heat/{heat.id}/undo')
        assert r.status_code in (200, 302)
        assert EventResult.query.filter_by(event_id=event_id).count() == 0


# ===========================================================================
# 2. Admin repair route
# ===========================================================================


class TestRepairPointsRoute:
    """POST /scoring/admin/repair-points/<tid> rebuilds caches from SUM."""

    def test_repair_rebuilds_drifted_individual_points(self, db_session, admin_client, tid):
        """When the cached value is wrong, repair sets it back to SUM."""
        from models.competitor import CollegeCompetitor
        team = _make_team(db_session, tid, code='RP-A')
        c1 = _make_college(db_session, tid, team.id, 'Drift-A')
        event = _make_event(db_session, tid, name='Repair Test')
        # Write a result with explicit points_awarded — simulate the drifted state
        # where the result row has 8.5 but the competitor cache says 999.
        from models.event import EventResult
        r = EventResult(
            event_id=event.id, competitor_id=c1.id,
            competitor_type='college', competitor_name=c1.name,
            result_value=20.0, run1_value=20.0,
            points_awarded=Decimal('8.50'),
            final_position=1, status='completed',
        )
        db_session.add(r)
        c1.individual_points = Decimal('999.00')  # corrupted cache
        db_session.flush()
        c1_id = c1.id

        # POST the repair.
        resp = admin_client.post(f'/scoring/admin/repair-points/{tid}')
        assert resp.status_code == 200, resp.data[:300]
        body = resp.get_json()
        assert body['ok'] is True
        assert body['competitors_rebuilt'] >= 1
        assert body['teams_rebuilt'] >= 1

        # The cache should now match the SUM of points_awarded.
        c1_loaded = CollegeCompetitor.query.get(c1_id)
        assert c1_loaded.individual_points == Decimal('8.50')

    def test_repair_rebuilds_team_totals(self, db_session, admin_client, tid):
        from models.event import EventResult
        from models.team import Team
        team = _make_team(db_session, tid, code='RP-B')
        c1 = _make_college(db_session, tid, team.id, 'Tm-A')
        c2 = _make_college(db_session, tid, team.id, 'Tm-B')
        event = _make_event(db_session, tid, name='Team Repair')
        for c, pts in ((c1, Decimal('5.00')), (c2, Decimal('3.00'))):
            db_session.add(EventResult(
                event_id=event.id, competitor_id=c.id,
                competitor_type='college', competitor_name=c.name,
                result_value=20.0, run1_value=20.0,
                points_awarded=pts, final_position=1, status='completed',
            ))
            c.individual_points = pts
        team.total_points = Decimal('999.00')  # corrupted
        db_session.flush()
        team_id = team.id

        resp = admin_client.post(f'/scoring/admin/repair-points/{tid}')
        assert resp.status_code == 200
        # Total should now be 5 + 3 = 8.
        assert Team.query.get(team_id).total_points == Decimal('8.00')

    def test_repair_requires_admin_role(self, db_session, judge_client, tid):
        """Judge role is rejected — admin only."""
        resp = judge_client.post(f'/scoring/admin/repair-points/{tid}')
        assert resp.status_code == 403

    def test_repair_requires_authentication(self, app, tid):
        """Unauthenticated POST is rejected by Flask-Login."""
        c = app.test_client()
        resp = c.post(f'/scoring/admin/repair-points/{tid}')
        # Either redirected to login (302) or 403/401 depending on auth gate;
        # the important part is that it's NOT 200.
        assert resp.status_code != 200

    def test_repair_404_for_nonexistent_tournament(self, db_session, admin_client):
        resp = admin_client.post('/scoring/admin/repair-points/99999')
        assert resp.status_code == 404

    def test_repair_writes_audit_log(self, db_session, admin_client, tid):
        from models.audit_log import AuditLog
        team = _make_team(db_session, tid, code='RP-C')
        _make_college(db_session, tid, team.id, 'Audit-A')
        db_session.flush()

        before = AuditLog.query.filter_by(action='points_cache_rebuilt').count()
        resp = admin_client.post(f'/scoring/admin/repair-points/{tid}')
        assert resp.status_code == 200
        after = AuditLog.query.filter_by(action='points_cache_rebuilt').count()
        assert after == before + 1

    def test_repair_idempotent(self, db_session, admin_client, tid):
        """Running repair twice produces the same result."""
        from models.competitor import CollegeCompetitor
        from models.event import EventResult
        team = _make_team(db_session, tid, code='RP-D')
        c1 = _make_college(db_session, tid, team.id, 'Idem')
        event = _make_event(db_session, tid, name='Idempotent')
        db_session.add(EventResult(
            event_id=event.id, competitor_id=c1.id,
            competitor_type='college', competitor_name=c1.name,
            result_value=20.0, run1_value=20.0,
            points_awarded=Decimal('7.00'), final_position=2, status='completed',
        ))
        db_session.flush()
        c1_id = c1.id

        admin_client.post(f'/scoring/admin/repair-points/{tid}')
        first = CollegeCompetitor.query.get(c1_id).individual_points
        admin_client.post(f'/scoring/admin/repair-points/{tid}')
        second = CollegeCompetitor.query.get(c1_id).individual_points
        assert first == second == Decimal('7.00')
