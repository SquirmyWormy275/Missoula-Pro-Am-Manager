"""
Phase 3 of the V2.8.0 scoring fix — split-tie points + rebuild SUM tests.

Verifies that:

1. ``split_tie_points()`` produces the AWFC fractional values for every common
   tie shape (1-way through 6-way and beyond-table).
2. ``calculate_positions()`` writes the split values into ``points_awarded``
   and rebuilds ``individual_points`` from SUM, NOT from delta arithmetic.
3. The rebuild path is idempotent — calling ``calculate_positions()`` twice
   on the same event produces identical totals.
4. Partner events award full split-points to BOTH partners independently
   (the AWFC dual-credit rule), and team totals reflect the points twice.
5. ``record_throwoff_result()`` uses the same SUM rebuild path as
   ``calculate_positions()`` so the two functions stay consistent
   (PLAN_REVIEW.md A6 regression).
6. JSON-boundary numerics in ``preview_positions()`` and ``live_standings_data()``
   are plain ``float`` (no Decimal) so ``jsonify`` works.
"""
import json
import os
from decimal import Decimal

import pytest

from database import db as _db

# ---------------------------------------------------------------------------
# Self-contained app fixture (module-scoped, mirrors test_routes_post pattern)
# ---------------------------------------------------------------------------

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
    if not User.query.filter_by(username='st_admin').first():
        u = User(username='st_admin', role='admin')
        u.set_password('st_pass')
        _db.session.add(u)
    if not Tournament.query.first():
        t = Tournament(name='Phase 3 Test 2026', year=2026, status='setup')
        _db.session.add(t)
    _db.session.commit()


