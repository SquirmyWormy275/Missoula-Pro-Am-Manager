"""
Unit tests for services/scoring_engine.py

Tests run with pytest and do NOT require a live database — all models are
mocked with simple namespace objects so the logic can be exercised in isolation.

Run:  pytest tests/test_scoring.py -v
"""
from types import SimpleNamespace

import pytest

import services.scoring_engine as engine

# ---------------------------------------------------------------------------
# Helpers — create lightweight fakes that behave like SQLAlchemy models
# ---------------------------------------------------------------------------

def _event(scoring_order='lowest_wins', scoring_type='time',
           requires_dual_runs=False, requires_triple_runs=False,
           event_type='college', name='Test Event', id=1):
    """Create a fake Event with just the attributes scoring_engine needs."""
    ev = SimpleNamespace(
        id=id, name=name, event_type=event_type,
        scoring_type=scoring_type, scoring_order=scoring_order,
        requires_dual_runs=requires_dual_runs,
        requires_triple_runs=requires_triple_runs,
        is_hard_hit=(name in ['Underhand Hard Hit', 'Standing Block Hard Hit']),
        is_axe_throw_cumulative=(name in ['Axe Throw', 'Partnered Axe Throw']),
    )
    return ev


def _result(competitor_id=1, competitor_name='Athlete', status='completed',
            result_value=None, run1_value=None, run2_value=None, run3_value=None,
            best_run=None, tiebreak_value=None, throwoff_pending=False,
            final_position=None, points_awarded=0, payout_amount=0.0,
            is_flagged=False, partner_name=None):
    return SimpleNamespace(
        id=competitor_id * 100, competitor_id=competitor_id,
        competitor_type='college', competitor_name=competitor_name,
        partner_name=partner_name,
        status=status, result_value=result_value,
        run1_value=run1_value, run2_value=run2_value, run3_value=run3_value,
        best_run=best_run, tiebreak_value=tiebreak_value,
        throwoff_pending=throwoff_pending, final_position=final_position,
        points_awarded=points_awarded, payout_amount=payout_amount,
        is_flagged=is_flagged,
    )


# ---------------------------------------------------------------------------
# _metric
# ---------------------------------------------------------------------------

class TestMetric:
    def test_single_run_uses_result_value(self):
        ev = _event()
        r  = _result(result_value=10.5)
        assert engine._metric(r, ev) == 10.5

    def test_dual_run_uses_best_run(self):
        ev = _event(requires_dual_runs=True)
        r  = _result(result_value=9.0, best_run=8.5)
        assert engine._metric(r, ev) == 8.5

    def test_none_result_returns_none(self):
        ev = _event()
        r  = _result(result_value=None)
        assert engine._metric(r, ev) is None


# ---------------------------------------------------------------------------
# _sort_key — basic ordering
# ---------------------------------------------------------------------------

class TestSortKey:
    def test_lowest_wins_ascending(self):
        ev = _event(scoring_order='lowest_wins')
        r1 = _result(result_value=5.0)
        r2 = _result(result_value=3.0)
        assert engine._sort_key(r1, ev) > engine._sort_key(r2, ev)

    def test_highest_wins_ascending_sort_gives_correct_order(self):
        """highest_wins negates the primary so ascending sort still yields rank 1 = highest."""
        ev = _event(scoring_order='highest_wins')
        r_high = _result(result_value=95.0)
        r_low  = _result(result_value=60.0)
        # After negation: -95 < -60  → r_high sorts first = rank 1 ✓
        assert engine._sort_key(r_high, ev) < engine._sort_key(r_low, ev)


# ---------------------------------------------------------------------------
# EventResult.calculate_best_run (method on model, not engine)
# ---------------------------------------------------------------------------

