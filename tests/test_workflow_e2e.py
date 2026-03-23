"""
End-to-end tournament lifecycle tests.

Exercises the full path that a real tournament follows:
  1. Create tournament
  2. Setup events (college + pro)
  3. Register competitors (college teams + pro individuals)
  4. Generate heats
  5. Enter scores
  6. Calculate positions / finalize
  7. Verify standings, points, payouts

These tests use the shared conftest fixtures and hit real service code
(not mocks). They are the most valuable reliability guard after major changes.

Run:
    pytest tests/test_workflow_e2e.py -v
    pytest -m integration   (runs these + other integration tests)
"""
from __future__ import annotations

import json
import pytest
from tests.conftest import (
    make_tournament, make_team, make_college_competitor,
    make_pro_competitor, make_event, make_heat, make_event_result, make_flight,
)

pytestmark = pytest.mark.integration


# ===========================================================================
# COLLEGE WORKFLOW: register → heats → score → finalize → standings
# ===========================================================================

class TestCollegeWorkflow:
    """Full college competition lifecycle."""

    def test_college_event_full_lifecycle(self, db_session):
        """Create a college speed event with 4 competitors, score them, finalize."""
        import services.scoring_engine as engine

        # --- Setup ---
        t = make_tournament(db_session)
        team_a = make_team(db_session, t, code='UM-A', school='University of Montana', abbrev='UM')
        team_b = make_team(db_session, t, code='MSU-A', school='Montana State', abbrev='MSU')

        c1 = make_college_competitor(db_session, t, team_a, 'Alice Smith', 'F')
        c2 = make_college_competitor(db_session, t, team_a, 'Bob Jones', 'M')
        c3 = make_college_competitor(db_session, t, team_b, 'Carol Lee', 'F')
        c4 = make_college_competitor(db_session, t, team_b, 'Dave Kim', 'M')

        event = make_event(db_session, t, "Women's Underhand Speed",
                           event_type='college', gender='F',
                           scoring_type='time', scoring_order='lowest_wins',
                           stand_type='underhand')

        # --- Create heats with competitors ---
        heat = make_heat(db_session, event, heat_number=1,
                         competitors=[c1.id, c3.id])

        # --- Enter results ---
        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               result_value=25.3, status='completed')
        r2 = make_event_result(db_session, event, c3, competitor_type='college',
                               result_value=23.1, status='completed')

        db_session.flush()

        # --- Calculate positions ---
        engine.calculate_positions(event)
        db_session.flush()

        # Carol (23.1) should be 1st, Alice (25.3) should be 2nd
        assert r2.final_position == 1
        assert r1.final_position == 2

        # Points: 1st=10, 2nd=7
        assert r2.points_awarded == 10
        assert r1.points_awarded == 7

        # Competitor individual points updated
        assert c3.individual_points == 10
        assert c1.individual_points == 7

        # Team points updated
        assert team_b.total_points == 10
        assert team_a.total_points == 7

        # Event marked finalized
        assert event.is_finalized is True
        assert event.status == 'completed'

    def test_dual_run_event_uses_best_run(self, db_session):
        """Speed Climb: two runs, best (lowest) time counts."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'Runner A', 'M')
        c2 = make_college_competitor(db_session, t, team, 'Runner B', 'M')

        event = make_event(db_session, t, "Men's Speed Climb",
                           event_type='college', gender='M',
                           scoring_type='time', scoring_order='lowest_wins',
                           stand_type='speed_climb', requires_dual_runs=True)

        # Runner A: run1=18.0, run2=15.5 → best=15.5
        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               run1_value=18.0, run2_value=15.5, status='completed')
        r1.calculate_best_run('lowest_wins')

        # Runner B: run1=14.0, run2=16.0 → best=14.0
        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               run1_value=14.0, run2_value=16.0, status='completed')
        r2.calculate_best_run('lowest_wins')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        assert r2.final_position == 1  # 14.0
        assert r1.final_position == 2  # 15.5

    def test_recalculate_positions_is_idempotent(self, db_session):
        """Calling calculate_positions twice yields the same result."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'Competitor 1', 'M')
        c2 = make_college_competitor(db_session, t, team, 'Competitor 2', 'M')

        event = make_event(db_session, t, 'Test Event',
                           event_type='college', gender='M',
                           scoring_type='time')

        make_event_result(db_session, event, c1, competitor_type='college',
                          result_value=30.0, status='completed')
        make_event_result(db_session, event, c2, competitor_type='college',
                          result_value=28.0, status='completed')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()
        pts1 = c1.individual_points
        pts2 = c2.individual_points
        team_pts = team.total_points

        # Call again — should not double points
        engine.calculate_positions(event)
        db_session.flush()

        assert c1.individual_points == pts1
        assert c2.individual_points == pts2
        assert team.total_points == team_pts


# ===========================================================================
# PRO WORKFLOW: register → heats → score → finalize → payouts
# ===========================================================================