@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def tid(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.first().id


# ---------------------------------------------------------------------------
# Local seed helpers
# ---------------------------------------------------------------------------

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


def _make_result(session, event, comp, value, status='completed', partner_name=None):
    from models.event import EventResult
    r = EventResult(
        event_id=event.id, competitor_id=comp.id,
        competitor_type='college', competitor_name=comp.name,
        partner_name=partner_name,
        result_value=value, run1_value=value,
        status=status,
    )
    session.add(r)
    session.flush()
    return r


# ===========================================================================
# 1. split_tie_points() — pure-function tests
# ===========================================================================


class TestSplitTiePointsHelper:
    """Pure-function tests for the split-tie helper."""

    def test_solo_first_place(self):
        from services.scoring_engine import split_tie_points
        assert split_tie_points(1, 1) == Decimal('10.00')

    def test_solo_sixth_place(self):
        from services.scoring_engine import split_tie_points
        assert split_tie_points(6, 1) == Decimal('1.00')

    def test_seventh_and_below_zero(self):
        from services.scoring_engine import split_tie_points
        assert split_tie_points(7, 1) == Decimal('0.00')
        assert split_tie_points(20, 1) == Decimal('0.00')

    def test_two_way_tie_for_first(self):
        """(10 + 7) / 2 = 8.5"""
        from services.scoring_engine import split_tie_points
        assert split_tie_points(1, 2) == Decimal('8.50')

    def test_three_way_tie_for_first(self):
        """(10 + 7 + 5) / 3 = 7.33..."""
        from services.scoring_engine import split_tie_points
        assert split_tie_points(1, 3) == Decimal('7.33')

    def test_two_way_tie_for_second(self):
        """(7 + 5) / 2 = 6.0"""
        from services.scoring_engine import split_tie_points
        assert split_tie_points(2, 2) == Decimal('6.00')

    def test_two_way_tie_for_fifth(self):
        """(2 + 1) / 2 = 1.5"""
        from services.scoring_engine import split_tie_points
        assert split_tie_points(5, 2) == Decimal('1.50')

    def test_six_way_tie_for_first(self):
        """All 6 places shared: (10+7+5+3+2+1)/6 = 4.67"""
        from services.scoring_engine import split_tie_points
        assert split_tie_points(1, 6) == Decimal('4.67')

    def test_tie_spilling_past_table(self):
        """Three tied for 6th: only 6th has any points; (1+0+0)/3 = 0.33"""
        from services.scoring_engine import split_tie_points
        assert split_tie_points(6, 3) == Decimal('0.33')

    def test_tie_entirely_past_table(self):
        """Three tied for 8th: all zero, (0+0+0)/3 = 0"""
        from services.scoring_engine import split_tie_points
        assert split_tie_points(8, 3) == Decimal('0.00')


# ===========================================================================
# 2. calculate_positions() with fractional split-ties
# ===========================================================================


class TestCalculatePositionsSplitTie:
    """End-to-end split-tie behavior in the engine."""

    def test_two_way_tie_first_place_credits_individual_points(self, db_session, tid):
        """Both tied competitors get 8.5 points; their individual_points reflect it."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tid)
        c1 = _make_college(db_session, tid, team.id, 'A')
        c2 = _make_college(db_session, tid, team.id, 'B')
        c3 = _make_college(db_session, tid, team.id, 'C')
        event = _make_event(db_session, tid)
        _make_result(db_session, event, c1, 20.0)
        _make_result(db_session, event, c2, 20.0)
        _make_result(db_session, event, c3, 25.0)
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        r1 = event.results.filter_by(competitor_id=c1.id).first()
        r2 = event.results.filter_by(competitor_id=c2.id).first()
        r3 = event.results.filter_by(competitor_id=c3.id).first()
        assert r1.points_awarded == Decimal('8.50')
        assert r2.points_awarded == Decimal('8.50')
        assert r3.points_awarded == Decimal('5.00')  # 3rd place (positions 2 skipped)

        # individual_points reflects the SUM rebuild
        from models.competitor import CollegeCompetitor
        c1_loaded = CollegeCompetitor.query.get(c1.id)
        c2_loaded = CollegeCompetitor.query.get(c2.id)
        c3_loaded = CollegeCompetitor.query.get(c3.id)
        assert c1_loaded.individual_points == Decimal('8.50')
        assert c2_loaded.individual_points == Decimal('8.50')
        assert c3_loaded.individual_points == Decimal('5.00')

    def test_three_way_tie_first_place(self, db_session, tid):
        """Three tied for 1st each get (10+7+5)/3 = 7.33."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tid)
        c1 = _make_college(db_session, tid, team.id, 'A')
        c2 = _make_college(db_session, tid, team.id, 'B')
        c3 = _make_college(db_session, tid, team.id, 'C')
        c4 = _make_college(db_session, tid, team.id, 'D')
        event = _make_event(db_session, tid)
        _make_result(db_session, event, c1, 20.0)
        _make_result(db_session, event, c2, 20.0)
        _make_result(db_session, event, c3, 20.0)
        _make_result(db_session, event, c4, 25.0)
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        r1 = event.results.filter_by(competitor_id=c1.id).first()
        r2 = event.results.filter_by(competitor_id=c2.id).first()
        r3 = event.results.filter_by(competitor_id=c3.id).first()
        r4 = event.results.filter_by(competitor_id=c4.id).first()
        assert r1.points_awarded == Decimal('7.33')
        assert r2.points_awarded == Decimal('7.33')
        assert r3.points_awarded == Decimal('7.33')
        # Position 4 (positions 1, 2, 3 consumed by the tie)
        assert r4.final_position == 4
        assert r4.points_awarded == Decimal('3.00')

    def test_team_total_points_uses_fractional_sum(self, db_session, tid):
        """Team totals correctly accumulate fractional individual points."""
        from models.team import Team
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tid)
        c1 = _make_college(db_session, tid, team.id, 'A')
        c2 = _make_college(db_session, tid, team.id, 'B')
        # Need 2 of each gender to satisfy team validity later (not required
        # by the scoring engine itself but kept consistent with model expectations).
        event = _make_event(db_session, tid)
        _make_result(db_session, event, c1, 20.0)
        _make_result(db_session, event, c2, 20.0)
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        team_loaded = Team.query.get(team.id)
        # Two competitors tied for 1st: each gets 8.5, team gets 17.0
        assert team_loaded.total_points == Decimal('17.00')


# ===========================================================================
# 3. Idempotency — run calculate_positions twice
# ===========================================================================


class TestRebuildIdempotency:
    """The SUM rebuild path must be idempotent under repeat calls."""

    def test_running_calculate_positions_twice_yields_same_totals(self, db_session, tid):
        from models.competitor import CollegeCompetitor
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tid)
        c1 = _make_college(db_session, tid, team.id, 'A')
        c2 = _make_college(db_session, tid, team.id, 'B')
        event = _make_event(db_session, tid)
        _make_result(db_session, event, c1, 18.0)
        _make_result(db_session, event, c2, 22.0)
        db_session.flush()

        calculate_positions(event)
        db_session.flush()
        c1_first = CollegeCompetitor.query.get(c1.id).individual_points
        c2_first = CollegeCompetitor.query.get(c2.id).individual_points

        # Second run — should produce identical results.
        calculate_positions(event)
        db_session.flush()
        c1_second = CollegeCompetitor.query.get(c1.id).individual_points
        c2_second = CollegeCompetitor.query.get(c2.id).individual_points

        assert c1_second == c1_first
        assert c2_second == c2_first
        assert c1_first == Decimal('10.00')
        assert c2_first == Decimal('7.00')


