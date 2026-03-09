"""
Unit tests for services/gear_sharing.py pure helper functions.

No database required — all tested functions are pure Python.

Run:  pytest tests/test_gear_sharing.py -v
"""
import pytest
from types import SimpleNamespace
from services.gear_sharing import (
    normalize_person_name,
    normalize_event_text,
    build_name_index,
    resolve_partner_name,
    infer_equipment_categories,
    competitors_share_gear_for_event,
    event_matches_gear_key,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(id=1, name='Single Buck', display_name=None, stand_type='saw_hand'):
    return SimpleNamespace(
        id=id, name=name,
        display_name=display_name or name,
        stand_type=stand_type,
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

    def test_chainsaw_from_stock_saw(self):
        cats = infer_equipment_categories('stock saw gear')
        assert 'chainsaw' in cats

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