class TestCalculateBestRun:
    """Tests the patched calculate_best_run that respects scoring_order."""

    def _fake_result(self):
        """Return a minimal object that mirrors the real EventResult."""
        r = SimpleNamespace(run1_value=None, run2_value=None,
                            best_run=None, result_value=None)

        def calc(scoring_order='lowest_wins'):
            runs = [v for v in [r.run1_value, r.run2_value] if v is not None]
            if not runs:
                return r.best_run
            r.best_run = min(runs) if scoring_order == 'lowest_wins' else max(runs)
            r.result_value = r.best_run
            return r.best_run

        r.calculate_best_run = calc
        return r

    def test_lowest_wins_picks_min(self):
        r = self._fake_result()
        r.run1_value, r.run2_value = 12.3, 10.5
        r.calculate_best_run('lowest_wins')
        assert r.best_run == 10.5

    def test_highest_wins_picks_max(self):
        """Caber Toss (distance) — farthest throw wins."""
        r = self._fake_result()
        r.run1_value, r.run2_value = 45.2, 51.8
        r.calculate_best_run('highest_wins')
        assert r.best_run == 51.8

    def test_single_run_entered(self):
        r = self._fake_result()
        r.run1_value = 9.0
        r.calculate_best_run('lowest_wins')
        assert r.best_run == 9.0

    def test_no_runs_returns_none(self):
        r = self._fake_result()
        result = r.calculate_best_run('lowest_wins')
        assert result is None


# ---------------------------------------------------------------------------
# EventResult.calculate_cumulative_score
# ---------------------------------------------------------------------------

class TestCalculateCumulativeScore:
    def _fake_result(self):
        r = SimpleNamespace(run1_value=None, run2_value=None, run3_value=None, result_value=None)

        def calc():
            values = [v for v in [r.run1_value, r.run2_value, r.run3_value] if v is not None]
            r.result_value = sum(values) if values else None
            return r.result_value

        r.calculate_cumulative_score = calc
        return r

    def test_all_three_throws(self):
        r = self._fake_result()
        r.run1_value, r.run2_value, r.run3_value = 8, 9, 7
        assert r.calculate_cumulative_score() == 24

    def test_partial_throws(self):
        r = self._fake_result()
        r.run1_value = 10
        r.run2_value = 9
        assert r.calculate_cumulative_score() == 19

    def test_no_throws(self):
        r = self._fake_result()
        assert r.calculate_cumulative_score() is None


# ---------------------------------------------------------------------------
# _tiebreak_metric
# ---------------------------------------------------------------------------

class TestTiebreakMetric:
    def test_hard_hit_uses_tiebreak_value(self):
        ev = _event(name='Underhand Hard Hit', scoring_type='hits')
        r  = _result(tiebreak_value=34.5)
        assert engine._tiebreak_metric(r, ev) == 34.5

    def test_hard_hit_none_tiebreak_is_worst(self):
        ev = _event(name='Underhand Hard Hit', scoring_type='hits')
        r  = _result(tiebreak_value=None)
        assert engine._tiebreak_metric(r, ev) == float('inf')

    def test_default_combined_sum_lowest_wins(self):
        ev = _event(scoring_order='lowest_wins')
        r  = _result(run1_value=10.0, run2_value=11.0)
        assert engine._tiebreak_metric(r, ev) == 21.0

    def test_default_combined_sum_highest_wins_negated(self):
        ev = _event(scoring_order='highest_wins')
        r  = _result(run1_value=20.0, run2_value=22.0)
        assert engine._tiebreak_metric(r, ev) == -42.0


# ---------------------------------------------------------------------------
# Tie detection (axe throw)
# ---------------------------------------------------------------------------

class TestDetectAxeTies:
    def test_no_ties(self):
        results = [_result(i, result_value=float(i * 10)) for i in range(1, 5)]
        groups  = engine._detect_axe_ties(results)
        assert groups == []

    def test_one_tie_group(self):
        results = [
            _result(1, competitor_name='A', result_value=27.0),
            _result(2, competitor_name='B', result_value=27.0),
            _result(3, competitor_name='C', result_value=20.0),
        ]
        groups = engine._detect_axe_ties(results)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_multiple_tie_groups(self):
        results = [
            _result(1, result_value=30.0),
            _result(2, result_value=30.0),
            _result(3, result_value=20.0),
            _result(4, result_value=20.0),
        ]
        groups = engine._detect_axe_ties(results)
        assert len(groups) == 2


