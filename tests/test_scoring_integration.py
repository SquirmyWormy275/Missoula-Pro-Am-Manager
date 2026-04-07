"""
Scoring pipeline integration tests.

Feeds real multi-competitor data through the full scoring engine path:
  - _metric() with various event types
  - _sort_key() ordering
  - calculate_positions() end-to-end
  - Handicap scoring with start marks
  - Hard-Hit tiebreak logic
  - Axe throw cumulative + throwoff detection
  - Outlier flagging with statistical data
  - preview_positions() without DB side effects

Run:
    pytest tests/test_scoring_integration.py -v
    pytest -m integration
"""
from __future__ import annotations

import pytest

from tests.conftest import (
    make_college_competitor,
    make_event,
    make_event_result,
    make_pro_competitor,
    make_team,
    make_tournament,
)

pytestmark = pytest.mark.integration


# ===========================================================================
# HANDICAP SCORING
# ===========================================================================

class TestHandicapScoringIntegration:
    """End-to-end handicap scoring with start marks applied."""

    def test_handicap_subtracts_start_mark(self, db_session):
        """Competitor with start mark should have adjusted time."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p1 = make_pro_competitor(db_session, t, 'Fast Pro', 'M')
        p2 = make_pro_competitor(db_session, t, 'Slow Pro', 'M')

        event = make_event(db_session, t, 'Handicap UH',
                           event_type='pro', scoring_type='time',
                           stand_type='underhand', is_handicap=True)

        # Fast: raw 20s, no start mark (1.0 = scratch = 0.0 mark) → net 20s
        r1 = make_event_result(db_session, event, p1, competitor_type='pro',
                               result_value=20.0, handicap_factor=1.0,
                               status='completed')
        # Slow: raw 28s, 10s start mark → net 18s
        r2 = make_event_result(db_session, event, p2, competitor_type='pro',
                               result_value=28.0, handicap_factor=10.0,
                               status='completed')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        # Slow Pro wins: 28 - 10 = 18 < 20
        assert r2.final_position == 1
        assert r1.final_position == 2

    def test_handicap_none_factor_treated_as_scratch(self, db_session):
        """handicap_factor=None should be treated as 0.0 scratch."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p = make_pro_competitor(db_session, t, 'None Mark', 'M')
        event = make_event(db_session, t, 'HCP None',
                           event_type='pro', scoring_type='time',
                           stand_type='underhand', is_handicap=True)
        r = make_event_result(db_session, event, p, competitor_type='pro',
                              result_value=25.0, handicap_factor=None,
                              status='completed')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        # No mark subtracted → position 1 (only competitor)
        assert r.final_position == 1

    def test_championship_mode_ignores_handicap_factor(self, db_session):
        """When is_handicap=False, handicap_factor should be ignored."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p1 = make_pro_competitor(db_session, t, 'Champ A', 'M')
        p2 = make_pro_competitor(db_session, t, 'Champ B', 'M')

        event = make_event(db_session, t, 'Championship UH',
                           event_type='pro', scoring_type='time',
                           stand_type='underhand', is_handicap=False)

        r1 = make_event_result(db_session, event, p1, competitor_type='pro',
                               result_value=20.0, handicap_factor=10.0,
                               status='completed')
        r2 = make_event_result(db_session, event, p2, competitor_type='pro',
                               result_value=22.0, handicap_factor=1.0,
                               status='completed')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        # Championship mode: raw times only. 20 < 22 → p1 wins
        assert r1.final_position == 1
        assert r2.final_position == 2


# ===========================================================================
# HARD-HIT TIEBREAK
# ===========================================================================

class TestHardHitTiebreak:
    """Hard Hit events: primary=hits, tiebreak=time."""

    def test_hard_hit_tiebreak_on_time(self, db_session):
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p1 = make_pro_competitor(db_session, t, 'Hitter A', 'M')
        p2 = make_pro_competitor(db_session, t, 'Hitter B', 'M')

        event = make_event(db_session, t, 'Underhand Hard Hit',
                           event_type='college', gender='M',
                           scoring_type='hits', scoring_order='highest_wins',
                           stand_type='underhand')

        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'Hitter A', 'M')
        c2 = make_college_competitor(db_session, t, team, 'Hitter B', 'M')

        # Both 15 hits, but B is faster
        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               result_value=15.0, tiebreak_value=45.2,
                               status='completed')
        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               result_value=15.0, tiebreak_value=38.7,
                               status='completed')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        # Same hits → tiebreak on time → B (38.7) beats A (45.2)
        assert r2.final_position == 1
        assert r1.final_position == 2

    def test_hard_hit_different_hits_no_tiebreak(self, db_session):
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'More Hits', 'M')
        c2 = make_college_competitor(db_session, t, team, 'Less Hits', 'M')

        event = make_event(db_session, t, 'Standing Block Hard Hit',
                           event_type='college', gender='M',
                           scoring_type='hits', scoring_order='highest_wins',
                           stand_type='standing_block')

        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               result_value=18.0, tiebreak_value=55.0,
                               status='completed')
        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               result_value=14.0, tiebreak_value=30.0,
                               status='completed')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        # 18 hits > 14 hits → c1 first regardless of time
        assert r1.final_position == 1
        assert r2.final_position == 2


# ===========================================================================
# AXE THROW CUMULATIVE + THROWOFF
# ===========================================================================

class TestAxeThrowIntegration:
    """Axe throw: 3-run cumulative, highest wins, ties → throwoff."""

    def test_axe_throw_cumulative_scoring(self, db_session):
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'Axe A', 'M')
        c2 = make_college_competitor(db_session, t, team, 'Axe B', 'M')

        event = make_event(db_session, t, 'Axe Throw',
                           event_type='college', scoring_type='score',
                           scoring_order='highest_wins', stand_type='axe_throw',
                           requires_triple_runs=True)

        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               run1_value=5, run2_value=7, run3_value=3,
                               status='completed')
        r1.calculate_cumulative_score()  # result_value = 15

        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               run1_value=6, run2_value=8, run3_value=4,
                               status='completed')
        r2.calculate_cumulative_score()  # result_value = 18
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        # B (18) > A (15) → B first
        assert r2.final_position == 1
        assert r1.final_position == 2

    def test_axe_throw_tie_triggers_throwoff(self, db_session):
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'Tie X', 'M')
        c2 = make_college_competitor(db_session, t, team, 'Tie Y', 'M')

        event = make_event(db_session, t, 'Axe Throw',
                           event_type='college', scoring_type='score',
                           scoring_order='highest_wins', stand_type='axe_throw',
                           requires_triple_runs=True)

        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               run1_value=5, run2_value=5, run3_value=5,
                               status='completed')
        r1.calculate_cumulative_score()  # 15

        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               run1_value=4, run2_value=6, run3_value=5,
                               status='completed')
        r2.calculate_cumulative_score()  # 15
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        # Both 15 → throwoff pending
        assert r1.throwoff_pending is True
        assert r2.throwoff_pending is True


# ===========================================================================
# OUTLIER FLAGGING
# ===========================================================================

class TestOutlierFlaggingIntegration:
    """Outlier flagging with a realistic data set."""

    def test_outlier_flagged_with_enough_data(self, db_session):
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        event = make_event(db_session, t, 'Outlier Event',
                           event_type='pro', scoring_type='time')

        # Normal times: 20, 21, 22, 23, 24 + one outlier: 60
        pros = []
        times = [20.0, 21.0, 22.0, 23.0, 24.0, 60.0]
        for i, tm in enumerate(times):
            p = make_pro_competitor(db_session, t, f'Pro {i}', 'M')
            pros.append(p)
            make_event_result(db_session, event, p, competitor_type='pro',
                              result_value=tm, status='completed')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        results = event.results.all()
        flagged = [r for r in results if r.is_flagged]
        # The 60.0 should be flagged as an outlier
        assert len(flagged) >= 1
        assert any(r.result_value == 60.0 for r in flagged)

    def test_no_outlier_with_few_results(self, db_session):
        """Fewer than 3 results should never flag outliers."""
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        event = make_event(db_session, t, 'Small Event',
                           event_type='pro', scoring_type='time')
        p1 = make_pro_competitor(db_session, t, 'Few A', 'M')
        p2 = make_pro_competitor(db_session, t, 'Few B', 'M')
        make_event_result(db_session, event, p1, competitor_type='pro',
                          result_value=20.0, status='completed')
        make_event_result(db_session, event, p2, competitor_type='pro',
                          result_value=100.0, status='completed')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        results = event.results.all()
        assert all(r.is_flagged is False for r in results)


# ===========================================================================
# PREVIEW POSITIONS (READ-ONLY)
# ===========================================================================

class TestPreviewPositions:
    """preview_positions() should not modify the DB."""

    def test_preview_does_not_modify_db(self, db_session):
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p1 = make_pro_competitor(db_session, t, 'Preview A', 'M')
        p2 = make_pro_competitor(db_session, t, 'Preview B', 'M')

        event = make_event(db_session, t, 'Preview Event',
                           event_type='pro', scoring_type='time',
                           payouts={'1': 300, '2': 100})
        r1 = make_event_result(db_session, event, p1, competitor_type='pro',
                               result_value=20.0, status='completed')
        r2 = make_event_result(db_session, event, p2, competitor_type='pro',
                               result_value=25.0, status='completed')
        db_session.flush()

        # Preview
        preview = engine.preview_positions(event)

        # Should return ordered list
        assert len(preview) == 2
        assert preview[0]['position'] == 1
        assert preview[0]['competitor_name'] == 'Preview A'
        assert preview[0]['payout'] == 300
        assert preview[1]['position'] == 2
        assert preview[1]['payout'] == 100

        # DB should NOT be modified
        assert r1.final_position is None
        assert r2.final_position is None
        assert p1.total_earnings == 0
        assert event.is_finalized is not True


# ===========================================================================
# HIGHEST_WINS SCORING (distance events)
# ===========================================================================

class TestHighestWinsScoring:
    """Events where highest value wins (Caber Toss, Axe Throw)."""

    def test_distance_event_highest_wins(self, db_session):
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'Tosser A', 'M')
        c2 = make_college_competitor(db_session, t, team, 'Tosser B', 'M')

        event = make_event(db_session, t, 'Caber Toss',
                           event_type='college', scoring_type='distance',
                           scoring_order='highest_wins', stand_type='caber',
                           requires_dual_runs=True)

        r1 = make_event_result(db_session, event, c1, competitor_type='college',
                               run1_value=45.0, run2_value=50.0,
                               status='completed')
        r1.calculate_best_run('highest_wins')  # best=50

        r2 = make_event_result(db_session, event, c2, competitor_type='college',
                               run1_value=55.0, run2_value=48.0,
                               status='completed')
        r2.calculate_best_run('highest_wins')  # best=55
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        # 55 > 50 → c2 first
        assert r2.final_position == 1
        assert r1.final_position == 2


# ===========================================================================
# MIXED STATUS RESULTS
# ===========================================================================

class TestMixedStatusResults:
    """Verify DNF and scratched competitors are handled correctly."""

    def test_dnf_excluded_from_rankings(self, db_session):
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p1 = make_pro_competitor(db_session, t, 'Finished', 'M')
        p2 = make_pro_competitor(db_session, t, 'DNF Guy', 'M')

        event = make_event(db_session, t, 'DNF Event', event_type='pro',
                           scoring_type='time')
        r1 = make_event_result(db_session, event, p1, competitor_type='pro',
                               result_value=25.0, status='completed')
        r2 = make_event_result(db_session, event, p2, competitor_type='pro',
                               result_value=None, status='dnf')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        assert r1.final_position == 1
        assert r2.final_position is None

    def test_all_scratched_no_finalization(self, db_session):
        import services.scoring_engine as engine

        t = make_tournament(db_session)
        p = make_pro_competitor(db_session, t, 'Scratched', 'M')

        event = make_event(db_session, t, 'All Scratch', event_type='pro',
                           scoring_type='time')
        make_event_result(db_session, event, p, competitor_type='pro',
                          result_value=None, status='scratched')
        db_session.flush()

        engine.calculate_positions(event)
        db_session.flush()

        assert event.is_finalized is False
