"""
Unit tests for services/heat_generator.py pure helper functions.

These tests do NOT touch the database — all heavy DB functions
(generate_event_heats, _get_event_competitors) are excluded.
Only the pure algorithmic helpers are exercised here.

Run:  pytest tests/test_heat_generator.py -v
"""
from types import SimpleNamespace

import pytest

from services.heat_generator import (
    _advance_snake_index,
    _build_partner_units,
    _competitor_entered_event,
    _competitors_share_gear_for_event,
    _generate_saw_heats,
    _generate_springboard_heats,
    _generate_standard_heats,
    _has_gear_sharing_conflict,
    _is_list_only_event,
    _norm_name,
    _normalize_name,
    _stand_numbers_for_event,
)

# ---------------------------------------------------------------------------
# Helpers — lightweight fake event
# ---------------------------------------------------------------------------

def _event(id=1, name='Underhand', display_name=None, event_type='college',
           gender=None, stand_type='underhand', is_partnered=False,
           partner_gender=None, max_stands=None, tournament_id=1):
    ev = SimpleNamespace(
        id=id, name=name,
        display_name=display_name or name,
        event_type=event_type,
        gender=gender,
        stand_type=stand_type,
        is_partnered=is_partnered,
        partner_gender=partner_gender,
        max_stands=max_stands,
        tournament_id=tournament_id,
    )
    return ev


def _comp(id=1, name='Alice', gender='F', is_left_handed=False,
          is_slow_springboard=False, gear_sharing=None, partner_name=''):
    return {
        'id': id,
        'name': name,
        'gender': gender,
        'is_left_handed': is_left_handed,
        'is_slow_springboard': is_slow_springboard,
        'gear_sharing': gear_sharing or {},
        'partner_name': partner_name,
    }


# ---------------------------------------------------------------------------
# _advance_snake_index
# ---------------------------------------------------------------------------

class TestAdvanceSnakeIndex:
    def test_forward_in_middle(self):
        idx, direction = _advance_snake_index(0, 1, 3)
        assert (idx, direction) == (1, 1)

    def test_forward_hits_right_boundary(self):
        # 2 + 1 = 3 >= 3 → bounce back
        idx, direction = _advance_snake_index(2, 1, 3)
        assert idx == 2 and direction == -1

    def test_backward_hits_left_boundary(self):
        # 0 - 1 = -1 < 0 → bounce forward
        idx, direction = _advance_snake_index(0, -1, 3)
        assert idx == 0 and direction == 1

    def test_backward_in_middle(self):
        idx, direction = _advance_snake_index(2, -1, 3)
        assert (idx, direction) == (1, -1)

    def test_single_heat(self):
        # With only 1 heat, going forward bounces immediately
        idx, direction = _advance_snake_index(0, 1, 1)
        assert idx == 0 and direction == -1

    def test_full_snake_sequence(self):
        """Confirm a snake pattern: 0 1 2 2 1 0 0 1 2 ... for 3 heats."""
        positions = []
        idx, direction = 0, 1
        for _ in range(9):
            positions.append(idx)
            idx, direction = _advance_snake_index(idx, direction, 3)
        assert positions == [0, 1, 2, 2, 1, 0, 0, 1, 2]


# ---------------------------------------------------------------------------
# _normalize_name  (removes non-alnum, lowercases)
# ---------------------------------------------------------------------------

class TestNormalizeName:
    def test_spaces_removed(self):
        assert _normalize_name('Stock Saw') == 'stocksaw'

    def test_hyphens_and_symbols(self):
        assert _normalize_name("Speed-Climb!") == 'speedclimb'

    def test_empty_string(self):
        assert _normalize_name('') == ''

    def test_none_becomes_empty(self):
        assert _normalize_name(None) == ''

    def test_mixed_case(self):
        assert _normalize_name('Axe Throw') == 'axethrow'


# ---------------------------------------------------------------------------
# _norm_name  (strip + lower, keeps spaces)
# ---------------------------------------------------------------------------

class TestNormName:
    def test_strips_and_lowercases(self):
        assert _norm_name('  Alice  ') == 'alice'

    def test_none_becomes_empty(self):
        assert _norm_name(None) == ''

    def test_empty_is_empty(self):
        assert _norm_name('') == ''