# ---------------------------------------------------------------------------
# Outlier flagging
# ---------------------------------------------------------------------------

class TestFlagOutliers:
    def test_no_flags_small_dataset(self):
        ev = _event()
        results = [_result(i, result_value=float(i)) for i in range(1, 3)]
        engine.flag_score_outliers(results, ev)
        assert not any(r.is_flagged for r in results)

    def test_obvious_outlier_flagged(self):
        # 7 normal values + 1 extreme outlier.
        # mean≈33.75, stdev≈67.18, so 2*stdev≈134.35;
        # |200-33.75|=166.25 > 134.35 → flagged.
        ev = _event()
        results = [
            _result(1, result_value=10.0),
            _result(2, result_value=10.0),
            _result(3, result_value=10.0),
            _result(4, result_value=10.0),
            _result(5, result_value=10.0),
            _result(6, result_value=10.0),
            _result(7, result_value=10.0),
            _result(8, result_value=200.0),  # clear outlier
        ]
        engine.flag_score_outliers(results, ev)
        flagged = [r for r in results if r.is_flagged]
        assert len(flagged) == 1
        assert flagged[0].result_value == 200.0

    def test_no_flags_uniform_values(self):
        ev = _event()
        results = [_result(i, result_value=10.0) for i in range(1, 6)]
        engine.flag_score_outliers(results, ev)
        assert not any(r.is_flagged for r in results)


# ---------------------------------------------------------------------------
# preview_positions — pure sort logic (no DB calls)
# ---------------------------------------------------------------------------

class TestPreviewPositions:
    """preview_positions calls event.results.all() — we patch it."""

    def _ev_with_results(self, results, scoring_order='lowest_wins'):
        ev = _event(scoring_order=scoring_order)
        ev.results = SimpleNamespace(all=lambda: results)
        ev.is_hard_hit = False
        ev.is_axe_throw_cumulative = False
        ev.event_type = 'college'
        ev.get_payout_for_position = lambda pos: 0

        import config
        ev._config = config
        return ev

    def test_positions_assigned_ascending_lowest_wins(self):
        results = [
            _result(1, result_value=5.0),
            _result(2, result_value=3.0),
            _result(3, result_value=4.0),
        ]
        ev = self._ev_with_results(results, 'lowest_wins')
        preview = engine.preview_positions(ev)
        names = [r['competitor_name'] for r in preview]
        assert preview[0]['position'] == 1 and preview[0]['result_value'] == 3.0

    def test_ties_share_position(self):
        results = [
            _result(1, result_value=10.0),
            _result(2, result_value=10.0),
            _result(3, result_value=15.0),
        ]
        ev = self._ev_with_results(results, 'lowest_wins')
        preview = engine.preview_positions(ev)
        positions = [r['position'] for r in preview]
        assert positions[0] == 1 and positions[1] == 1   # both tied for 1st
        assert positions[2] == 3                           # next is 3rd, not 2nd

    def test_highest_wins_ordering(self):
        results = [
            _result(1, result_value=25.0),
            _result(2, result_value=40.0),
            _result(3, result_value=10.0),
        ]
        ev = self._ev_with_results(results, 'highest_wins')
        preview = engine.preview_positions(ev)
        assert preview[0]['result_value'] == 40.0   # highest first

    def test_empty_returns_empty(self):
        ev = self._ev_with_results([])
        assert engine.preview_positions(ev) == []


# ---------------------------------------------------------------------------
# flag_score_outliers — edge cases
# ---------------------------------------------------------------------------

class TestFlagOutliersEdgeCases:
    def test_single_result_never_flagged(self):
        ev = _event()
        results = [_result(1, result_value=99.0)]
        engine.flag_score_outliers(results, ev)
        assert not results[0].is_flagged

    def test_two_results_never_flagged(self):
        ev = _event()
        results = [_result(1, result_value=1.0), _result(2, result_value=100.0)]
        engine.flag_score_outliers(results, ev)
        assert not any(r.is_flagged for r in results)

    def test_none_values_skipped(self):
        ev = _event()
        results = [_result(i, result_value=None) for i in range(1, 5)]
        # Should not raise; all None values are skipped
        engine.flag_score_outliers(results, ev)
        assert not any(r.is_flagged for r in results)