# ===========================================================================
# 4. Partner event dual-credit (AWFC rule, PLAN_REVIEW.md T2)
# ===========================================================================


class TestPartnerEventDualCredit:
    """Both partners receive the same split-tie points; team total counts both."""

    def test_jack_and_jill_both_partners_credited(self, db_session, tid):
        """In a partnered event, BOTH competitors on the pair get full points."""
        from models.competitor import CollegeCompetitor
        from models.team import Team
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tid)
        # Pair A: Mike + Mary (one row each, same partner_name)
        mike = _make_college(db_session, tid, team.id, 'Mike', gender='M')
        mary = _make_college(db_session, tid, team.id, 'Mary', gender='F')
        # Pair B: Bob + Beth
        bob = _make_college(db_session, tid, team.id, 'Bob', gender='M')
        beth = _make_college(db_session, tid, team.id, 'Beth', gender='F')

        event = _make_event(db_session, tid, name='Jack & Jill',
                            scoring_type='time', is_partnered=True,
                            partner_gender_requirement='mixed', stand_type='saw_hand')

        # Pair A wins with 22.0; Pair B places 2nd with 24.0.
        _make_result(db_session, event, mike, 22.0, partner_name='Mary')
        _make_result(db_session, event, mary, 22.0, partner_name='Mike')
        _make_result(db_session, event, bob, 24.0, partner_name='Beth')
        _make_result(db_session, event, beth, 24.0, partner_name='Bob')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        # Both Pair A members get full 1st-place points (10 each).
        assert CollegeCompetitor.query.get(mike.id).individual_points == Decimal('10.00')
        assert CollegeCompetitor.query.get(mary.id).individual_points == Decimal('10.00')
        # Both Pair B members get full 2nd-place points (7 each).
        assert CollegeCompetitor.query.get(bob.id).individual_points == Decimal('7.00')
        assert CollegeCompetitor.query.get(beth.id).individual_points == Decimal('7.00')

        # The team total reflects ALL FOUR contributions: 10 + 10 + 7 + 7 = 34
        # (this is the AWFC "team gets points twice" rule from ProAM requirements).
        assert Team.query.get(team.id).total_points == Decimal('34.00')

    def test_partner_event_with_two_pairs_tied_for_first(self, db_session, tid):
        """Two pairs tie for 1st: all 4 competitors get the split value (8.5)."""
        from models.competitor import CollegeCompetitor
        from models.team import Team
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tid)
        a1 = _make_college(db_session, tid, team.id, 'A1', gender='M')
        a2 = _make_college(db_session, tid, team.id, 'A2', gender='F')
        b1 = _make_college(db_session, tid, team.id, 'B1', gender='M')
        b2 = _make_college(db_session, tid, team.id, 'B2', gender='F')

        event = _make_event(db_session, tid, name='Jack & Jill Tied',
                            scoring_type='time', is_partnered=True,
                            partner_gender_requirement='mixed', stand_type='saw_hand')

        # Both pairs tie at 22.0 — split position 1 + 2 → 8.5 each.
        _make_result(db_session, event, a1, 22.0, partner_name='A2')
        _make_result(db_session, event, a2, 22.0, partner_name='A1')
        _make_result(db_session, event, b1, 22.0, partner_name='B2')
        _make_result(db_session, event, b2, 22.0, partner_name='B1')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        for c in (a1, a2, b1, b2):
            assert CollegeCompetitor.query.get(c.id).individual_points == Decimal('8.50')

        # Team total: 8.5 * 4 = 34.0
        assert Team.query.get(team.id).total_points == Decimal('34.00')


# ===========================================================================
# 5. Throw-off path uses the same SUM rebuild
# ===========================================================================