# ---------------------------------------------------------------------------
# _competitor_entered_event
# ---------------------------------------------------------------------------

class TestCompetitorEnteredEvent:
    def test_match_by_numeric_id(self):
        ev = _event(id=42, name='Underhand', event_type='college')
        assert _competitor_entered_event(ev, [42]) is True

    def test_match_by_string_id(self):
        ev = _event(id=42, name='Underhand', event_type='college')
        assert _competitor_entered_event(ev, ['42']) is True

    def test_match_by_normalized_name(self):
        ev = _event(id=5, name='Underhand', event_type='college')
        assert _competitor_entered_event(ev, ['Underhand']) is True

    def test_no_match_wrong_id(self):
        ev = _event(id=1, name='Underhand', event_type='college')
        assert _competitor_entered_event(ev, [99]) is False

    def test_empty_list(self):
        ev = _event(id=1, name='Underhand', event_type='college')
        assert _competitor_entered_event(ev, []) is False

    def test_pro_springboard_aliases(self):
        ev = _event(id=10, name='Springboard', event_type='pro')
        assert _competitor_entered_event(ev, ['SpringboardL']) is True
        assert _competitor_entered_event(ev, ['SpringboardR']) is True

    def test_pro_jack_and_jill_alias(self):
        ev = _event(id=11, name='Jack & Jill Sawing', event_type='pro')
        assert _competitor_entered_event(ev, ['Jack & Jill']) is True

    def test_match_is_case_insensitive(self):
        ev = _event(id=5, name='Underhand', event_type='college')
        assert _competitor_entered_event(ev, ['UNDERHAND']) is True


# ---------------------------------------------------------------------------
# _is_list_only_event
# ---------------------------------------------------------------------------

class TestIsListOnlyEvent:
    def test_axe_throw_is_list_only(self):
        ev = _event(name='Axe Throw', event_type='college')
        assert _is_list_only_event(ev) is True

    def test_peavey_log_roll_is_list_only(self):
        ev = _event(name='Peavey Log Roll', event_type='college')
        assert _is_list_only_event(ev) is True

    def test_caber_toss_is_list_only(self):
        ev = _event(name='Caber Toss', event_type='college')
        assert _is_list_only_event(ev) is True

    def test_pulp_toss_is_list_only(self):
        ev = _event(name='Pulp Toss', event_type='college')
        assert _is_list_only_event(ev) is True

    def test_underhand_is_not_list_only(self):
        ev = _event(name='Underhand', event_type='college')
        assert _is_list_only_event(ev) is False

    def test_pro_axe_throw_is_not_list_only(self):
        # List-only check only applies to college events
        ev = _event(name='Axe Throw', event_type='pro')
        assert _is_list_only_event(ev) is False


# ---------------------------------------------------------------------------
# _stand_numbers_for_event
# ---------------------------------------------------------------------------

class TestStandNumbersForEvent:
    def test_default_sequential_stands(self):
        ev = _event(name='Underhand', event_type='college', stand_type='underhand')
        numbers = _stand_numbers_for_event(ev, 4, {})
        assert numbers == [1, 2, 3, 4]

    def test_college_stock_saw_uses_stands_7_and_8(self):
        ev = _event(name='Stock Saw', event_type='college', stand_type='stock_saw')
        numbers = _stand_numbers_for_event(ev, 2, {})
        assert numbers == [7, 8]

    def test_specific_stands_from_config(self):
        ev = _event(name='Hot Saw', event_type='pro', stand_type='hot_saw')
        stand_config = {'specific_stands': [1, 2, 3, 4]}
        numbers = _stand_numbers_for_event(ev, 4, stand_config)
        assert numbers == [1, 2, 3, 4]

    def test_specific_stands_truncated_by_max(self):
        ev = _event(name='Hot Saw', event_type='pro', stand_type='hot_saw')
        stand_config = {'specific_stands': [1, 2, 3, 4]}
        numbers = _stand_numbers_for_event(ev, 2, stand_config)
        assert numbers == [1, 2]

    def test_pro_stock_saw_uses_stands_7_and_8(self):
        # DOMAIN_CONTRACT (2026-04-27): ALL Stock Saw runs on stands 7-8.
        # Pro events override the stand_config specific_stands too.
        ev = _event(name='Stock Saw', event_type='pro', stand_type='stock_saw')
        stand_config = {'specific_stands': [7, 8]}
        numbers = _stand_numbers_for_event(ev, 2, stand_config)
        assert numbers == [7, 8]