class TestProWorkflow:
    """Full pro competition lifecycle."""

    def test_pro_event_with_payouts(self, db_session):
        """Pro Underhand: 3 competitors, payouts for top 2, verify earnings."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p1 = make_pro_competitor(db_session, t, 'Pro Alice', 'F')
        p2 = make_pro_competitor(db_session, t, 'Pro Bob', 'M')
        p3 = make_pro_competitor(db_session, t, 'Pro Carol', 'F')

        event = make_event(db_session, t, "Women's Underhand",
                           event_type='pro', gender='F',
                           scoring_type='time', scoring_order='lowest_wins',
                           stand_type='underhand',
                           payouts={'1': 500, '2': 250})

        make_event_result(db_session, event, p1, competitor_type='pro',
                          result_value=22.5, status='completed')
        make_event_result(db_session, event, p2, competitor_type='pro',
                          result_value=20.1, status='completed')
        make_event_result(db_session, event, p3, competitor_type='pro',
                          result_value=24.0, status='completed')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        results = sorted(event.results.all(), key=lambda r: r.final_position)
        assert results[0].competitor_name == 'Pro Bob'
        assert results[0].payout_amount == 500
        assert results[1].competitor_name == 'Pro Alice'
        assert results[1].payout_amount == 250
        assert results[2].competitor_name == 'Pro Carol'
        assert results[2].payout_amount == 0

        # Earnings accumulated on competitor
        assert p2.total_earnings == 500
        assert p1.total_earnings == 250
        assert p3.total_earnings == 0

    def test_pro_event_with_flights(self, db_session):
        """Verify heats can be assigned to flights."""
        t = make_tournament(db_session)
        p1 = make_pro_competitor(db_session, t, 'Flyer A', 'M')

        event = make_event(db_session, t, 'Springboard',
                           event_type='pro', stand_type='springboard')
        flight = make_flight(db_session, t, flight_number=1)
        heat = make_heat(db_session, event, heat_number=1,
                         competitors=[p1.id],
                         flight_id=flight.id, flight_position=1)

        assert heat.flight_id == flight.id
        assert heat.flight_position == 1
        assert flight.heat_count == 1


# ===========================================================================
# MULTI-EVENT WORKFLOW: points accumulate across events
# ===========================================================================

class TestMultiEventWorkflow:
    """Verify points and earnings accumulate across multiple events."""

    def test_college_points_accumulate(self, db_session):
        """Points from multiple events sum on competitor and team."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'Multi Alice', 'F')
        c2 = make_college_competitor(db_session, t, team, 'Multi Bob', 'M')

        # Event 1: Alice 1st, Bob 2nd
        e1 = make_event(db_session, t, 'Event 1', event_type='college',
                         scoring_type='time')
        make_event_result(db_session, e1, c1, competitor_type='college',
                          result_value=10.0, status='completed')
        make_event_result(db_session, e1, c2, competitor_type='college',
                          result_value=12.0, status='completed')
        db_session.flush()
        engine.calculate_positions(e1)
        db_session.flush()

        # Event 2: Bob 1st, Alice 2nd
        e2 = make_event(db_session, t, 'Event 2', event_type='college',
                         scoring_type='time')
        make_event_result(db_session, e2, c1, competitor_type='college',
                          result_value=15.0, status='completed')
        make_event_result(db_session, e2, c2, competitor_type='college',
                          result_value=11.0, status='completed')
        db_session.flush()
        engine.calculate_positions(e2)
        db_session.flush()

        # Alice: 10 (1st) + 7 (2nd) = 17
        # Bob:   7 (2nd) + 10 (1st) = 17
        assert c1.individual_points == 17
        assert c2.individual_points == 17
        assert team.total_points == 34

    def test_pro_earnings_accumulate(self, db_session):
        """Payouts from multiple events sum on competitor."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p = make_pro_competitor(db_session, t, 'Rich Pro', 'M')

        e1 = make_event(db_session, t, 'Pro Event 1', event_type='pro',
                         scoring_type='time', payouts={'1': 1000})
        make_event_result(db_session, e1, p, competitor_type='pro',
                          result_value=20.0, status='completed')
        db_session.flush()
        engine.calculate_positions(e1)
        db_session.flush()

        e2 = make_event(db_session, t, 'Pro Event 2', event_type='pro',
                         scoring_type='time', payouts={'1': 500})
        make_event_result(db_session, e2, p, competitor_type='pro',
                          result_value=18.0, status='completed')
        db_session.flush()
        engine.calculate_positions(e2)
        db_session.flush()

        assert p.total_earnings == 1500


# ===========================================================================
# EDGE CASES
# ===========================================================================

class TestWorkflowEdgeCases:

    def test_no_completed_results_stays_in_progress(self, db_session):
        """Event with only pending results should not finalize."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p = make_pro_competitor(db_session, t, 'Pending Pro', 'M')
        event = make_event(db_session, t, 'Empty Event', event_type='pro',
                            scoring_type='time')
        make_event_result(db_session, event, p, competitor_type='pro',
                          result_value=None, status='pending')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        assert event.is_finalized is False
        assert event.status == 'in_progress'

    def test_scratched_competitor_excluded(self, db_session):
        """Scratched competitors should not receive positions or points."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'Active', 'M')
        c2 = make_college_competitor(db_session, t, team, 'Scratched', 'M')

        event = make_event(db_session, t, 'Scratch Test', event_type='college',
                            scoring_type='time')
        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               result_value=20.0, status='completed')
        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               result_value=18.0, status='scratched')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        assert r1.final_position == 1
        assert r2.final_position is None
        assert r2.points_awarded == 0

    def test_tied_competitors_get_same_position(self, db_session):
        """Two competitors with identical times should share the same position."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p1 = make_pro_competitor(db_session, t, 'Tie A', 'M')
        p2 = make_pro_competitor(db_session, t, 'Tie B', 'M')
        p3 = make_pro_competitor(db_session, t, 'Third', 'M')

        event = make_event(db_session, t, 'Tie Test', event_type='pro',
                            scoring_type='time')
        r1 = make_event_result(db_session, event, p1, competitor_type='pro',
                               result_value=20.0, status='completed')
        r2 = make_event_result(db_session, event, p2, competitor_type='pro',
                               result_value=20.0, status='completed')
        r3 = make_event_result(db_session, event, p3, competitor_type='pro',
                               result_value=25.0, status='completed')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        # Both tied at 20.0 → position 1; third at 25.0 → position 3 (skip 2)
        assert r1.final_position == 1
        assert r2.final_position == 1
        assert r3.final_position == 3
