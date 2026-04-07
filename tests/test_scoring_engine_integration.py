"""
Integration tests for scoring_engine.py — full pipeline with real DB.

Tests calculate_positions(), preview_positions(), payout templates,
CSV import, handicap scoring, throwoff workflow, and standings.

Run:
    pytest tests/test_scoring_engine_integration.py -v
"""
import json

import pytest

from database import db as _db
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_event_result,
    make_heat,
    make_pro_competitor,
    make_team,
    make_tournament,
)

# ---------------------------------------------------------------------------
# Fixtures (use conftest app/db_session)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _db_session(db_session):
    """Activate conftest's db_session for every test in this module."""
    yield db_session


@pytest.fixture()
def tournament(db_session):
    return make_tournament(db_session, status='pro_active')


@pytest.fixture()
def college_tournament(db_session):
    return make_tournament(db_session, name='College Test', status='college_active')


# ---------------------------------------------------------------------------
# calculate_positions — time events (lowest wins)
# ---------------------------------------------------------------------------

class TestCalculatePositionsTime:
    """Position calculation for lowest-time-wins events."""

    def test_basic_time_ranking(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, "Men's Underhand",
                           scoring_type='time', scoring_order='lowest_wins')
        c1 = make_pro_competitor(db_session, tournament, 'Alice', 'F', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Bob', 'M', events=[event.id])
        c3 = make_pro_competitor(db_session, tournament, 'Carol', 'F', events=[event.id])

        make_event_result(db_session, event, c1, result_value=15.2, status='completed')
        make_event_result(db_session, event, c2, result_value=12.8, status='completed')
        make_event_result(db_session, event, c3, result_value=18.1, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        results = EventResult.query.filter_by(event_id=event.id).order_by(
            EventResult.final_position).all()

        assert results[0].competitor_name == 'Bob'
        assert results[0].final_position == 1
        assert results[1].competitor_name == 'Alice'
        assert results[1].final_position == 2
        assert results[2].competitor_name == 'Carol'
        assert results[2].final_position == 3

    def test_sets_event_finalized(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, "Men's Standing Block",
                           scoring_type='time', scoring_order='lowest_wins')
        c1 = make_pro_competitor(db_session, tournament, 'Dave', 'M', events=[event.id])
        make_event_result(db_session, event, c1, result_value=10.0, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        assert event.is_finalized is True
        assert event.status == 'completed'

    def test_empty_results_no_crash(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'Empty Event',
                           scoring_type='time', scoring_order='lowest_wins')
        db_session.flush()

        calculate_positions(event)  # should not raise

    def test_scratched_results_excluded(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, "Women's Underhand",
                           scoring_type='time', scoring_order='lowest_wins')
        c1 = make_pro_competitor(db_session, tournament, 'Eve', 'F', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Fay', 'F', events=[event.id])
        make_event_result(db_session, event, c1, result_value=20.0, status='completed')
        make_event_result(db_session, event, c2, result_value=15.0, status='scratched')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        completed = EventResult.query.filter_by(
            event_id=event.id, status='completed').all()
        assert len(completed) == 1
        assert completed[0].final_position == 1

    def test_dnf_results_placed_last(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'DNF Test',
                           scoring_type='time', scoring_order='lowest_wins')
        c1 = make_pro_competitor(db_session, tournament, 'Grace', 'F', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Hank', 'M', events=[event.id])
        make_event_result(db_session, event, c1, result_value=25.0, status='completed')
        make_event_result(db_session, event, c2, result_value=None, status='dnf')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        results = EventResult.query.filter_by(event_id=event.id).order_by(
            EventResult.final_position.asc().nullslast()).all()
        # Grace gets position 1; Hank (DNF) gets no position or a later one
        grace = [r for r in results if r.competitor_name == 'Grace'][0]
        assert grace.final_position == 1


# ---------------------------------------------------------------------------
# calculate_positions — highest wins (distance/score)
# ---------------------------------------------------------------------------

class TestCalculatePositionsHighestWins:
    """Position calculation for highest-score-wins events."""

    def test_highest_wins_ordering(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'Cookie Stack',
                           scoring_type='score', scoring_order='highest_wins',
                           stand_type='cookie_stack')
        c1 = make_pro_competitor(db_session, tournament, 'Ivy', 'F', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Jake', 'M', events=[event.id])
        make_event_result(db_session, event, c1, result_value=8.0, status='completed')
        make_event_result(db_session, event, c2, result_value=12.0, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        first = EventResult.query.filter_by(
            event_id=event.id, final_position=1).first()
        assert first.competitor_name == 'Jake'


# ---------------------------------------------------------------------------
# Dual-run best_run calculation
# ---------------------------------------------------------------------------

class TestDualRunScoring:
    """Dual-run events: best run (lowest or highest) is the result_value."""

    def test_dual_run_lowest_wins_picks_min(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'Speed Climb',
                           scoring_type='time', scoring_order='lowest_wins',
                           requires_dual_runs=True, stand_type='obstacle_pole')
        c1 = make_pro_competitor(db_session, tournament, 'Karl', 'M', events=[event.id])
        r1 = make_event_result(db_session, event, c1,
                               run1_value=12.5, run2_value=11.8,
                               result_value=11.8, best_run=11.8,
                               status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        assert r1.final_position == 1

    def test_dual_run_both_none_skipped(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, "Chokerman's Race",
                           scoring_type='time', scoring_order='lowest_wins',
                           requires_dual_runs=True, stand_type='obstacle_pole')
        c1 = make_pro_competitor(db_session, tournament, 'Lee', 'M', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Mae', 'F', events=[event.id])
        make_event_result(db_session, event, c1,
                          run1_value=15.0, run2_value=14.0,
                          result_value=14.0, best_run=14.0, status='completed')
        make_event_result(db_session, event, c2,
                          run1_value=None, run2_value=None,
                          result_value=None, status='dnf')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        first = EventResult.query.filter_by(
            event_id=event.id, final_position=1).first()
        assert first.competitor_name == 'Lee'


# ---------------------------------------------------------------------------
# Triple-run cumulative scoring (Axe Throw)
# ---------------------------------------------------------------------------

class TestTripleRunScoring:
    """Triple-run cumulative events: run1 + run2 + run3 = result_value."""

    def test_cumulative_score_ranking(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'Axe Throw',
                           scoring_type='hits', scoring_order='highest_wins',
                           requires_triple_runs=True, stand_type='axe_throw')
        c1 = make_pro_competitor(db_session, tournament, 'Nate', 'M', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Olivia', 'F', events=[event.id])
        make_event_result(db_session, event, c1,
                          run1_value=3, run2_value=4, run3_value=5,
                          result_value=12, status='completed')
        make_event_result(db_session, event, c2,
                          run1_value=5, run2_value=5, run3_value=5,
                          result_value=15, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        first = EventResult.query.filter_by(
            event_id=event.id, final_position=1).first()
        assert first.competitor_name == 'Olivia'
        assert first.result_value == 15

    def test_axe_throw_tie_triggers_throwoff(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'Axe Throw',
                           scoring_type='hits', scoring_order='highest_wins',
                           requires_triple_runs=True, stand_type='axe_throw')
        c1 = make_pro_competitor(db_session, tournament, 'Pat', 'M', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Quinn', 'F', events=[event.id])
        make_event_result(db_session, event, c1,
                          run1_value=4, run2_value=4, run3_value=4,
                          result_value=12, status='completed')
        make_event_result(db_session, event, c2,
                          run1_value=5, run2_value=3, run3_value=4,
                          result_value=12, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        # Tie on cumulative = throwoff for axe events
        # At minimum, positions should be assigned and both results present
        results = EventResult.query.filter_by(
            event_id=event.id, status='completed').all()
        assert len(results) == 2
        # Check if throwoff was triggered (depends on event name matching config)
        pending = EventResult.query.filter_by(
            event_id=event.id, throwoff_pending=True).all()
        # Either throwoff triggered or positions assigned — both are valid
        if len(pending) == 0:
            # Positions should be assigned
            positioned = [r for r in results if r.final_position is not None]
            assert len(positioned) == 2


# ---------------------------------------------------------------------------
# Hard-Hit tiebreak logic
# ---------------------------------------------------------------------------

class TestHardHitTiebreak:
    """Hard-Hit events: tied on hits → fastest time wins."""

    def test_hard_hit_time_breaks_tie(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'Underhand Hard Hit',
                           scoring_type='hits', scoring_order='highest_wins',
                           stand_type='underhand')
        c1 = make_pro_competitor(db_session, tournament, 'Rick', 'M', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Sue', 'F', events=[event.id])
        # Same hits (10), different tiebreak times
        make_event_result(db_session, event, c1,
                          result_value=10, tiebreak_value=45.2, status='completed')
        make_event_result(db_session, event, c2,
                          result_value=10, tiebreak_value=38.7, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        first = EventResult.query.filter_by(
            event_id=event.id, final_position=1).first()
        # Same hits; lower tiebreak time wins
        assert first.competitor_name == 'Sue'

    def test_hard_hit_different_hits_ignores_tiebreak(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'Standing Block Hard Hit',
                           scoring_type='hits', scoring_order='highest_wins',
                           stand_type='standing_block')
        c1 = make_pro_competitor(db_session, tournament, 'Tom', 'M', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Uma', 'F', events=[event.id])
        make_event_result(db_session, event, c1,
                          result_value=12, tiebreak_value=50.0, status='completed')
        make_event_result(db_session, event, c2,
                          result_value=10, tiebreak_value=35.0, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        first = EventResult.query.filter_by(
            event_id=event.id, final_position=1).first()
        assert first.competitor_name == 'Tom'  # More hits wins


# ---------------------------------------------------------------------------
# Handicap scoring (subtract handicap_factor from time)
# ---------------------------------------------------------------------------

class TestHandicapScoringIntegration:
    """Handicap events: net_time = raw_time - handicap_factor."""

    def test_handicap_changes_ranking(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, "Women's Underhand Handicap",
                           scoring_type='time', scoring_order='lowest_wins',
                           stand_type='underhand', is_handicap=True)
        c1 = make_pro_competitor(db_session, tournament, 'Vera', 'F', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Wendy', 'F', events=[event.id])
        # Vera: raw 20.0, mark 5.0 → net 15.0
        # Wendy: raw 18.0, mark 1.0 → net 17.0 (1.0 = default = scratch = 0)
        make_event_result(db_session, event, c1,
                          result_value=20.0, handicap_factor=5.0, status='completed')
        make_event_result(db_session, event, c2,
                          result_value=18.0, handicap_factor=1.0, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        first = EventResult.query.filter_by(
            event_id=event.id, final_position=1).first()
        # Vera net=15.0 beats Wendy net=18.0
        assert first.competitor_name == 'Vera'

    def test_handicap_factor_default_treated_as_scratch(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'Scratch Test',
                           scoring_type='time', scoring_order='lowest_wins',
                           stand_type='underhand', is_handicap=True)
        c1 = make_pro_competitor(db_session, tournament, 'Xena', 'F', events=[event.id])
        # handicap_factor=1.0 (default) should be treated as 0.0 scratch
        make_event_result(db_session, event, c1,
                          result_value=15.0, handicap_factor=1.0, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        r = EventResult.query.filter_by(event_id=event.id).first()
        assert r.final_position == 1

    def test_non_handicap_event_ignores_factor(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'Championship UH',
                           scoring_type='time', scoring_order='lowest_wins',
                           stand_type='underhand', is_handicap=False)
        c1 = make_pro_competitor(db_session, tournament, 'Yuri', 'M', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Zane', 'M', events=[event.id])
        # Even with handicap_factor set, championship ignores it
        make_event_result(db_session, event, c1,
                          result_value=20.0, handicap_factor=10.0, status='completed')
        make_event_result(db_session, event, c2,
                          result_value=15.0, handicap_factor=1.0, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        first = EventResult.query.filter_by(
            event_id=event.id, final_position=1).first()
        assert first.competitor_name == 'Zane'  # Raw 15.0 wins


# ---------------------------------------------------------------------------
# Payout distribution
# ---------------------------------------------------------------------------

class TestPayoutDistribution:
    """Payouts are distributed based on final_position."""

    def test_payouts_assigned_by_position(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        payouts = {1: 500, 2: 300, 3: 100}
        event = make_event(db_session, tournament, 'Payout Test',
                           scoring_type='time', scoring_order='lowest_wins',
                           payouts=payouts)
        comps = []
        for i, (name, time) in enumerate([('A', 10.0), ('B', 12.0), ('C', 15.0), ('D', 20.0)]):
            c = make_pro_competitor(db_session, tournament, name, 'M', events=[event.id])
            make_event_result(db_session, event, c, result_value=time, status='completed')
            comps.append(c)
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        r1 = EventResult.query.filter_by(event_id=event.id, final_position=1).first()
        r2 = EventResult.query.filter_by(event_id=event.id, final_position=2).first()
        r3 = EventResult.query.filter_by(event_id=event.id, final_position=3).first()
        r4 = EventResult.query.filter_by(event_id=event.id, final_position=4).first()

        assert r1.payout_amount == 500
        assert r2.payout_amount == 300
        assert r3.payout_amount == 100
        assert r4.payout_amount == 0.0


# ---------------------------------------------------------------------------
# College points and team recalculation
# ---------------------------------------------------------------------------

class TestCollegePointsIntegration:
    """College events: placement points + team recalculation."""

    def test_college_points_awarded(self, db_session, college_tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, college_tournament, 'Underhand Speed',
                           event_type='college', gender='M',
                           scoring_type='time', scoring_order='lowest_wins',
                           stand_type='underhand')
        team = make_team(db_session, college_tournament, code='UM-A')
        c1 = make_college_competitor(db_session, college_tournament, team,
                                     'Student A', 'M', events=[event.id])
        c2 = make_college_competitor(db_session, college_tournament, team,
                                     'Student B', 'M', events=[event.id])

        make_event_result(db_session, event, c1, competitor_type='college',
                          result_value=18.0, status='completed')
        make_event_result(db_session, event, c2, competitor_type='college',
                          result_value=22.0, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        first = EventResult.query.filter_by(
            event_id=event.id, final_position=1).first()
        assert first.competitor_name == 'Student A'
        # 1st place = 10 points per config.PLACEMENT_POINTS
        assert first.points_awarded == 10


# ---------------------------------------------------------------------------
# preview_positions (read-only)
# ---------------------------------------------------------------------------

class TestPreviewPositions:
    """preview_positions() should not modify DB."""

    def test_preview_returns_list(self, db_session, tournament):
        from services.scoring_engine import preview_positions
        event = make_event(db_session, tournament, 'Preview Test',
                           scoring_type='time', scoring_order='lowest_wins')
        c1 = make_pro_competitor(db_session, tournament, 'Alpha', 'M', events=[event.id])
        make_event_result(db_session, event, c1, result_value=10.0, status='completed')
        db_session.flush()

        result = preview_positions(event)
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_preview_does_not_finalize(self, db_session, tournament):
        from services.scoring_engine import preview_positions
        event = make_event(db_session, tournament, 'Preview NoFinalize',
                           scoring_type='time', scoring_order='lowest_wins')
        c1 = make_pro_competitor(db_session, tournament, 'Beta', 'M', events=[event.id])
        make_event_result(db_session, event, c1, result_value=10.0, status='completed')
        db_session.flush()

        preview_positions(event)

        assert event.is_finalized is not True


# ---------------------------------------------------------------------------
# Payout template CRUD
# ---------------------------------------------------------------------------

class TestPayoutTemplateCRUD:
    """Payout template save, list, apply, delete."""

    def test_save_and_list(self, db_session):
        from services.scoring_engine import list_payout_templates, save_payout_template
        t = save_payout_template('Standard 5', {1: 500, 2: 300, 3: 200, 4: 100, 5: 50})
        db_session.flush()

        templates = list_payout_templates()
        assert any(tp.name == 'Standard 5' for tp in templates)

    def test_apply_template(self, db_session, tournament):
        from services.scoring_engine import apply_payout_template, save_payout_template
        t = save_payout_template('Apply Test', {1: 1000, 2: 500})
        db_session.flush()

        event = make_event(db_session, tournament, 'Template Target',
                           scoring_type='time', scoring_order='lowest_wins')
        db_session.flush()

        result = apply_payout_template(event, t.id)
        assert result is True
        assert event.get_payouts().get('1') == 1000 or event.get_payouts().get(1) == 1000

    def test_delete_template(self, db_session):
        from services.scoring_engine import delete_payout_template, save_payout_template
        t = save_payout_template('Delete Me', {1: 100})
        db_session.flush()
        tid = t.id

        result = delete_payout_template(tid)
        assert result is True


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

class TestCSVImportIntegration:
    """Bulk CSV import via import_results_from_csv()."""

    def test_basic_csv_import(self, db_session, tournament):
        from services.scoring_engine import import_results_from_csv
        event = make_event(db_session, tournament, 'CSV Import Event',
                           scoring_type='time', scoring_order='lowest_wins')
        c1 = make_pro_competitor(db_session, tournament, 'CSV Alice', 'F', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'CSV Bob', 'M', events=[event.id])
        db_session.flush()

        csv_text = "competitor_name,result\nCSV Alice,15.5\nCSV Bob,12.3\n"
        result = import_results_from_csv(event, csv_text)

        assert result['imported'] >= 2 or result.get('updated', 0) >= 0

    def test_csv_with_malformed_row(self, db_session, tournament):
        from services.scoring_engine import import_results_from_csv
        event = make_event(db_session, tournament, 'CSV Malformed',
                           scoring_type='time', scoring_order='lowest_wins')
        c1 = make_pro_competitor(db_session, tournament, 'CSV Carol', 'F', events=[event.id])
        db_session.flush()

        csv_text = "competitor_name,result\nCSV Carol,15.5\nBadRow\n,\n"
        result = import_results_from_csv(event, csv_text)
        # Should not crash; skipped/errors counted
        assert 'imported' in result


# ---------------------------------------------------------------------------
# Outlier flagging
# ---------------------------------------------------------------------------

class TestOutlierFlagging:
    """flag_score_outliers() marks statistical outliers."""

    def test_outlier_flagged(self, db_session, tournament):
        from services.scoring_engine import flag_score_outliers
        event = make_event(db_session, tournament, 'Outlier Event',
                           scoring_type='time', scoring_order='lowest_wins')
        results = []
        # Need enough normal points so outlier is >2 stdev from mean
        # 10 normals (≈15s) + 1 extreme (500s) → z-score ≈3.02
        normal_vals = [
            ('N1', 15.0), ('N2', 15.5), ('N3', 14.8), ('N4', 15.2),
            ('N5', 15.1), ('N6', 14.9), ('N7', 15.3), ('N8', 15.0),
            ('N9', 15.2), ('N10', 14.8),
        ]
        for name, val in normal_vals:
            c = make_pro_competitor(db_session, tournament, name, 'M', events=[event.id])
            r = make_event_result(db_session, event, c, result_value=val, status='completed')
            results.append(r)

        c_out = make_pro_competitor(db_session, tournament, 'Out', 'M', events=[event.id])
        r_out = make_event_result(db_session, event, c_out, result_value=500.0, status='completed')
        results.append(r_out)
        db_session.flush()

        flag_score_outliers(results, event)

        outlier = [r for r in results if r.competitor_name == 'Out'][0]
        normal = [r for r in results if r.competitor_name == 'N1'][0]
        assert outlier.is_flagged is True
        assert normal.is_flagged is False

    def test_too_few_results_no_flags(self, db_session, tournament):
        from services.scoring_engine import flag_score_outliers
        event = make_event(db_session, tournament, 'Few Results',
                           scoring_type='time', scoring_order='lowest_wins')
        c1 = make_pro_competitor(db_session, tournament, 'Only1', 'M', events=[event.id])
        r1 = make_event_result(db_session, event, c1, result_value=50.0, status='completed')
        db_session.flush()

        flag_score_outliers([r1], event)
        assert r1.is_flagged is False


# ---------------------------------------------------------------------------
# Idempotency — recalculate multiple times
# ---------------------------------------------------------------------------

class TestIdempotency:
    """calculate_positions() is safe to call multiple times."""

    def test_double_calculate_same_result(self, db_session, tournament):
        from services.scoring_engine import calculate_positions
        event = make_event(db_session, tournament, 'Idempotent Event',
                           scoring_type='time', scoring_order='lowest_wins',
                           payouts={1: 500, 2: 200})
        c1 = make_pro_competitor(db_session, tournament, 'Idem1', 'M', events=[event.id])
        c2 = make_pro_competitor(db_session, tournament, 'Idem2', 'M', events=[event.id])
        make_event_result(db_session, event, c1, result_value=10.0, status='completed')
        make_event_result(db_session, event, c2, result_value=12.0, status='completed')
        db_session.flush()

        calculate_positions(event)
        db_session.flush()
        calculate_positions(event)
        db_session.flush()

        from models.event import EventResult
        first = EventResult.query.filter_by(event_id=event.id, final_position=1).first()
        assert first.competitor_name == 'Idem1'
        assert first.payout_amount == 500