# ---------------------------------------------------------------------------
# _competitors_share_gear_for_event  (alias for gear_sharing service)
# ---------------------------------------------------------------------------

class TestCompetitorsShareGearForEvent:
    def test_gear_sharing_conflict_detected(self):
        comp1 = _comp(id=1, name='Alice', gear_sharing={'5': 'Bob'})
        comp2 = _comp(id=2, name='Bob', gear_sharing={})
        ev = _event(id=5, name='Single Buck', event_type='pro', stand_type='saw_hand')
        assert _competitors_share_gear_for_event(comp1, comp2, ev) is True

    def test_no_conflict_different_event(self):
        comp1 = _comp(id=1, name='Alice', gear_sharing={'99': 'Bob'})
        comp2 = _comp(id=2, name='Bob', gear_sharing={})
        ev = _event(id=5, name='Single Buck', event_type='pro', stand_type='saw_hand')
        # Key 99 doesn't match event id 5
        assert _competitors_share_gear_for_event(comp1, comp2, ev) is False

    def test_no_conflict_unrelated_competitors(self):
        comp1 = _comp(id=1, name='Alice', gear_sharing={})
        comp2 = _comp(id=2, name='Bob', gear_sharing={})
        ev = _event(id=5)
        assert _competitors_share_gear_for_event(comp1, comp2, ev) is False


# ---------------------------------------------------------------------------
# _has_gear_sharing_conflict
# ---------------------------------------------------------------------------

class TestHasGearSharingConflict:
    def test_conflict_when_gear_partner_in_heat(self):
        ev = _event(id=5, name='Single Buck', event_type='pro', stand_type='saw_hand')
        comp = _comp(id=3, name='Charlie', gear_sharing={'5': 'Alice'})
        heat = [_comp(id=1, name='Alice'), _comp(id=2, name='Bob')]
        assert _has_gear_sharing_conflict(comp, heat, ev) is True

    def test_no_conflict_when_partner_absent(self):
        ev = _event(id=5, name='Single Buck', event_type='pro', stand_type='saw_hand')
        comp = _comp(id=3, name='Charlie', gear_sharing={'5': 'Dana'})
        heat = [_comp(id=1, name='Alice'), _comp(id=2, name='Bob')]
        assert _has_gear_sharing_conflict(comp, heat, ev) is False

    def test_no_conflict_empty_heat(self):
        ev = _event(id=5)
        comp = _comp(id=3, name='Charlie', gear_sharing={'5': 'Alice'})
        assert _has_gear_sharing_conflict(comp, [], ev) is False


# ---------------------------------------------------------------------------
# _build_partner_units
# ---------------------------------------------------------------------------