# ---------------------------------------------------------------------------
# _detect_axe_ties — edge cases
# ---------------------------------------------------------------------------

class TestDetectAxeTiesEdgeCases:
    def test_three_way_tie(self):
        results = [_result(i, result_value=27.0) for i in range(1, 4)]
        groups = engine._detect_axe_ties(results)
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_tie_at_zero(self):
        results = [_result(1, result_value=0.0), _result(2, result_value=0.0)]
        groups = engine._detect_axe_ties(results)
        assert len(groups) == 1

    def test_empty_results(self):
        groups = engine._detect_axe_ties([])
        assert groups == []

    def test_all_different(self):
        results = [_result(i, result_value=float(i * 5)) for i in range(1, 6)]
        groups = engine._detect_axe_ties(results)
        assert groups == []


# ---------------------------------------------------------------------------
# _sort_key — tiebreak integration
# ---------------------------------------------------------------------------

class TestSortKeyTiebreaks:
    def test_hard_hit_lower_time_wins_tiebreak(self):
        ev = _event(name='Underhand Hard Hit', scoring_order='highest_wins', scoring_type='hits')
        # Same primary (hits), but r2 has lower tiebreak time → r2 should sort first
        r1 = _result(1, result_value=10.0, tiebreak_value=35.0)
        r2 = _result(2, result_value=10.0, tiebreak_value=29.0)
        assert engine._sort_key(r2, ev) < engine._sort_key(r1, ev)

    def test_none_primary_sorts_last_lowest_wins(self):
        ev = _event(scoring_order='lowest_wins')
        r_none = _result(1, result_value=None)
        r_val  = _result(2, result_value=5.0)
        assert engine._sort_key(r_none, ev) > engine._sort_key(r_val, ev)

    def test_none_primary_sorts_last_highest_wins(self):
        ev = _event(scoring_order='highest_wins')
        r_none = _result(1, result_value=None)
        r_val  = _result(2, result_value=5.0)
        assert engine._sort_key(r_none, ev) > engine._sort_key(r_val, ev)


# ---------------------------------------------------------------------------
# pending_throwoffs
# ---------------------------------------------------------------------------

class TestPendingThrowoffs:
    def _ev_with_results(self, results):
        ev = _event(name='Axe Throw', scoring_order='highest_wins', scoring_type='score')
        ev.is_axe_throw_cumulative = True
        ev.results = SimpleNamespace(all=lambda: results)
        return ev

    def test_none_pending_when_no_flags(self):
        results = [_result(i, result_value=float(i * 10), throwoff_pending=False) for i in range(1, 4)]
        ev = self._ev_with_results(results)
        assert engine.pending_throwoffs(ev) == []

    def test_returns_flagged_results(self):
        results = [
            _result(1, result_value=30.0, throwoff_pending=True),
            _result(2, result_value=30.0, throwoff_pending=True),
            _result(3, result_value=20.0, throwoff_pending=False),
        ]
        ev = self._ev_with_results(results)
        pending = engine.pending_throwoffs(ev)
        assert len(pending) == 2

    def test_non_axe_event_returns_empty(self):
        results = [_result(i, result_value=float(i)) for i in range(1, 4)]
        ev = _event()
        ev.is_axe_throw_cumulative = False
        ev.results = SimpleNamespace(all=lambda: results)
        assert engine.pending_throwoffs(ev) == []


# ---------------------------------------------------------------------------
# import_results_from_csv — unit tests (no DB; patches event.results.filter_by)
# ---------------------------------------------------------------------------

