"""
Phase 2 of the V2.8.0 scoring fix — dual-judge timer entry tests.

Verifies that the heat-result POST handler correctly parses two judge stopwatch
readings (T1, T2) per physical run, averages them into the existing
``run1_value`` / ``run2_value`` / ``result_value`` fields, and stores the raw
readings on the new ``t1_run1`` / ``t2_run1`` / ``t1_run2`` / ``t2_run2``
columns from Phase 1.

Coverage matrix:

  - Single-run pro timed event (Pro Underhand) — primary path
  - Single-run college timed event (Single Buck) — verifies pro/college parity
  - Dual-run college timed event (Speed Climb) run 1
  - Dual-run college timed event (Speed Climb) run 2 (verifies best-of-two)
  - Distance event (Caber Toss) — highest_wins, dual-run
  - Partial entry (only T1 filled) — must mark row 'partial' and skip in finalize
  - Hard-Hit regression — primary score still uses single ``result_<cid>`` input
  - Triple-run regression — axe throw still uses single ``result_<cid>`` per throw
  - GET handler exposes new ``existing_t1_run1`` etc fields to template

Self-contained module-scoped app fixture (same pattern as test_routes_post.py)
to avoid conftest's per-test admin user creation cascade.
"""
import json
import os

import pytest

from database import db as _db

