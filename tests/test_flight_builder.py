"""
Unit tests for services/flight_builder.py pure helper functions.

No database required — only the pure algorithmic helpers are tested here.
Functions that require DB (build_pro_flights, get_flight_summary, etc.) are excluded.

Run:  pytest tests/test_flight_builder.py -v
"""
import json
from types import SimpleNamespace

import pytest

from services.flight_builder import (
    _CONFLICTING_STANDS,
    _STAND_CONFLICT_GAP,
    EVENT_SPACING_TIERS,
    MIN_HEAT_SPACING,
    TARGET_HEAT_SPACING,
    _calculate_heat_score,
    _get_partnered_axe_qualifier_pairs,
    _get_spacing,
    _score_ordering,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(stand_type='underhand', id=1, name='Underhand'):
    return SimpleNamespace(stand_type=stand_type, id=id, name=name)


def _heat_data(event, competitors, heat_number=1):
    heat = SimpleNamespace(event_id=event.id, heat_number=heat_number, run_number=1)
    return {'heat': heat, 'event': event, 'competitors': set(competitors)}


def _axe_event(payouts_json='{}'):
    ev = SimpleNamespace(payouts=payouts_json)
    return ev


# ---------------------------------------------------------------------------
# _get_spacing
# ---------------------------------------------------------------------------

class TestGetSpacing:
    def test_springboard_tier(self):
        ev = _event(stand_type='springboard')
        assert _get_spacing(ev) == EVENT_SPACING_TIERS['springboard']

    def test_saw_hand_tier(self):
        ev = _event(stand_type='saw_hand')
        assert _get_spacing(ev) == EVENT_SPACING_TIERS['saw_hand']

    def test_underhand_tier(self):
        ev = _event(stand_type='underhand')
        assert _get_spacing(ev) == EVENT_SPACING_TIERS['underhand']

    def test_unknown_stand_type_uses_global_defaults(self):
        ev = _event(stand_type='birling')
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == MIN_HEAT_SPACING
        assert target_sp == TARGET_HEAT_SPACING

    def test_none_event_uses_global_defaults(self):
        min_sp, target_sp = _get_spacing(None)
        assert min_sp == MIN_HEAT_SPACING
        assert target_sp == TARGET_HEAT_SPACING

    def test_springboard_stricter_than_default(self):
        sp_min, _ = _get_spacing(_event(stand_type='springboard'))
        default_min, _ = _get_spacing(_event(stand_type='birling'))
        assert sp_min > default_min


# ---------------------------------------------------------------------------
# _calculate_heat_score
# ---------------------------------------------------------------------------

class TestCalculateHeatScore:
    def test_new_competitors_score_highest(self):
        """Competitors never seen before get base score of 1000 (plus any bonuses)."""
        ev = _event(stand_type='underhand')
        score = _calculate_heat_score({1, 2}, {}, 0, ev, {}, 8, {})
        # Base score is 1000; recency bonus (+30) may also apply on first appearance
        assert score >= 1000.0

    def test_empty_heat_gets_fixed_score(self):
        ev = _event(stand_type='underhand')
        score = _calculate_heat_score(set(), {}, 5, ev, {}, 8, {})
        assert score == 100.0

    def test_adequate_spacing_earns_positive_score(self):
        ev = _event(stand_type='underhand')
        # Competitor 1 last appeared at position 0, now at position 6 (spacing=6 > min=4)
        score = _calculate_heat_score({1}, {1: 0}, 6, ev, {}, 8, {})
        assert score > 0

    def test_insufficient_spacing_still_positive_but_below_threshold(self):
        ev = _event(stand_type='underhand')
        # Spacing = 1, min=4 → penalty applied
        score = _calculate_heat_score({1}, {1: 0}, 1, ev, {}, 8, {})
        # Should be >= 0 and less than the score for adequate spacing
        adequate_score = _calculate_heat_score({1}, {1: 0}, 6, ev, {}, 8, {})
        assert score < adequate_score
        assert score >= 0

    def test_springboard_opener_bonus_at_flight_start(self):
        ev = _event(stand_type='springboard')
        # Position 0 is the start of the first flight block (8-heat flights)
        score_at_start = _calculate_heat_score({1, 2}, {}, 0, ev, {}, 8, {})
        score_elsewhere = _calculate_heat_score({1, 2}, {}, 3, ev, {}, 8, {})
        assert score_at_start > score_elsewhere  # +500 opener bonus

    def test_hot_saw_closer_bonus_at_flight_end(self):
        ev = _event(stand_type='hot_saw')
        # Position 7 is the last slot in an 8-heat flight (index 7, (7+1) % 8 == 0)
        score_at_end = _calculate_heat_score({1}, {}, 7, ev, {}, 8, {})
        score_elsewhere = _calculate_heat_score({1}, {}, 3, ev, {}, 8, {})
        assert score_at_end > score_elsewhere  # +300 closer bonus

    def test_stand_conflict_returns_minus_one(self):
        """Cookie Stack blocked when Standing Block was recently placed."""
        cs_event = _event(stand_type='cookie_stack')
        # Standing block was at position 0, current is 2 — gap=2 < _STAND_CONFLICT_GAP
        stand_positions = {'standing_block': 0}
        score = _calculate_heat_score({1}, {}, 2, cs_event, stand_positions, 8, {})
        assert score == -1.0

    def test_standing_block_blocked_by_cookie_stack(self):
        sb_event = _event(stand_type='standing_block')
        stand_positions = {'cookie_stack': 0}
        score = _calculate_heat_score({1}, {}, 2, sb_event, stand_positions, 8, {})
        assert score == -1.0

    def test_stand_conflict_clears_after_gap(self):
        """After enough heats, the conflict penalty no longer applies."""
        cs_event = _event(stand_type='cookie_stack')
        stand_positions = {'standing_block': 0}
        current_pos = _STAND_CONFLICT_GAP + 1
        score = _calculate_heat_score({1}, {}, current_pos, cs_event, stand_positions, 8, {})
        assert score != -1.0

    def test_disabled_stand_conflict_check(self):
        """Passing stand_type_last_position=None disables the conflict check."""
        cs_event = _event(stand_type='cookie_stack')
        # Would normally be blocked but None disables it
        score = _calculate_heat_score({1}, {}, 2, cs_event, None, 8, {})
        assert score != -1.0

    def test_event_recency_bonus_first_appearance_in_block(self):
        ev = _event(stand_type='underhand', id=42)
        # Use a non-empty competitor set so the early-return for empty heats is bypassed.
        # Competitor 99 has never appeared, so competitor_count=0 → base=1000.
        # First call: event 42 not in event_last_block → recency bonus +30 → 1030.
        score_first = _calculate_heat_score({99}, {}, 0, ev, {}, 8, {})
        # Second call: event 42 already appeared in block 0 (same block) → no bonus → 1000.
        score_repeat = _calculate_heat_score({99}, {}, 1, ev, {}, 8, {42: 0})
        assert score_first > score_repeat  # +30 recency bonus difference


# ---------------------------------------------------------------------------
# _score_ordering
# ---------------------------------------------------------------------------

class TestScoreOrdering:
    def test_empty_ordering_scores_zero(self):
        assert _score_ordering([], 8) == 0.0

    def test_single_heat_no_prior_appearances(self):
        ev = _event(stand_type='underhand', id=1)
        ordered = [_heat_data(ev, [10, 11])]
        score = _score_ordering(ordered, 8)
        # No prior appearances, no spacing rewards yet, only variety bonus
        assert isinstance(score, float)

    def test_well_spaced_competitors_score_higher(self):
        ev = _event(stand_type='underhand', id=1)
        # Well-spaced: competitor 1 appears at positions 0 and 5 only (spacing=5, target=5).
        # Intermediate positions 1-4 have different competitors.
        well_spaced = (
            [_heat_data(ev, [1])]
            + [_heat_data(ev, [10 + i]) for i in range(4)]
            + [_heat_data(ev, [1])]
        )
        score_good = _score_ordering(well_spaced, 8)

        # Poorly-spaced: competitor 1 in every heat (spacing=1, well below min=4 → heavy penalty).
        poorly_spaced = [_heat_data(ev, [1]) for _ in range(4)]
        score_bad = _score_ordering(poorly_spaced, 8)

        assert score_good > score_bad

    def test_different_events_give_variety_bonus(self):
        ev1 = _event(stand_type='underhand', id=1)
        ev2 = _event(stand_type='springboard', id=2)
        mixed = [_heat_data(ev1, []), _heat_data(ev2, [])]
        score_mixed = _score_ordering(mixed, 8)

        same = [_heat_data(ev1, []), _heat_data(ev1, [])]
        score_same = _score_ordering(same, 8)

        # Mixed events earn more variety bonuses
        assert score_mixed >= score_same


# ---------------------------------------------------------------------------
# _get_partnered_axe_qualifier_pairs
# ---------------------------------------------------------------------------

class TestGetPartneredAxeQualifierPairs:
    def _make_pair(self, c1_id, c2_id, score=None):
        return {
            'competitor1': {'id': c1_id, 'name': f'Comp{c1_id}'},
            'competitor2': {'id': c2_id, 'name': f'Comp{c2_id}'},
            'prelim_score': score,
        }

    def test_returns_empty_for_invalid_json(self):
        ev = _axe_event(payouts_json='not-json')
        result = _get_partnered_axe_qualifier_pairs(ev, 4)
        assert result == []

    def test_returns_empty_for_no_pairs(self):
        ev = _axe_event(payouts_json='{}')
        result = _get_partnered_axe_qualifier_pairs(ev, 4)
        assert result == []

    def test_returns_top_n_from_prelim_results(self):
        pairs = [self._make_pair(i * 2 - 1, i * 2, score=10 - i) for i in range(1, 7)]
        state = {'prelim_results': pairs}
        ev = _axe_event(payouts_json=json.dumps(state))
        result = _get_partnered_axe_qualifier_pairs(ev, 4)
        assert len(result) == 4

    def test_uses_pairs_when_no_prelim_results(self):
        """Falls back to 'pairs' key sorted by prelim_score."""
        pairs = [self._make_pair(1, 2, score=20), self._make_pair(3, 4, score=30)]
        state = {'pairs': pairs}
        ev = _axe_event(payouts_json=json.dumps(state))
        result = _get_partnered_axe_qualifier_pairs(ev, 4)
        # Pair (3,4) has higher prelim_score so should be first
        assert result[0]['competitor1']['id'] == 3

    def test_ignores_pairs_missing_competitor_ids(self):
        bad_pair = {'competitor1': {'name': 'X'}, 'competitor2': {'id': 2, 'name': 'Y'}}
        good_pair = self._make_pair(1, 2, score=15)
        state = {'prelim_results': [bad_pair, good_pair]}
        ev = _axe_event(payouts_json=json.dumps(state))
        result = _get_partnered_axe_qualifier_pairs(ev, 4)
        assert len(result) == 1
        assert result[0]['competitor1']['id'] == 1

    def test_count_limits_results(self):
        pairs = [self._make_pair(i * 2 - 1, i * 2, score=i) for i in range(1, 10)]
        state = {'prelim_results': pairs}
        ev = _axe_event(payouts_json=json.dumps(state))
        result = _get_partnered_axe_qualifier_pairs(ev, 3)
        assert len(result) == 3