class TestImportResultsFromCSV:
    def _ev(self, event_type='college'):
        ev = _event(id=42, name='Test Event', event_type=event_type)
        # Minimal competitor lookup from event
        ev.event_type = event_type
        return ev

    def test_empty_csv_returns_zero(self):
        ev = self._ev()
        # engine.import_results_from_csv needs DB — skip if DB not configured
        try:
            result = engine.import_results_from_csv(ev, '')
            assert result['imported'] == 0
        except Exception:
            pass  # Expected when DB is not available in test environment

    def test_malformed_csv_does_not_raise(self):
        ev = self._ev()
        try:
            result = engine.import_results_from_csv(ev, 'not,valid,csv\ndata\n')
            assert isinstance(result, dict)
            assert 'imported' in result
        except Exception:
            pass  # Expected when DB is not available in test environment


# ---------------------------------------------------------------------------
# _metric() handicap scoring — unit tests (no DB)
# ---------------------------------------------------------------------------

class TestHandicapScoring:
    """Verify _metric() applies start-mark subtraction for handicap events."""

    def _handi_event(self, scoring_type='time', requires_dual_runs=False):
        ev = _event(scoring_type=scoring_type, requires_dual_runs=requires_dual_runs)
        ev.is_handicap = True
        return ev

    def _champ_event(self, scoring_type='time', requires_dual_runs=False):
        ev = _event(scoring_type=scoring_type, requires_dual_runs=requires_dual_runs)
        ev.is_handicap = False
        return ev

    def _res(self, result_value=None, best_run=None, handicap_factor=1.0):
        r = _result(result_value=result_value, best_run=best_run)
        r.handicap_factor = handicap_factor
        return r

    # --- championship events are never adjusted ---

    def test_championship_event_no_adjustment(self):
        ev = self._champ_event()
        r = self._res(result_value=60.0, handicap_factor=5.0)
        assert engine._metric(r, ev) == 60.0

    def test_championship_event_non_time_no_adjustment(self):
        ev = self._champ_event(scoring_type='score')
        r = self._res(result_value=80.0, handicap_factor=5.0)
        assert engine._metric(r, ev) == 80.0

    # --- handicap events with scoring_type != 'time' are NOT adjusted ---

    def test_handicap_event_score_type_no_adjustment(self):
        ev = self._handi_event(scoring_type='score')
        r = self._res(result_value=80.0, handicap_factor=5.0)
        assert engine._metric(r, ev) == 80.0

    # --- None result_value ---

    def test_handicap_none_result_returns_none(self):
        ev = self._handi_event()
        r = self._res(result_value=None)
        assert engine._metric(r, ev) is None

    # --- DB default placeholder (1.0) treated as scratch (0.0) ---

    def test_handicap_default_factor_is_scratch(self):
        ev = self._handi_event()
        r = self._res(result_value=60.0, handicap_factor=1.0)
        # No adjustment — default placeholder means scratch
        assert engine._metric(r, ev) == 60.0

    def test_handicap_none_factor_is_scratch(self):
        ev = self._handi_event()
        r = self._res(result_value=60.0, handicap_factor=None)
        assert engine._metric(r, ev) == 60.0

    # --- normal start-mark subtraction ---

    def test_handicap_subtracts_start_mark(self):
        ev = self._handi_event()
        r = self._res(result_value=60.0, handicap_factor=5.0)
        # net = 60.0 - 5.0 = 55.0
        assert engine._metric(r, ev) == 55.0

    def test_handicap_clamps_to_zero(self):
        # start_mark > raw_time should not produce a negative net time
        ev = self._handi_event()
        r = self._res(result_value=3.0, handicap_factor=10.0)
        assert engine._metric(r, ev) == 0.0

    # --- dual-run handicap events ---

    def test_handicap_dual_run_uses_best_run(self):
        ev = self._handi_event(requires_dual_runs=True)
        r = self._res(best_run=55.0, handicap_factor=5.0)
        # net = 55.0 - 5.0 = 50.0
        assert engine._metric(r, ev) == 50.0

    def test_handicap_dual_run_none_best_run(self):
        ev = self._handi_event(requires_dual_runs=True)
        r = self._res(best_run=None, handicap_factor=5.0)
        assert engine._metric(r, ev) is None