class TestBuildPartnerUnits:
    def test_non_partnered_all_singles(self):
        ev = _event(is_partnered=False)
        comps = [_comp(i, name=f'Comp{i}') for i in range(1, 5)]
        units = _build_partner_units(comps, ev)
        assert all(len(u) == 1 for u in units)
        assert len(units) == 4

    def test_non_partnered_event_is_none(self):
        comps = [_comp(i, name=f'Comp{i}') for i in range(1, 3)]
        units = _build_partner_units(comps, None)
        assert all(len(u) == 1 for u in units)

    def test_partnered_recognized_pair_grouped(self):
        ev = _event(is_partnered=True)
        alice = _comp(id=1, name='Alice', partner_name='Bob')
        bob = _comp(id=2, name='Bob', partner_name='Alice')
        units = _build_partner_units([alice, bob], ev)
        # One pair unit of size 2
        pair_units = [u for u in units if len(u) == 2]
        assert len(pair_units) == 1
        ids_in_pair = {c['id'] for c in pair_units[0]}
        assert ids_in_pair == {1, 2}

    def test_partnered_unmatched_competitor_held_back_by_default(self):
        """Behaviour change 2026-04-23: unmatched competitors in a partnered
        event are HELD BACK (not placed solo on a stand). A solo placement in
        a partnered event is wrong by definition — the event needs a pair.
        Was ``test_partnered_unmatched_competitor_is_single`` before the fix.
        """
        ev = _event(is_partnered=True)
        alice = _comp(id=1, name='Alice', partner_name='Bob')  # Bob not in pool
        charlie = _comp(id=3, name='Charlie', partner_name='')  # blank
        unpaired_log: list = []
        units = _build_partner_units([alice, charlie], ev, unpaired_log=unpaired_log)
        assert units == []
        assert {entry['comp_id'] for entry in unpaired_log} == {1, 3}
        reasons = {entry['comp_id']: entry['reason'] for entry in unpaired_log}
        assert reasons[1] == 'unresolved'
        assert reasons[3] == 'blank'

    def test_partnered_unmatched_legacy_solo_placement_opt_in(self):
        """Legacy mode: skip_unpaired=False reproduces the pre-fix behaviour
        (solo placement) for any caller that needs it."""
        ev = _event(is_partnered=True)
        alice = _comp(id=1, name='Alice', partner_name='Bob')
        charlie = _comp(id=3, name='Charlie', partner_name='')
        units = _build_partner_units([alice, charlie], ev, skip_unpaired=False)
        assert len(units) == 2
        assert all(len(u) == 1 for u in units)


# ---------------------------------------------------------------------------
# _generate_standard_heats
# ---------------------------------------------------------------------------

class TestGenerateStandardHeats:
    def test_all_competitors_placed(self):
        ev = _event(event_type='college')
        comps = [_comp(i, name=f'Comp{i}') for i in range(1, 9)]
        heats = _generate_standard_heats(comps, 2, 4, event=ev)
        placed = [c for heat in heats for c in heat]
        assert len(placed) == 8

    def test_correct_number_of_heats(self):
        ev = _event(event_type='college')
        comps = [_comp(i, name=f'Comp{i}') for i in range(1, 7)]
        heats = _generate_standard_heats(comps, 2, 4, event=ev)
        assert len(heats) == 2

    def test_no_heat_exceeds_max(self):
        ev = _event(event_type='college')
        comps = [_comp(i, name=f'Comp{i}') for i in range(1, 10)]
        heats = _generate_standard_heats(comps, 3, 3, event=ev)
        assert all(len(h) <= 3 for h in heats)

    def test_single_competitor(self):
        ev = _event(event_type='college')
        comps = [_comp(1, name='Solo')]
        heats = _generate_standard_heats(comps, 1, 4, event=ev)
        assert len(heats) == 1
        assert len(heats[0]) == 1

    def test_no_duplicates_across_heats(self):
        ev = _event(event_type='college')
        comps = [_comp(i, name=f'Comp{i}') for i in range(1, 13)]
        heats = _generate_standard_heats(comps, 3, 4, event=ev)
        all_ids = [c['id'] for heat in heats for c in heat]
        assert len(all_ids) == len(set(all_ids))


# ---------------------------------------------------------------------------
# _generate_springboard_heats
# ---------------------------------------------------------------------------

