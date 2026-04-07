"""
Unit tests for services/gear_sharing.py pure helper functions.

No database required — all tested functions are pure Python.

Run:  pytest tests/test_gear_sharing.py -v
"""
from types import SimpleNamespace

import pytest

from services.gear_sharing import (
    build_name_index,
    competitors_share_gear_for_event,
    event_matches_gear_key,
    get_family_events,
    get_gear_family,
    infer_equipment_categories,
    is_no_constraint_event,
    normalize_event_text,
    normalize_person_name,
    resolve_partner_name,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(id=1, name='Single Buck', display_name=None, stand_type='saw_hand', event_type='pro'):
    return SimpleNamespace(
        id=id, name=name,
        display_name=display_name or name,
        stand_type=stand_type,
        event_type=event_type,
    )


# ---------------------------------------------------------------------------
# normalize_person_name
# ---------------------------------------------------------------------------

class TestNormalizePersonName:
    def test_strips_and_lowercases(self):
        assert normalize_person_name('  Alice Smith  ') == 'alicesmith'

    def test_removes_punctuation(self):
        assert normalize_person_name("O'Brien") == 'obrien'

    def test_removes_hyphens(self):
        assert normalize_person_name('Smith-Jones') == 'smithjones'

    def test_empty_string(self):
        assert normalize_person_name('') == ''

    def test_none_becomes_empty(self):
        assert normalize_person_name(None) == ''

    def test_numeric_characters_kept(self):
        assert normalize_person_name('John3') == 'john3'


# ---------------------------------------------------------------------------
# normalize_event_text
# ---------------------------------------------------------------------------

class TestNormalizeEventText:
    def test_spaces_stripped(self):
        assert normalize_event_text('Single Buck') == 'singlebuck'

    def test_ampersand_removed(self):
        assert normalize_event_text('Jack & Jill') == 'jackjill'

    def test_none_becomes_empty(self):
        assert normalize_event_text(None) == ''

    def test_already_normalized(self):
        assert normalize_event_text('springboard') == 'springboard'


# ---------------------------------------------------------------------------
# build_name_index
# ---------------------------------------------------------------------------

class TestBuildNameIndex:
    def test_names_indexed_by_normalized_key(self):
        idx = build_name_index(['Alice Smith', 'Bob Jones'])
        assert idx.get('alicesmith') == 'Alice Smith'
        assert idx.get('bobjones') == 'Bob Jones'

    def test_empty_names_skipped(self):
        idx = build_name_index(['Alice', '', None])
        assert len(idx) == 1

    def test_duplicate_names_first_wins(self):
        # Two entries normalizing to the same key — first wins
        idx = build_name_index(['Alice', 'ALICE'])
        assert idx.get('alice') == 'Alice'

    def test_empty_iterable(self):
        idx = build_name_index([])
        assert idx == {}


# ---------------------------------------------------------------------------
# resolve_partner_name
# ---------------------------------------------------------------------------

class TestResolvePartnerName:
    def setup_method(self):
        self.index = build_name_index([
            'Alice Smith', 'Bob Jones', 'Charlie Brown', 'Diana Prince'
        ])

    def test_exact_match(self):
        assert resolve_partner_name('Alice Smith', self.index) == 'Alice Smith'

    def test_case_insensitive_exact_match(self):
        assert resolve_partner_name('alice smith', self.index) == 'Alice Smith'

    def test_fuzzy_match(self):
        # 'Bob Jonnes' is close to 'Bob Jones'
        result = resolve_partner_name('Bob Jonnes', self.index)
        assert result == 'Bob Jones'

    def test_last_name_only_unambiguous(self):
        # 'Prince' matches only 'Diana Prince'
        result = resolve_partner_name('Prince', self.index)
        assert result == 'Diana Prince'

    def test_last_name_only_ambiguous_returns_raw(self):
        # Both 'Alice Smith' and another 'Smith' would be ambiguous — returns raw
        idx = build_name_index(['Alice Smith', 'Bob Smith'])
        result = resolve_partner_name('Smith', idx)
        # Ambiguous: must return the original input unchanged
        assert result == 'Smith'

    def test_initials_match(self):
        # 'A. Smith' should match 'Alice Smith'
        result = resolve_partner_name('A. Smith', self.index)
        assert result == 'Alice Smith'

    def test_empty_input_returns_empty(self):
        assert resolve_partner_name('', self.index) == ''

    def test_none_input_returns_empty(self):
        assert resolve_partner_name(None, self.index) == ''

    def test_unknown_name_returned_as_is(self):
        result = resolve_partner_name('Zephyr Unkown', self.index)
        assert result == 'Zephyr Unkown'

    def test_empty_index_returns_raw(self):
        result = resolve_partner_name('Alice', {})
        assert result == 'Alice'


# ---------------------------------------------------------------------------
# infer_equipment_categories
# ---------------------------------------------------------------------------

class TestInferEquipmentCategories:
    def test_crosscut_from_single_buck(self):
        cats = infer_equipment_categories('shares single buck with partner')
        assert 'crosscut' in cats

    def test_crosscut_from_double_buck(self):
        cats = infer_equipment_categories('double buck')
        assert 'crosscut' in cats

    def test_crosscut_from_hand_saw(self):
        cats = infer_equipment_categories('hand saw equipment')
        assert 'crosscut' in cats

    def test_chainsaw_from_hot_saw(self):
        cats = infer_equipment_categories('hot saw - borrowed from team')
        assert 'chainsaw' in cats

    def test_stock_saw_not_chainsaw(self):
        # Stock saws are show-provided — no gear sharing constraint
        cats = infer_equipment_categories('stock saw gear')
        assert 'chainsaw' not in cats

    def test_springboard_from_board(self):
        cats = infer_equipment_categories('springboard setup')
        assert 'springboard' in cats

    def test_springboard_from_board_keyword(self):
        cats = infer_equipment_categories('uses board for 1 board event')
        assert 'springboard' in cats

    def test_multiple_categories(self):
        cats = infer_equipment_categories('shares springboard and hot saw')
        assert 'springboard' in cats
        assert 'chainsaw' in cats

    def test_no_match_returns_empty(self):
        cats = infer_equipment_categories('general equipment sharing')
        assert cats == set()

    def test_empty_string_returns_empty(self):
        cats = infer_equipment_categories('')
        assert cats == set()

    def test_none_returns_empty(self):
        cats = infer_equipment_categories(None)
        assert cats == set()


# ---------------------------------------------------------------------------
# event_matches_gear_key
# ---------------------------------------------------------------------------

class TestEventMatchesGearKey:
    def test_match_by_event_id(self):
        ev = _event(id=7)
        assert event_matches_gear_key(ev, '7') is True

    def test_no_match_wrong_id(self):
        ev = _event(id=7)
        assert event_matches_gear_key(ev, '99') is False

    def test_match_by_normalized_name(self):
        ev = _event(id=1, name='Single Buck', stand_type='saw_hand')
        assert event_matches_gear_key(ev, 'Single Buck') is True

    def test_category_crosscut_matches_saw_hand(self):
        ev = _event(id=1, name='Single Buck', stand_type='saw_hand')
        assert event_matches_gear_key(ev, 'category:crosscut') is True

    def test_category_chainsaw_matches_hot_saw(self):
        ev = _event(id=1, name='Hot Saw', stand_type='hot_saw')
        assert event_matches_gear_key(ev, 'category:chainsaw') is True

    def test_category_springboard_matches_springboard(self):
        ev = _event(id=1, name='Springboard', stand_type='springboard')
        assert event_matches_gear_key(ev, 'category:springboard') is True

    def test_category_crosscut_does_not_match_springboard(self):
        ev = _event(id=1, name='Springboard', stand_type='springboard')
        assert event_matches_gear_key(ev, 'category:crosscut') is False

    def test_none_event_returns_false(self):
        assert event_matches_gear_key(None, 'Single Buck') is False

    def test_empty_key_returns_false(self):
        ev = _event(id=1)
        assert event_matches_gear_key(ev, '') is False


# ---------------------------------------------------------------------------
# competitors_share_gear_for_event
# ---------------------------------------------------------------------------

class TestCompetitorsShareGearForEvent:
    def test_comp1_lists_comp2_for_this_event(self):
        ev = _event(id=5, stand_type='saw_hand')
        comp1_gear = {'5': 'Bob Jones'}
        comp2_gear = {}
        result = competitors_share_gear_for_event('Alice', comp1_gear, 'Bob Jones', comp2_gear, ev)
        assert result is True

    def test_comp2_lists_comp1_for_this_event(self):
        ev = _event(id=5, stand_type='saw_hand')
        comp1_gear = {}
        comp2_gear = {'5': 'Alice'}
        result = competitors_share_gear_for_event('Alice', comp1_gear, 'Bob', comp2_gear, ev)
        assert result is True

    def test_no_conflict_when_gear_for_different_event(self):
        ev = _event(id=5, stand_type='saw_hand')
        comp1_gear = {'99': 'Bob'}  # key 99 doesn't match event id 5
        comp2_gear = {}
        result = competitors_share_gear_for_event('Alice', comp1_gear, 'Bob', comp2_gear, ev)
        assert result is False

    def test_no_conflict_unrelated_competitors(self):
        ev = _event(id=5)
        result = competitors_share_gear_for_event('Alice', {}, 'Bob', {}, ev)
        assert result is False

    def test_event_none_uses_any_key_match(self):
        # When event=None, any matching partner name across any key triggers conflict
        comp1_gear = {'anything': 'Bob'}
        comp2_gear = {}
        result = competitors_share_gear_for_event('Alice', comp1_gear, 'Bob', comp2_gear, None)
        assert result is True

    def test_case_insensitive_name_matching(self):
        ev = _event(id=5, stand_type='saw_hand')
        comp1_gear = {'5': 'BOB JONES'}
        comp2_gear = {}
        result = competitors_share_gear_for_event('Alice', comp1_gear, 'bob jones', comp2_gear, ev)
        assert result is True

    def test_category_key_conflict(self):
        ev = _event(id=5, name='Single Buck', stand_type='saw_hand')
        comp1_gear = {'category:crosscut': 'Bob'}
        comp2_gear = {}
        result = competitors_share_gear_for_event('Alice', comp1_gear, 'Bob', comp2_gear, ev)
        assert result is True

    def test_no_conflict_empty_gear_both_sides(self):
        ev = _event(id=5)
        result = competitors_share_gear_for_event('Alice', {}, 'Bob', {}, ev)
        assert result is False

    def test_non_dict_gear_treated_as_empty(self):
        ev = _event(id=5)
        result = competitors_share_gear_for_event('Alice', None, 'Bob', None, ev)
        assert result is False


# ---------------------------------------------------------------------------
# get_gear_family
# ---------------------------------------------------------------------------

class TestGetGearFamily:
    def test_underhand_is_chopping(self):
        fam, conf = get_gear_family(_event(stand_type='underhand'))
        assert fam == 'chopping'
        assert conf['cascade'] is True

    def test_standing_block_is_chopping(self):
        fam, _ = get_gear_family(_event(stand_type='standing_block'))
        assert fam == 'chopping'

    def test_springboard_is_chopping(self):
        fam, _ = get_gear_family(_event(stand_type='springboard'))
        assert fam == 'chopping'

    def test_saw_hand_is_crosscut_saw(self):
        fam, conf = get_gear_family(_event(stand_type='saw_hand'))
        assert fam == 'crosscut_saw'
        assert conf['cascade'] is True

    def test_hot_saw_family(self):
        fam, conf = get_gear_family(_event(stand_type='hot_saw'))
        assert fam == 'hot_saw'
        assert conf['cascade'] is False

    def test_speed_climb_is_climbing(self):
        fam, _ = get_gear_family(_event(stand_type='speed_climb'))
        assert fam == 'climbing'

    def test_obstacle_pole_pro_is_op_saw(self):
        fam, _ = get_gear_family(_event(stand_type='obstacle_pole', event_type='pro'))
        assert fam == 'op_saw'

    def test_obstacle_pole_college_excluded_by_pro_only(self):
        fam, conf = get_gear_family(_event(stand_type='obstacle_pole', event_type='college'))
        assert fam is None
        assert conf is None

    def test_cookie_stack_family(self):
        fam, conf = get_gear_family(_event(stand_type='cookie_stack'))
        assert fam == 'cookie_stack'
        assert conf['cascade'] is False

    def test_stock_saw_no_family(self):
        fam, _ = get_gear_family(_event(stand_type='stock_saw'))
        assert fam is None

    def test_birling_no_family(self):
        fam, _ = get_gear_family(_event(stand_type='birling'))
        assert fam is None

    def test_axe_throw_no_family(self):
        fam, _ = get_gear_family(_event(stand_type='axe_throw'))
        assert fam is None

    def test_empty_stand_type(self):
        fam, _ = get_gear_family(_event(stand_type=''))
        assert fam is None

    def test_none_stand_type(self):
        fam, _ = get_gear_family(_event(stand_type=None))
        assert fam is None


# ---------------------------------------------------------------------------
# get_family_events
# ---------------------------------------------------------------------------

class TestGetFamilyEvents:
    def setup_method(self):
        self.uh = _event(id=10, name='Underhand', stand_type='underhand')
        self.sb = _event(id=20, name='Standing Block', stand_type='standing_block')
        self.spring = _event(id=30, name='Springboard', stand_type='springboard')
        self.single = _event(id=40, name='Single Buck', stand_type='saw_hand')
        self.double = _event(id=50, name='Double Buck', stand_type='saw_hand')
        self.jj = _event(id=60, name='Jack & Jill', stand_type='saw_hand')
        self.hot = _event(id=70, name='Hot Saw', stand_type='hot_saw')
        self.stock = _event(id=80, name='Stock Saw', stand_type='stock_saw')
        self.all = [self.uh, self.sb, self.spring, self.single,
                    self.double, self.jj, self.hot, self.stock]

    def test_chopping_cascade_returns_siblings(self):
        siblings = get_family_events(self.uh, self.all)
        sibling_ids = {e.id for e in siblings}
        assert sibling_ids == {20, 30}  # standing_block + springboard

    def test_chopping_cascade_excludes_self(self):
        siblings = get_family_events(self.spring, self.all)
        assert self.spring.id not in {e.id for e in siblings}

    def test_crosscut_cascade_returns_siblings(self):
        siblings = get_family_events(self.single, self.all)
        sibling_ids = {e.id for e in siblings}
        assert sibling_ids == {50, 60}  # double buck + J&J

    def test_hot_saw_no_cascade(self):
        siblings = get_family_events(self.hot, self.all)
        assert siblings == []

    def test_stock_saw_no_family_no_siblings(self):
        siblings = get_family_events(self.stock, self.all)
        assert siblings == []

    def test_empty_event_list(self):
        siblings = get_family_events(self.uh, [])
        assert siblings == []


# ---------------------------------------------------------------------------
# is_no_constraint_event
# ---------------------------------------------------------------------------

class TestIsNoConstraintEvent:
    def test_stock_saw(self):
        assert is_no_constraint_event(_event(stand_type='stock_saw')) is True

    def test_axe_throw(self):
        assert is_no_constraint_event(_event(stand_type='axe_throw')) is True

    def test_birling(self):
        assert is_no_constraint_event(_event(stand_type='birling')) is True

    def test_peavey(self):
        assert is_no_constraint_event(_event(stand_type='peavey')) is True

    def test_caber(self):
        assert is_no_constraint_event(_event(stand_type='caber')) is True

    def test_pulp_toss(self):
        assert is_no_constraint_event(_event(stand_type='pulp_toss')) is True

    def test_chokerman(self):
        assert is_no_constraint_event(_event(stand_type='chokerman')) is True

    def test_college_obstacle_pole_no_constraint(self):
        assert is_no_constraint_event(_event(stand_type='obstacle_pole', event_type='college')) is True

    def test_pro_obstacle_pole_has_constraint(self):
        assert is_no_constraint_event(_event(stand_type='obstacle_pole', event_type='pro')) is False

    def test_underhand_has_constraint(self):
        assert is_no_constraint_event(_event(stand_type='underhand')) is False

    def test_springboard_has_constraint(self):
        assert is_no_constraint_event(_event(stand_type='springboard')) is False

    def test_hot_saw_has_constraint(self):
        assert is_no_constraint_event(_event(stand_type='hot_saw')) is False

    def test_saw_hand_has_constraint(self):
        assert is_no_constraint_event(_event(stand_type='saw_hand')) is False


# ---------------------------------------------------------------------------
# event_matches_gear_key — stock_saw / hot_saw separation
# ---------------------------------------------------------------------------

class TestEventMatchesGearKeyStockSawFix:
    def test_category_chainsaw_does_not_match_stock_saw(self):
        ev = _event(id=1, name='Stock Saw', stand_type='stock_saw')
        assert event_matches_gear_key(ev, 'category:chainsaw') is False

    def test_category_chainsaw_still_matches_hot_saw(self):
        ev = _event(id=1, name='Hot Saw', stand_type='hot_saw')
        assert event_matches_gear_key(ev, 'category:chainsaw') is True

    def test_hot_saw_aliases_do_not_include_stocksaw(self):
        ev = _event(id=1, name='Hot Saw', stand_type='hot_saw')
        # 'stocksaw' should not match hot_saw event
        assert event_matches_gear_key(ev, 'stocksaw') is False


# ---------------------------------------------------------------------------
# Cascade conflict detection
# ---------------------------------------------------------------------------

class TestCascadeConflictDetection:
    """Tests for cross-event gear conflict cascade via the all_events parameter."""

    def setup_method(self):
        self.uh = _event(id=10, name='Underhand', stand_type='underhand')
        self.sb = _event(id=20, name='Standing Block', stand_type='standing_block')
        self.spring = _event(id=30, name='Springboard', stand_type='springboard')
        self.single = _event(id=40, name='Single Buck', stand_type='saw_hand')
        self.double = _event(id=50, name='Double Buck', stand_type='saw_hand')
        self.jj = _event(id=60, name='Jack & Jill Sawing', stand_type='saw_hand')
        self.hot = _event(id=70, name='Hot Saw', stand_type='hot_saw')
        self.climb = _event(id=80, name='Speed Climb', stand_type='speed_climb')
        self.all = [self.uh, self.sb, self.spring, self.single,
                    self.double, self.jj, self.hot, self.climb]

    # --- Chopping family cascade ---

    def test_axe_shared_for_springboard_conflicts_in_underhand(self):
        """Sharing an axe declared for Springboard should cascade to Underhand."""
        gear_a = {'30': 'Bob'}  # gear key = springboard event id
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.uh, all_events=self.all)
        assert result is True

    def test_axe_shared_for_underhand_conflicts_in_springboard(self):
        """Sharing declared for Underhand cascades to Springboard."""
        gear_a = {'10': 'Bob'}  # gear key = underhand event id
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.spring, all_events=self.all)
        assert result is True

    def test_axe_shared_for_underhand_conflicts_in_standing_block(self):
        gear_a = {'10': 'Bob'}
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.sb, all_events=self.all)
        assert result is True

    def test_chopping_cascade_bidirectional(self):
        """Comp2's gear declaration for Standing Block cascades when checking Springboard."""
        gear_b = {'20': 'Alice'}  # comp2 lists comp1 for standing block
        result = competitors_share_gear_for_event(
            'Alice', {}, 'Bob', gear_b, self.spring, all_events=self.all)
        # comp2 has standing block key with 'Alice' — but that doesn't match 'Alice' as comp1_name
        # because the check is: partner2 == name1 ('alice')
        # Wait: gear_b = {'20': 'Alice'}, and comp1_name = 'Alice'
        # So: key2='20' matches standing_block (sibling of springboard), partner2='alice' == name1='alice'
        assert result is True

    def test_chopping_does_not_cascade_to_saw_events(self):
        """Chopping family (axes) should NOT cascade to crosscut saw events."""
        gear_a = {'10': 'Bob'}  # gear for underhand (chopping)
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.single, all_events=self.all)
        assert result is False

    # --- Crosscut saw family cascade ---

    def test_saw_shared_for_single_buck_conflicts_in_double_buck(self):
        gear_a = {'40': 'Bob'}  # gear key = single buck event id
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.double, all_events=self.all)
        assert result is True

    def test_saw_shared_for_single_buck_conflicts_in_jj(self):
        gear_a = {'40': 'Bob'}
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.jj, all_events=self.all)
        assert result is True

    def test_saw_shared_for_double_buck_conflicts_in_single_buck(self):
        gear_a = {'50': 'Bob'}  # gear key = double buck event id
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.single, all_events=self.all)
        assert result is True

    def test_saw_does_not_cascade_to_chopping(self):
        """Crosscut saw family should NOT cascade to chopping events."""
        gear_a = {'40': 'Bob'}  # single buck (saw_hand)
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.uh, all_events=self.all)
        assert result is False

    # --- Non-cascade families stay isolated ---

    def test_hot_saw_does_not_cascade_to_anything(self):
        gear_a = {'70': 'Bob'}  # hot saw
        # Should NOT cascade to single buck or underhand
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.single, all_events=self.all) is False
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.uh, all_events=self.all) is False

    def test_hot_saw_still_conflicts_within_own_event(self):
        gear_a = {'70': 'Bob'}
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.hot, all_events=self.all)
        assert result is True

    def test_climbing_gear_stays_isolated(self):
        gear_a = {'80': 'Bob'}  # speed climb
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.uh, all_events=self.all) is False
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.climb, all_events=self.all) is True

    # --- Backward compatibility: no cascade without all_events ---

    def test_no_cascade_without_all_events(self):
        """Without all_events, sharing for Springboard should NOT cascade to Underhand."""
        gear_a = {'30': 'Bob'}  # springboard
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.uh)
        assert result is False

    def test_no_cascade_without_all_events_saw(self):
        """Without all_events, sharing for Single Buck should NOT cascade to Double Buck."""
        gear_a = {'40': 'Bob'}
        result = competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.double)
        assert result is False

    # --- Category keys still work with cascade ---

    def test_category_crosscut_with_cascade(self):
        """category:crosscut key should match all saw_hand events even without cascade."""
        gear_a = {'category:crosscut': 'Bob'}
        # Direct match — category key matches single buck
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.single) is True
        # Category key also matches double buck directly
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.double) is True

    def test_category_springboard_with_cascade_to_underhand(self):
        """category:springboard key should cascade to underhand via all_events."""
        gear_a = {'category:springboard': 'Bob'}
        # Direct match — category:springboard does NOT match underhand
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.uh) is False
        # But with cascade, springboard is in chopping family → checks siblings
        # The cascade checks siblings: standing_block and springboard as check events
        # category:springboard matches springboard check_event → Bob == name2 → True
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.uh, all_events=self.all) is True

    # --- Multiple gear declarations across families ---

    def test_multiple_gear_entries_only_correct_family_cascades(self):
        """A competitor sharing both axe and saw should cascade each independently."""
        gear_a = {'10': 'Bob', '40': 'Charlie'}  # axe for UH with Bob, saw for SB with Charlie
        # Bob should conflict in all chopping events
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.spring, all_events=self.all) is True
        # Charlie should conflict in all saw events
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Charlie', {}, self.double, all_events=self.all) is True
        # But Bob should NOT conflict in saw events
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Bob', {}, self.single, all_events=self.all) is False
        # And Charlie should NOT conflict in chopping events
        assert competitors_share_gear_for_event(
            'Alice', gear_a, 'Charlie', {}, self.uh, all_events=self.all) is False