class TestRecordThrowoffResultRebuild:
    """record_throwoff_result must use _rebuild_individual_points, not delta math."""

    def test_throwoff_individual_points_match_sum_after_resolve(self, db_session, tid):
        """After throw-off, individual_points must equal SUM(points_awarded)."""
        from models.competitor import CollegeCompetitor
        from services.scoring_engine import calculate_positions, record_throwoff_result
        team = _make_team(db_session, tid)
        c1 = _make_college(db_session, tid, team.id, 'Pat')
        c2 = _make_college(db_session, tid, team.id, 'Quinn')
        event = _make_event(
            db_session, tid, name='Axe Throw',
            scoring_type='score', scoring_order='highest_wins',
            requires_triple_runs=True, stand_type='axe_throw',
        )
        # Both end with cumulative 12 → tie → throw-off pending after calculate.
        _make_result(db_session, event, c1, 12.0)
        _make_result(db_session, event, c2, 12.0)
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        # The judge resolves: c1 → position 1, c2 → position 2.
        r1 = event.results.filter_by(competitor_id=c1.id).first()
        r2 = event.results.filter_by(competitor_id=c2.id).first()
        record_throwoff_result(event, {r1.id: 1, r2.id: 2})
        db_session.flush()

        # individual_points must equal each row's points_awarded after rebuild.
        c1_loaded = CollegeCompetitor.query.get(c1.id)
        c2_loaded = CollegeCompetitor.query.get(c2.id)
        assert c1_loaded.individual_points == r1.points_awarded
        assert c2_loaded.individual_points == r2.points_awarded
        assert c1_loaded.individual_points == Decimal('10.00')  # 1st place
        assert c2_loaded.individual_points == Decimal('7.00')   # 2nd place


# ===========================================================================
# 6. JSON-boundary numerics — preview_positions and live_standings_data
# ===========================================================================


class TestJsonBoundaryFloatCast:
    """preview_positions / live_standings_data must return plain floats (not Decimal)."""

    def test_preview_positions_returns_float_points(self, db_session, tid):
        from services.scoring_engine import preview_positions
        team = _make_team(db_session, tid)
        c1 = _make_college(db_session, tid, team.id, 'A')
        c2 = _make_college(db_session, tid, team.id, 'B')
        event = _make_event(db_session, tid)
        _make_result(db_session, event, c1, 20.0)
        _make_result(db_session, event, c2, 25.0)
        db_session.flush()

        preview = preview_positions(event)
        assert len(preview) == 2
        for row in preview:
            # The points field MUST be a plain float so jsonify works.
            assert isinstance(row['points'], float)
            # The result_value field also gets float-cast.
            assert isinstance(row['result_value'], float)
        # First place: 10.0
        assert preview[0]['points'] == pytest.approx(10.0)
        assert preview[1]['points'] == pytest.approx(7.0)

    def test_preview_positions_split_tie_in_modal(self, db_session, tid):
        """Two-way tie shows 8.5 in the preview modal."""
        from services.scoring_engine import preview_positions
        team = _make_team(db_session, tid)
        c1 = _make_college(db_session, tid, team.id, 'A')
        c2 = _make_college(db_session, tid, team.id, 'B')
        event = _make_event(db_session, tid)
        _make_result(db_session, event, c1, 20.0)
        _make_result(db_session, event, c2, 20.0)
        db_session.flush()

        preview = preview_positions(event)
        for row in preview:
            assert row['points'] == pytest.approx(8.5)
            # tied_with annotation lets the UI show a "shared" badge.
            assert row['tied_with'] == 2

    def test_live_standings_data_returns_float_only(self, db_session, tid):
        """live_standings_data must not contain any Decimal — jsonify would crash."""
        from services.scoring_engine import live_standings_data
        team = _make_team(db_session, tid)
        c1 = _make_college(db_session, tid, team.id, 'A')
        event = _make_event(db_session, tid)
        _make_result(db_session, event, c1, 18.5)
        db_session.flush()

        data = live_standings_data(event)
        assert 'rows' in data
        for row in data['rows']:
            for key in ('result_value', 'run1_value', 'run2_value', 'run3_value', 'best_run'):
                v = row.get(key)
                assert v is None or isinstance(v, float), \
                    f'{key} = {v!r} (type {type(v).__name__}), expected float or None'

    def test_jsonify_handles_live_standings(self, db_session, tid, app):
        """Smoke test: actually run jsonify() on the dict to confirm it doesn't TypeError."""
        from flask import jsonify

        from services.scoring_engine import live_standings_data
        team = _make_team(db_session, tid)
        c1 = _make_college(db_session, tid, team.id, 'A')
        event = _make_event(db_session, tid)
        _make_result(db_session, event, c1, 18.5)
        db_session.flush()

        data = live_standings_data(event)
        with app.test_request_context():
            response = jsonify(data)
            assert response.status_code == 200
            assert b'18.5' in response.data