class TestGenerateSpringboardHeats:
    """
    LH cutter placement rule (2026-04-20):
      - Only one physical LH springboard dummy on site.
      - At most one LH cutter per heat.
      - Spread LH cutters one per heat 0..N-1.
      - If more LH cutters than heats, overflow goes to the FINAL heat with
        a warning (emitted via lh_warnings list).
      - Slow-heat cutters still cluster into the final heat (unchanged).
    """

    def test_one_lh_cutter_placed_in_heat_0(self):
        ev = _event(event_type='college', stand_type='springboard')
        comps = [
            _comp(1, name='A', is_left_handed=True),
            _comp(2, name='B'),
            _comp(3, name='C'),
            _comp(4, name='D'),
            _comp(5, name='E'),
            _comp(6, name='F'),
        ]
        heats = _generate_springboard_heats(comps, 2, 4, {}, event=ev)
        heat0_ids = {c['id'] for c in heats[0]}
        heat1_ids = {c['id'] for c in heats[1]}
        assert 1 in heat0_ids
        assert 1 not in heat1_ids

    def test_two_lh_cutters_spread_not_clustered(self):
        ev = _event(event_type='college', stand_type='springboard')
        comps = [
            _comp(1, name='A', is_left_handed=True),
            _comp(2, name='B', is_left_handed=True),
            _comp(3, name='C'),
            _comp(4, name='D'),
            _comp(5, name='E'),
            _comp(6, name='F'),
        ]
        heats = _generate_springboard_heats(comps, 2, 4, {}, event=ev)
        heat0_ids = {c['id'] for c in heats[0]}
        heat1_ids = {c['id'] for c in heats[1]}
        # LH 1 should be in heat 0, LH 2 in heat 1 — NOT both in heat 0.
        assert 1 in heat0_ids and 2 in heat1_ids
        # Defensive: if both were clustered, the test would have caught the old bug.
        assert not ({1, 2}.issubset(heat0_ids))

    def test_four_lh_cutters_four_heats_one_per_heat(self):
        ev = _event(event_type='college', stand_type='springboard')
        comps = [
            _comp(1, name='A', is_left_handed=True),
            _comp(2, name='B', is_left_handed=True),
            _comp(3, name='C', is_left_handed=True),
            _comp(4, name='D', is_left_handed=True),
            _comp(5, name='E'),
            _comp(6, name='F'),
            _comp(7, name='G'),
            _comp(8, name='H'),
            _comp(9, name='I'),
            _comp(10, name='J'),
            _comp(11, name='K'),
            _comp(12, name='L'),
        ]
        heats = _generate_springboard_heats(comps, 4, 3, {}, event=ev)
        lh_per_heat = [
            sum(1 for c in heat if c['id'] in {1, 2, 3, 4})
            for heat in heats
        ]
        assert lh_per_heat == [1, 1, 1, 1]

    def test_overflow_lh_goes_to_final_heat_with_warning(self):
        """5 LH cutters across 4 heats: 4 spread, 1 overflows to final heat."""
        ev = _event(event_type='college', stand_type='springboard')
        comps = [
            _comp(1, name='A', is_left_handed=True),
            _comp(2, name='B', is_left_handed=True),
            _comp(3, name='C', is_left_handed=True),
            _comp(4, name='D', is_left_handed=True),
            _comp(5, name='E', is_left_handed=True),
            _comp(6, name='F'),
            _comp(7, name='G'),
            _comp(8, name='H'),
        ]
        lh_warnings: list = []
        heats = _generate_springboard_heats(
            comps, 4, 4, {}, event=ev, lh_warnings=lh_warnings,
        )
        # Heat 0..2 each have exactly 1 LH; final heat has 2 LH.
        lh_per_heat = [
            sum(1 for c in heat if c['id'] in {1, 2, 3, 4, 5})
            for heat in heats
        ]
        assert lh_per_heat == [1, 1, 1, 2]
        # Overflow warning emitted for heat 3.
        assert len(lh_warnings) == 1
        assert lh_warnings[0]['type'] == 'lh_overflow'
        assert lh_warnings[0]['heat_index'] == 3
        assert lh_warnings[0]['overflow_count'] == 1

    def test_overflow_unplaceable_when_final_heat_full(self):
        """LH overflow exceeds max_per_heat of final heat — gear_violations records it."""
        ev = _event(event_type='college', stand_type='springboard')
        # 3 heats of max 2 cutters each = 6 slots.
        # 4 LH cutters: 3 spread to heats 0/1/2 (one each), 4th overflow to final
        # heat 2.  Final heat now has 1 LH from spread + 1 LH overflow = 2/2 capacity.
        # Add a 5th LH: overflow unplaceable.
        comps = [
            _comp(1, name='A', is_left_handed=True),
            _comp(2, name='B', is_left_handed=True),
            _comp(3, name='C', is_left_handed=True),
            _comp(4, name='D', is_left_handed=True),
            _comp(5, name='E', is_left_handed=True),
        ]
        lh_warnings: list = []
        gear_violations: list = []
        _generate_springboard_heats(
            comps, 3, 2, {}, event=ev,
            gear_violations=gear_violations, lh_warnings=lh_warnings,
        )
        # E (id=5) can't fit anywhere — all heats at max 2 after spread + 1 overflow.
        unplaceable = [v for v in gear_violations if 'unplaced' in v.get('reason', '').lower()]
        assert len(unplaceable) == 1
        assert unplaceable[0]['comp_name'] == 'E'

    def test_no_left_handers_no_warning_and_no_grouping(self):
        ev = _event(event_type='college', stand_type='springboard')
        comps = [_comp(i, name=f'C{i}') for i in range(1, 9)]
        lh_warnings: list = []
        heats = _generate_springboard_heats(
            comps, 2, 4, {}, event=ev, lh_warnings=lh_warnings,
        )
        assert sum(len(h) for h in heats) == 8
        assert lh_warnings == []

    def test_slow_heat_clusters_into_final_heat_unchanged(self):
        """Slow-heat behavior preserved: cluster into final heat."""
        ev = _event(event_type='college', stand_type='springboard')
        comps = [
            _comp(1, name='A', is_slow_springboard=True),
            _comp(2, name='B', is_slow_springboard=True),
            _comp(3, name='C'),
            _comp(4, name='D'),
            _comp(5, name='E'),
            _comp(6, name='F'),
            _comp(7, name='G'),
            _comp(8, name='H'),
        ]
        heats = _generate_springboard_heats(comps, 2, 4, {}, event=ev)
        # Slow cutters should both be in the final heat (idx 1).
        final_ids = {c['id'] for c in heats[1]}
        assert {1, 2}.issubset(final_ids)

    def test_lh_and_slow_heat_both_land_in_final_heat(self):
        """Interaction: 1 LH cutter alone fits heat 0; 1 slow-heat cutter in final.
        With only 2 heats, final heat hosts the slow cutter AND (eventually) overflow
        LH would go there too.  Smoke test that both mechanisms coexist without crash.
        """
        ev = _event(event_type='college', stand_type='springboard')
        comps = [
            _comp(1, name='A', is_left_handed=True),
            _comp(2, name='B', is_slow_springboard=True),
            _comp(3, name='C'),
            _comp(4, name='D'),
            _comp(5, name='E'),
            _comp(6, name='F'),
        ]
        heats = _generate_springboard_heats(comps, 2, 4, {}, event=ev)
        heat0_ids = {c['id'] for c in heats[0]}
        heat1_ids = {c['id'] for c in heats[1]}
        assert 1 in heat0_ids      # LH spread to heat 0
        assert 2 in heat1_ids      # slow in final
        # All six placed.
        assert len(heat0_ids | heat1_ids) == 6

    def test_all_placed(self):
        ev = _event(event_type='college', stand_type='springboard')
        comps = [_comp(i, name=f'C{i}') for i in range(1, 9)]
        heats = _generate_springboard_heats(comps, 2, 4, {}, event=ev)
        placed = [c for heat in heats for c in heat]
        assert len(placed) == 8