# ---------------------------------------------------------------------------
# Self-contained app fixture (module-scoped)
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Test Flask app with temp-file SQLite built via flask db upgrade."""
    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()

    with _app.app_context():
        _seed_admin_and_tournament(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_admin_and_tournament(app):
    """Seed an admin user and an empty tournament for the test module."""
    from models import Tournament
    from models.user import User

    if not User.query.filter_by(username='dt_admin').first():
        u = User(username='dt_admin', role='admin')
        u.set_password('dt_pass')
        _db.session.add(u)

    if not Tournament.query.first():
        t = Tournament(name='Phase 2 Test 2026', year=2026, status='setup')
        _db.session.add(t)

    _db.session.commit()


@pytest.fixture(autouse=True)
def db_session(app):
    """Wrap each test in a nested transaction and roll back afterward."""
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def auth_client(app):
    c = app.test_client()
    with app.app_context():
        c.post('/auth/login', data={
            'username': 'dt_admin',
            'password': 'dt_pass',
        }, follow_redirects=True)
    return c


@pytest.fixture()
def tid(app):
    """Return the seeded tournament id."""
    with app.app_context():
        from models import Tournament
        return Tournament.query.first().id


# ---------------------------------------------------------------------------
# Local seed helpers (per-test, run inside the rollback scope)
# ---------------------------------------------------------------------------

def _make_event(session, tid, name, **kw):
    from models.event import Event
    defaults = dict(
        tournament_id=tid, name=name, event_type='pro', gender='M',
        scoring_type='time', scoring_order='lowest_wins',
        stand_type='underhand', max_stands=5, status='pending',
        payouts=json.dumps({}),
    )
    defaults.update(kw)
    e = Event(**defaults)
    session.add(e)
    session.flush()
    return e


def _make_pro(session, tid, name, gender='M'):
    from models.competitor import ProCompetitor
    c = ProCompetitor(
        tournament_id=tid, name=name, gender=gender,
        events_entered=json.dumps([]), status='active',
    )
    session.add(c)
    session.flush()
    return c


def _make_college(session, tid, team_id, name, gender='M'):
    from models.competitor import CollegeCompetitor
    c = CollegeCompetitor(
        tournament_id=tid, team_id=team_id, name=name, gender=gender,
        events_entered=json.dumps([]), status='active',
    )
    session.add(c)
    session.flush()
    return c


def _make_team(session, tid, code='UM-A'):
    from models import Team
    t = Team(tournament_id=tid, team_code=code,
             school_name='University of Montana', school_abbreviation='UM')
    session.add(t)
    session.flush()
    return t


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


def _save_url(tournament_id, heat_id):
    return f'/scoring/{tournament_id}/heat/{heat_id}/enter'


def _ok(resp):
    assert resp.status_code not in (500, 502, 503), \
        f'Server error {resp.status_code}: {resp.data[:300]}'


def _post_dual_timer(client, tid, heat, comp_id, t1, t2, run='run1', status='completed'):
    data = {
        'heat_version': str(heat.version_id),
        f'status_{comp_id}': status,
    }
    if t1 is not None:
        data[f't1_{run}_{comp_id}'] = str(t1)
    if t2 is not None:
        data[f't2_{run}_{comp_id}'] = str(t2)
    return client.post(_save_url(tid, heat.id), data=data, follow_redirects=False)


# ---------------------------------------------------------------------------
# Single-run pro timed event (Pro Underhand)
# ---------------------------------------------------------------------------


class TestSingleRunProEvent:
    """Pro Underhand: 1 run, 2 timers, average → result_value."""

    def test_pro_underhand_averages_two_timers(self, db_session, auth_client, tid):
        from models.event import EventResult
        comp = _make_pro(db_session, tid, name='Mike Sullivan')
        event = _make_event(db_session, tid, name='Underhand')
        heat = _make_heat(db_session, event, competitors=[comp.id])
        db_session.flush()
        comp_id = comp.id
        event_id = event.id

        r = _post_dual_timer(auth_client, tid, heat, comp_id, '14.32', '14.36')
        _ok(r)

        result = EventResult.query.filter_by(
            event_id=event_id, competitor_id=comp_id
        ).first()
        assert result is not None
        assert float(result.t1_run1) == pytest.approx(14.32)
        assert float(result.t2_run1) == pytest.approx(14.36)
        assert float(result.result_value) == pytest.approx(14.34)
        assert float(result.run1_value) == pytest.approx(14.34)
        assert result.t1_run2 is None
        assert result.t2_run2 is None
        assert result.status == 'completed'

    def test_two_competitors_independent_averages(self, db_session, auth_client, tid):
        from models.event import EventResult
        c1 = _make_pro(db_session, tid, name='A Smith')
        c2 = _make_pro(db_session, tid, name='B Jones')
        event = _make_event(db_session, tid, name='Underhand')
        heat = _make_heat(db_session, event, competitors=[c1.id, c2.id])
        db_session.flush()
        c1_id, c2_id = c1.id, c2.id
        event_id = event.id

        r = auth_client.post(_save_url(tid, heat.id), data={
            'heat_version': str(heat.version_id),
            f'status_{c1_id}': 'completed',
            f't1_run1_{c1_id}': '12.10',
            f't2_run1_{c1_id}': '12.20',
            f'status_{c2_id}': 'completed',
            f't1_run1_{c2_id}': '13.50',
            f't2_run1_{c2_id}': '13.40',
        })
        _ok(r)

        r1 = EventResult.query.filter_by(event_id=event_id, competitor_id=c1_id).first()
        r2 = EventResult.query.filter_by(event_id=event_id, competitor_id=c2_id).first()
        assert float(r1.result_value) == pytest.approx(12.15)
        assert float(r2.result_value) == pytest.approx(13.45)


# ---------------------------------------------------------------------------
# Single-run college timed event
# ---------------------------------------------------------------------------


class TestSingleRunCollegeEvent:
    """College Single Buck: same dual-timer rule applies — pro/college parity."""

    def test_college_single_buck_averages_two_timers(self, db_session, auth_client, tid):
        from models.event import EventResult
        team = _make_team(db_session, tid)
        comp = _make_college(db_session, tid, team.id, name='Jane Doe', gender='F')
        event = _make_event(db_session, tid, name='Single Buck',
                            event_type='college', gender='F', stand_type='saw_hand')
        heat = _make_heat(db_session, event, competitors=[comp.id])
        db_session.flush()
        comp_id = comp.id
        event_id = event.id

        r = _post_dual_timer(auth_client, tid, heat, comp_id, '8.55', '8.59')
        _ok(r)

        result = EventResult.query.filter_by(
            event_id=event_id, competitor_id=comp_id
        ).first()
        assert float(result.t1_run1) == pytest.approx(8.55)
        assert float(result.t2_run1) == pytest.approx(8.59)
        assert float(result.result_value) == pytest.approx(8.57)
        assert float(result.run1_value) == pytest.approx(8.57)


# ---------------------------------------------------------------------------
# Dual-run timed event (Speed Climb)
# ---------------------------------------------------------------------------


class TestDualRunSpeedClimb:
    """Speed Climb: each run has its own pair of timers; best avg wins."""

    def test_speed_climb_run_1_stores_run1_columns_only(self, db_session, auth_client, tid):
        from models.event import EventResult
        comp = _make_pro(db_session, tid, name='Climber A')
        event = _make_event(db_session, tid, name='Speed Climb',
                            stand_type='speed_climb', requires_dual_runs=True)
        heat1 = _make_heat(db_session, event, run_number=1, competitors=[comp.id])
        db_session.flush()
        comp_id = comp.id
        event_id = event.id

        r = _post_dual_timer(auth_client, tid, heat1, comp_id, '22.10', '22.14', run='run1')
        _ok(r)

        result = EventResult.query.filter_by(
            event_id=event_id, competitor_id=comp_id
        ).first()
        assert float(result.t1_run1) == pytest.approx(22.10)
        assert float(result.t2_run1) == pytest.approx(22.14)
        assert float(result.run1_value) == pytest.approx(22.12)
        # Run 2 columns must stay NULL until run 2 is scored.
        assert result.t1_run2 is None
        assert result.t2_run2 is None
        assert result.run2_value is None
        assert float(result.best_run) == pytest.approx(22.12)
        assert float(result.result_value) == pytest.approx(22.12)

    def test_speed_climb_run_2_stores_run2_columns_and_picks_best(self, db_session, auth_client, tid):
        """After both runs, best_run = min(run1_value, run2_value) for lowest_wins."""
        from models.event import EventResult
        comp = _make_pro(db_session, tid, name='Climber B')
        event = _make_event(db_session, tid, name='Speed Climb',
                            stand_type='speed_climb', requires_dual_runs=True)
        heat1 = _make_heat(db_session, event, run_number=1, competitors=[comp.id])
        heat2 = _make_heat(db_session, event, run_number=2, competitors=[comp.id])
        db_session.flush()
        comp_id = comp.id
        event_id = event.id

        r1 = _post_dual_timer(auth_client, tid, heat1, comp_id, '22.10', '22.14', run='run1')
        _ok(r1)
        r2 = _post_dual_timer(auth_client, tid, heat2, comp_id, '22.05', '22.09', run='run2')
        _ok(r2)

        result = EventResult.query.filter_by(
            event_id=event_id, competitor_id=comp_id
        ).first()
        assert float(result.t1_run1) == pytest.approx(22.10)
        assert float(result.t2_run1) == pytest.approx(22.14)
        assert float(result.t1_run2) == pytest.approx(22.05)
        assert float(result.t2_run2) == pytest.approx(22.09)
        assert float(result.run1_value) == pytest.approx(22.12)
        assert float(result.run2_value) == pytest.approx(22.07)
        assert float(result.best_run) == pytest.approx(22.07)
        assert float(result.result_value) == pytest.approx(22.07)


class TestDualRunCaberToss:
    """Caber Toss: dual-run, distance, highest_wins — best_run = max(run1, run2)."""

    def test_caber_toss_picks_max_for_highest_wins(self, db_session, auth_client, tid):
        from models.event import EventResult
        comp = _make_pro(db_session, tid, name='Caber Tosser')
        event = _make_event(db_session, tid, name='Caber Toss',
                            scoring_type='distance', scoring_order='highest_wins',
                            stand_type='caber', requires_dual_runs=True)
        heat1 = _make_heat(db_session, event, run_number=1, competitors=[comp.id])
        heat2 = _make_heat(db_session, event, run_number=2, competitors=[comp.id])
        db_session.flush()
        comp_id = comp.id
        event_id = event.id

        _post_dual_timer(auth_client, tid, heat1, comp_id, '18.50', '18.52', run='run1')
        _post_dual_timer(auth_client, tid, heat2, comp_id, '19.00', '19.02', run='run2')

        result = EventResult.query.filter_by(
            event_id=event_id, competitor_id=comp_id
        ).first()
        assert float(result.run1_value) == pytest.approx(18.51)
        assert float(result.run2_value) == pytest.approx(19.01)
        assert float(result.best_run) == pytest.approx(19.01)  # HIGHER for highest_wins
        assert float(result.result_value) == pytest.approx(19.01)


# ---------------------------------------------------------------------------
# Partial entry — only one timer
# ---------------------------------------------------------------------------


class TestPartialEntry:
    def test_only_t1_filled_marks_row_partial(self, db_session, auth_client, tid):
        from models.event import EventResult
        comp = _make_pro(db_session, tid, name='Partial Entry')
        event = _make_event(db_session, tid, name='Underhand')
        heat = _make_heat(db_session, event, competitors=[comp.id])
        db_session.flush()
        comp_id = comp.id
        event_id = event.id

        r = _post_dual_timer(auth_client, tid, heat, comp_id, '12.50', None, run='run1')
        _ok(r)

        result = EventResult.query.filter_by(
            event_id=event_id, competitor_id=comp_id
        ).first()
        assert result is not None
        assert float(result.t1_run1) == pytest.approx(12.50)
        assert result.t2_run1 is None
        assert result.run1_value is None
        assert result.result_value is None
        assert result.status == 'partial'

    def test_only_t2_filled_marks_row_partial(self, db_session, auth_client, tid):
        from models.event import EventResult
        comp = _make_pro(db_session, tid, name='Other Partial')
        event = _make_event(db_session, tid, name='Underhand')
        heat = _make_heat(db_session, event, competitors=[comp.id])
        db_session.flush()
        comp_id = comp.id
        event_id = event.id

        r = _post_dual_timer(auth_client, tid, heat, comp_id, None, '14.00', run='run1')
        _ok(r)

        result = EventResult.query.filter_by(
            event_id=event_id, competitor_id=comp_id
        ).first()
        assert result.t1_run1 is None
        assert float(result.t2_run1) == pytest.approx(14.00)
        assert result.run1_value is None
        assert result.status == 'partial'

    def test_neither_timer_filled_skips_competitor(self, db_session, auth_client, tid):
        """If both timers are absent, the row is not created at all."""
        from models.event import EventResult
        comp = _make_pro(db_session, tid, name='Skipped Entry')
        c2 = _make_pro(db_session, tid, name='Other')
        event = _make_event(db_session, tid, name='Underhand')
        heat = _make_heat(db_session, event, competitors=[comp.id, c2.id])
        db_session.flush()
        comp_id = comp.id
        c2_id = c2.id
        event_id = event.id

        r = auth_client.post(_save_url(tid, heat.id), data={
            'heat_version': str(heat.version_id),
            f'status_{comp_id}': 'completed',
            # No t1/t2 for comp.
            f'status_{c2_id}': 'completed',
            f't1_run1_{c2_id}': '15.00',
            f't2_run1_{c2_id}': '15.10',
        })
        _ok(r)

        skipped = EventResult.query.filter_by(
            event_id=event_id, competitor_id=comp_id
        ).first()
        assert skipped is None

        scored = EventResult.query.filter_by(
            event_id=event_id, competitor_id=c2_id
        ).first()
        assert scored is not None
        assert float(scored.result_value) == pytest.approx(15.05)


# ---------------------------------------------------------------------------
# Hard-Hit regression — primary score still single-input
# ---------------------------------------------------------------------------


class TestHardHitRegression:
    def test_hard_hit_uses_single_result_field(self, db_session, auth_client, tid):
        from models.event import EventResult
        team = _make_team(db_session, tid, code='HH-A')
        comp = _make_college(db_session, tid, team.id, name='Hitter')
        event = _make_event(db_session, tid, name='Underhand Hard Hit',
                            event_type='college', scoring_type='hits',
                            scoring_order='lowest_wins', stand_type='underhand')
        heat = _make_heat(db_session, event, competitors=[comp.id])
        db_session.flush()
        comp_id = comp.id
        event_id = event.id

        r = auth_client.post(_save_url(tid, heat.id), data={
            'heat_version': str(heat.version_id),
            f'status_{comp_id}': 'completed',
            f'result_{comp_id}': '12',
            f'tiebreak_{comp_id}': '14.5',
        })
        _ok(r)

        result = EventResult.query.filter_by(
            event_id=event_id, competitor_id=comp_id
        ).first()
        assert float(result.result_value) == pytest.approx(12.0)
        assert float(result.tiebreak_value) == pytest.approx(14.5)
        assert result.t1_run1 is None
        assert result.t2_run1 is None


# ---------------------------------------------------------------------------
# Triple-run regression — axe throw still single-input per throw
# ---------------------------------------------------------------------------


class TestTripleRunRegression:
    def test_axe_throw_three_throws_summed(self, db_session, auth_client, tid):
        from models.event import EventResult
        comp = _make_pro(db_session, tid, name='Thrower')
        event = _make_event(db_session, tid, name='Partnered Axe Throw',
                            scoring_type='score', scoring_order='highest_wins',
                            stand_type='axe_throw', requires_triple_runs=True)
        heat = _make_heat(db_session, event, competitors=[comp.id])
        db_session.flush()
        comp_id = comp.id
        event_id = event.id

        r = auth_client.post(_save_url(tid, heat.id), data={
            'heat_version': str(heat.version_id),
            f'status_{comp_id}': 'completed',
            f'result_{comp_id}': '5',
            f'result2_{comp_id}': '4',
            f'result3_{comp_id}': '5',
        })
        _ok(r)

        result = EventResult.query.filter_by(
            event_id=event_id, competitor_id=comp_id
        ).first()
        assert float(result.run1_value) == pytest.approx(5.0)
        assert float(result.run2_value) == pytest.approx(4.0)
        assert float(result.run3_value) == pytest.approx(5.0)
        assert float(result.result_value) == pytest.approx(14.0)
        assert result.t1_run1 is None


# ---------------------------------------------------------------------------
# GET handler — existing_t1_run1 etc surfaced to template
# ---------------------------------------------------------------------------


class TestGetHandlerExposesNewFields:
    def test_get_renders_existing_dual_timer_values(self, db_session, auth_client, tid):
        """A previously-scored heat should re-render with the existing T1/T2 values."""
        from models.event import EventResult
        comp = _make_pro(db_session, tid, name='Editor')
        event = _make_event(db_session, tid, name='Underhand')
        heat = _make_heat(db_session, event, competitors=[comp.id], status='completed')
        result = EventResult(
            event_id=event.id, competitor_id=comp.id,
            competitor_type='pro', competitor_name=comp.name,
            result_value=14.34, run1_value=14.34,
            t1_run1=14.32, t2_run1=14.36,
            status='completed',
        )
        db_session.add(result)
        db_session.flush()

        r = auth_client.get(_save_url(tid, heat.id))
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'value="14.32"' in body
        assert 'value="14.36"' in body
        assert '14.34' in body