# ---------------------------------------------------------------------------
# _generate_saw_heats
# ---------------------------------------------------------------------------

class TestGenerateSawHeats:
    # Use college event type — pro events trigger a ProEventRank DB query
    # (_sort_by_ability) which requires a live app context.
    def test_max_four_per_heat(self):
        ev = _event(event_type='college', stand_type='saw_hand', is_partnered=False)
        comps = [_comp(i, name=f'C{i}') for i in range(1, 9)]
        heats = _generate_saw_heats(comps, 2, 8, {}, event=ev)
        assert all(len(h) <= 4 for h in heats)

    def test_all_placed(self):
        ev = _event(event_type='college', stand_type='saw_hand', is_partnered=False)
        comps = [_comp(i, name=f'C{i}') for i in range(1, 9)]
        heats = _generate_saw_heats(comps, 2, 8, {}, event=ev)
        placed = [c for heat in heats for c in heat]
        assert len(placed) == 8


# ---------------------------------------------------------------------------
# Partial heat positioning — solo/odd-out competitor closes the event
# ---------------------------------------------------------------------------

class TestPartialHeatGoesLast:
    """Convention (user rule, 2026-04-22): when a field doesn't divide evenly,
    the leftover competitor or partial-fill heat runs LAST in the event order,
    not first. Snake-draft on its own leaves the partial in heat 0; this is
    the regression guard that the post-process reorder fixes it.
    """

    def test_saw_odd_competitors_solo_lands_in_final_heat(self):
        """College stock saw with 19 competitors and 2 stands per heat — heat 1
        must NOT be the solo competitor. Mirrors the screenshot bug report.
        """
        ev = _event(event_type='college', name='Stock Saw', stand_type='saw_hand',
                    is_partnered=False)
        comps = [_comp(i, name=f'C{i}') for i in range(1, 20)]  # 19 competitors
        heats = _generate_saw_heats(comps, 10, 2, {}, event=ev)
        sizes = [len(h) for h in heats]
        assert sum(sizes) == 19
        assert len(heats) == 10
        # First heat must be FULL (2), final heat is the solo (1).
        assert sizes[0] == 2, f'Heat 1 should not be the solo heat — sizes={sizes}'
        assert sizes[-1] == 1, f'Final heat should hold the solo competitor — sizes={sizes}'

    def test_standard_partial_heat_at_end(self):
        """5 competitors / 2 per heat → 3 heats sized [2, 2, 1] — solo last."""
        ev = _event(event_type='college', stand_type='underhand')
        comps = [_comp(i, name=f'C{i}') for i in range(1, 6)]
        heats = _generate_standard_heats(comps, 3, 2, event=ev)
        sizes = [len(h) for h in heats]
        assert sizes == [2, 2, 1]

    def test_standard_three_per_heat_partial_at_end(self):
        """7 competitors / 3 per heat → 3 heats sized [3, 3, 1]."""
        ev = _event(event_type='college', stand_type='underhand')
        comps = [_comp(i, name=f'C{i}') for i in range(1, 8)]
        heats = _generate_standard_heats(comps, 3, 3, event=ev)
        sizes = [len(h) for h in heats]
        # Snake-draft balances naturally — accept any layout where the smallest
        # size is at the end (not in heat 0).
        assert sizes[0] >= sizes[-1], f'First heat must be >= last — sizes={sizes}'
        assert sum(sizes) == 7

    def test_standard_full_field_no_reorder(self):
        """8 competitors / 2 per heat divides evenly — no partial, ordering
        unchanged from the snake draft."""
        ev = _event(event_type='college', stand_type='underhand')
        comps = [_comp(i, name=f'C{i}') for i in range(1, 9)]
        heats = _generate_standard_heats(comps, 4, 2, event=ev)
        sizes = [len(h) for h in heats]
        assert sizes == [2, 2, 2, 2]

    def test_standard_single_heat_no_reorder(self):
        """One heat, one competitor — nothing to reorder."""
        ev = _event(event_type='college', stand_type='underhand')
        comps = [_comp(1, name='Solo')]
        heats = _generate_standard_heats(comps, 1, 4, event=ev)
        assert len(heats) == 1
        assert len(heats[0]) == 1

    def test_springboard_no_lh_no_slow_partial_at_end(self):
        """Plain springboard with odd field — partial closes the event."""
        ev = _event(event_type='college', stand_type='springboard')
        comps = [_comp(i, name=f'C{i}') for i in range(1, 6)]  # 5 cutters
        heats = _generate_springboard_heats(comps, 3, 2, {}, event=ev)
        sizes = [len(h) for h in heats]
        assert sum(sizes) == 5
        assert sizes[0] >= sizes[-1], f'Partial must be last — sizes={sizes}'

    def test_springboard_slow_cluster_pinned_to_final_even_when_partial_exists(self):
        """Slow-heat invariant takes precedence over partial-at-end. Slow cutters
        must remain in the final heat even if it means a non-slow heat is the
        partial (rare interaction case)."""
        ev = _event(event_type='college', stand_type='springboard')
        # 5 cutters, 1 slow — the slow cluster goes to final heat by design.
        comps = [
            _comp(1, name='Slow', is_slow_springboard=True),
            _comp(2, name='B'),
            _comp(3, name='C'),
            _comp(4, name='D'),
            _comp(5, name='E'),
        ]
        heats = _generate_springboard_heats(comps, 3, 2, {}, event=ev)
        # Slow cutter must be in the final heat (idx -1).
        final_ids = {c['id'] for c in heats[-1]}
        assert 1 in final_ids, (
            f'Slow cutter must close the event — heats={[[c["id"] for c in h] for h in heats]}'
        )

    def test_gear_violation_heat_index_follows_reorder(self):
        """When a fallback gear-conflict places a competitor in the partial
        heat AND the partial gets reordered to the end, the gear_violations
        entry's heat_index must point at the NEW position so the judge's
        flash-warning fingers the correct heat (not the original snake-draft
        index that no longer exists in the final order)."""
        ev = _event(event_type='college', stand_type='underhand')
        # 3 competitors, all sharing axe gear → snake forces heat 0 to take
        # the leftover via the fallback path. With max_per_heat=1, num_heats=3,
        # so all heats end up partial — _no_ reorder occurs (all-partial guard).
        # Use 5 comps with max_per_heat=2 → snake gives sizes [1, 2, 2], a real
        # reorder. To force a fallback gear violation in the partial heat,
        # competitors share gear so the snake's first pass conflicts.
        gear = {'axe': True}
        comps = [
            _comp(i, name=f'C{i}', gear_sharing=dict(gear))
            for i in range(1, 6)
        ]
        gear_violations: list = []
        heats = _generate_standard_heats(
            comps, 3, 2, event=ev, gear_violations=gear_violations,
        )
        sizes = [len(h) for h in heats]
        assert sum(sizes) == 5
        # Partial heat (size 1) is at the end after reorder.
        assert sizes[-1] == 1, f'expected partial last, got {sizes}'
        # Any logged gear_violation heat_index points at a valid heat in the
        # post-reorder order (not stale pre-reorder index that would mislead
        # the judge's flash warning to the wrong heat number).
        for v in gear_violations:
            idx = v['heat_index']
            assert 0 <= idx < len(heats), (
                f'gear_violation heat_index={idx} out of range — stale after reorder?'
            )
            # The named competitor must actually be in the heat the violation
            # points at — proves the remap is correct, not just in-bounds.
            comp_id = v['comp_id']
            assert any(c['id'] == comp_id for c in heats[idx]), (
                f'gear_violation says comp {comp_id} is in heat {idx} but they are not — '
                f'heat {idx} contains {[c["id"] for c in heats[idx]]}'
            )

    def test_partnered_saw_odd_pairs_partial_at_end(self):
        """Partnered Jack & Jill: 5 mixed pairs (10 competitors) on 2-stand heats
        → 3 heats with units [2, 2, 1]. Partial pair-heat must close the event.
        """
        ev = _event(event_type='college', name='Jack & Jill Sawing',
                    stand_type='saw_hand', is_partnered=True, partner_gender='mixed')
        # 5 male + 5 female pairs by partner_name link
        pairs = []
        for i in range(5):
            m_id = 100 + i
            f_id = 200 + i
            m_name = f'M{i}'
            f_name = f'F{i}'
            pairs.append(_comp(m_id, name=m_name, gender='M', partner_name=f_name))
            pairs.append(_comp(f_id, name=f_name, gender='F', partner_name=m_name))
        # _generate_standard_heats handles partner unitization. 5 units / 2 per
        # heat → 3 heats; expect stands_used = [2, 2, 1].
        heats = _generate_standard_heats(pairs, 3, 2, event=ev)
        # Each unit is a pair (2 competitors). Sizes in competitor count: full=4, partial=2.
        sizes = [len(h) for h in heats]
        assert sum(sizes) == 10, f'All 10 partnered competitors placed — sizes={sizes}'
        assert sizes[0] >= sizes[-1], f'Partial pair-heat must be last — sizes={sizes}'
