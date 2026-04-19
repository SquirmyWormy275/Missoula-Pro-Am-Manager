"""
Realistic gear sharing parser tests using synthetic pro entry form data.

Tests parse_gear_sharing_details(), resolve_partner_name(), build_name_index(),
normalize_person_name(), and infer_equipment_categories() against the full
25-competitor pool from tests/fixtures/synthetic_data.py.
"""
import types

import pytest

from services.gear_sharing import (
    build_name_index,
    infer_equipment_categories,
    normalize_person_name,
    parse_gear_sharing_details,
    resolve_partner_name,
)
from tests.fixtures.synthetic_data import PRO_COMPETITORS, PRO_GEAR_SHARING_TEXTS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_PRO_NAMES = [c['name'] for c in PRO_COMPETITORS]
NAME_INDEX = build_name_index(ALL_PRO_NAMES)

# Map of stand_type values to use for mock events based on event name keywords.
_STAND_TYPE_MAP = {
    'underhand': 'underhand',
    'standing block': 'standing_block',
    'standing': 'standing_block',
    'springboard': 'springboard',
    '1-board': 'springboard',
    '1 board': 'springboard',
    'single buck': 'saw_hand',
    'double buck': 'saw_hand',
    'jack & jill': 'saw_hand',
    'jack and jill': 'saw_hand',
    'hot saw': 'hot_saw',
    'obstacle pole': 'obstacle_pole',
    'speed climb': 'speed_climb',
    'cookie stack': 'cookie_stack',
    'partnered axe': 'axe_throw',
    'stock saw': 'stock_saw',
}


def _stand_type_for(event_name: str) -> str:
    lower = event_name.lower()
    for keyword, st in _STAND_TYPE_MAP.items():
        if keyword in lower:
            return st
    return 'other'


def _make_mock_events() -> list:
    """Build SimpleNamespace event objects covering all pro events."""
    event_names = set()
    for comp in PRO_COMPETITORS:
        for ev in comp.get('events', []):
            event_names.add(ev)
    events = []
    for idx, name in enumerate(sorted(event_names), start=1):
        st = _stand_type_for(name)
        events.append(types.SimpleNamespace(
            id=idx,
            name=name,
            display_name=name,
            stand_type=st,
            event_type='pro',
        ))
    return events


MOCK_EVENTS = _make_mock_events()


def _entered_event_names_for(competitor_name: str) -> list[str]:
    """Return the list of event names the competitor entered."""
    for comp in PRO_COMPETITORS:
        if comp['name'] == competitor_name:
            return comp.get('events', [])
    return []


# ---------------------------------------------------------------------------
# 1. TestParseGearSharingRealistic
# ---------------------------------------------------------------------------

class TestParseGearSharingRealistic:
    """Parse every PRO_GEAR_SHARING_TEXTS entry against the full 25-name pool."""

    @pytest.mark.parametrize('entry', PRO_GEAR_SHARING_TEXTS,
                             ids=[e['competitor'] for e in PRO_GEAR_SHARING_TEXTS])
    def test_partner_resolved_and_events_matched(self, entry):
        entered = _entered_event_names_for(entry['competitor'])
        gear_map, warnings = parse_gear_sharing_details(
            entry['text'],
            MOCK_EVENTS,
            NAME_INDEX,
            self_name=entry['competitor'],
            entered_event_names=entered,
        )

        # Partner must have been found (no 'partner_not_resolved' warning).
        assert 'partner_not_resolved' not in warnings, (
            f"Partner not resolved for {entry['competitor']}: {entry['text']}"
        )

        # At least one event or category key must have been matched.
        assert len(gear_map) > 0, (
            f"No event keys matched for {entry['competitor']}: {entry['text']}"
        )

        # The resolved partner name should match the expected partner.
        partner_names_in_map = set(gear_map.values())
        assert entry['expected_partner'] in partner_names_in_map, (
            f"Expected partner '{entry['expected_partner']}' not in "
            f"gear_map values {partner_names_in_map}"
        )

    @pytest.mark.parametrize('entry', PRO_GEAR_SHARING_TEXTS,
                             ids=[e['competitor'] for e in PRO_GEAR_SHARING_TEXTS])
    def test_expected_event_keywords_present(self, entry):
        """The matched event keys should cover at least one expected keyword."""
        entered = _entered_event_names_for(entry['competitor'])
        gear_map, _ = parse_gear_sharing_details(
            entry['text'],
            MOCK_EVENTS,
            NAME_INDEX,
            self_name=entry['competitor'],
            entered_event_names=entered,
        )

        # Build a combined string of all matched keys for assertion.
        # Includes: event names (from ID lookup), category keys, and raw gear_map keys.
        matched_tokens = ''
        for k in gear_map.keys():
            if k.isdigit():
                eid = int(k)
                for ev in MOCK_EVENTS:
                    if ev.id == eid:
                        matched_tokens += ' ' + ev.name.lower()
                        # Also add stand_type and short codes
                        matched_tokens += ' ' + (ev.stand_type or '').lower()
            elif k.startswith('category:'):
                matched_tokens += ' ' + k
            else:
                matched_tokens += ' ' + k.lower()

        # Also add the raw keys themselves (some tests check for 'sb', 'uh', 'op')
        matched_tokens += ' ' + ' '.join(gear_map.keys()).lower()

        for keyword in entry['expected_events_contain']:
            assert keyword.lower() in matched_tokens, (
                f"Expected keyword '{keyword}' not found in matched events "
                f"for {entry['competitor']}: keys={list(gear_map.keys())} tokens='{matched_tokens.strip()}'"
            )


# ---------------------------------------------------------------------------
# 2. TestBidirectionalGearSharing
# ---------------------------------------------------------------------------

class TestBidirectionalGearSharing:
    """When A shares with B and B shares with A, both should parse correctly."""

    BIDIRECTIONAL_PAIRS = [
        ('Imortal Joe', 'Joe Manyfingers'),
        ('Jonathon Wept', 'Meau Jeau'),
        ('Dee John', 'Juicy Crust'),
        ('Steptoe Edwall', 'Carson Mitsubishi'),
        ('Ada Byrd', 'Jaam Slam'),
        ('Dorian Gray', 'Garfield Heathcliff'),
    ]

    @pytest.mark.parametrize('name_a,name_b', BIDIRECTIONAL_PAIRS)
    def test_both_directions_resolve(self, name_a, name_b):
        text_a = None
        text_b = None
        for entry in PRO_GEAR_SHARING_TEXTS:
            if entry['competitor'] == name_a:
                text_a = entry['text']
            if entry['competitor'] == name_b:
                text_b = entry['text']

        if text_a is None or text_b is None:
            pytest.skip(f"Missing text for {name_a} or {name_b}")

        entered_a = _entered_event_names_for(name_a)
        entered_b = _entered_event_names_for(name_b)

        map_a, warn_a = parse_gear_sharing_details(
            text_a, MOCK_EVENTS, NAME_INDEX,
            self_name=name_a, entered_event_names=entered_a,
        )
        map_b, warn_b = parse_gear_sharing_details(
            text_b, MOCK_EVENTS, NAME_INDEX,
            self_name=name_b, entered_event_names=entered_b,
        )

        assert 'partner_not_resolved' not in warn_a, f"{name_a} partner not resolved"
        assert 'partner_not_resolved' not in warn_b, f"{name_b} partner not resolved"

        # A's map should reference B and vice versa.
        assert name_b in set(map_a.values()), f"{name_a}'s gear map should reference {name_b}"
        assert name_a in set(map_b.values()), f"{name_b}'s gear map should reference {name_a}"


# ---------------------------------------------------------------------------
# 3. TestCategoryInference
# ---------------------------------------------------------------------------

class TestCategoryInference:
    """Test infer_equipment_categories() on realistic free-text snippets."""

    def test_hot_saw_op(self):
        """'Hot Saw, OP' should map to chainsaw (OP is not equipment category)."""
        cats = infer_equipment_categories('Hot Saw, OP')
        assert 'chainsaw' in cats

    def test_single_buck(self):
        cats = infer_equipment_categories('single buck')
        assert 'crosscut' in cats

    def test_springboard(self):
        cats = infer_equipment_categories('springboard')
        assert 'springboard' in cats

    def test_sb_does_not_infer_springboard(self):
        """Abbreviation 'SB' alone should NOT trigger springboard (too ambiguous)."""
        cats = infer_equipment_categories('SB')
        assert 'springboard' not in cats

    def test_uh_sb_combined(self):
        """'UH, SB' contains no category keywords (abbreviations only)."""
        cats = infer_equipment_categories('UH, SB')
        # Neither 'underhand' nor 'standing block' is a category key returned
        # by infer_equipment_categories.
        assert len(cats) == 0

    def test_double_buck_jack_jill(self):
        cats = infer_equipment_categories('Double Buck, Jack & Jill')
        assert 'crosscut' in cats

    def test_hot_saw_only(self):
        cats = infer_equipment_categories('Hot Saw')
        assert cats == {'chainsaw'}

    def test_empty_string(self):
        assert infer_equipment_categories('') == set()

    def test_no_match_returns_empty(self):
        # "axe throw" maps to no equipment category (axes are personal, not shared).
        assert infer_equipment_categories('axe throw') == set()

    def test_obstacle_pole_emits_op_saw(self):
        assert infer_equipment_categories('obstacle pole') == {'op_saw'}

    def test_op_saw_emits_op_saw(self):
        assert infer_equipment_categories('OP Saw') == {'op_saw'}

    def test_cookie_stack_emits_cookie_stack(self):
        assert infer_equipment_categories('cookie stack') == {'cookie_stack'}

    def test_cookie_saw_emits_cookie_stack(self):
        assert infer_equipment_categories('cookie saw') == {'cookie_stack'}

    def test_speed_climb_emits_climbing(self):
        assert infer_equipment_categories('speed climb') == {'climbing'}

    def test_pole_climb_emits_climbing(self):
        assert infer_equipment_categories('pole climb') == {'climbing'}

    def test_multiple_categories(self):
        cats = infer_equipment_categories('single buck and hot saw and springboard')
        assert 'crosscut' in cats
        assert 'chainsaw' in cats
        assert 'springboard' in cats

    def test_hand_saw_keyword(self):
        cats = infer_equipment_categories('hand saw')
        assert 'crosscut' in cats

    def test_board_keyword(self):
        """The word 'board' triggers springboard category."""
        cats = infer_equipment_categories('1-board')
        assert 'springboard' in cats


# ---------------------------------------------------------------------------
# 4. TestNameResolution
# ---------------------------------------------------------------------------

class TestNameResolution:
    """Test resolve_partner_name() with exact, fuzzy, and last-name-only inputs."""

    def test_exact_match(self):
        result = resolve_partner_name('Imortal Joe', NAME_INDEX)
        assert result == 'Imortal Joe'

    def test_exact_match_case_insensitive(self):
        result = resolve_partner_name('imortal joe', NAME_INDEX)
        assert result == 'Imortal Joe'

    def test_exact_match_extra_spaces(self):
        result = resolve_partner_name('  Imortal  Joe  ', NAME_INDEX)
        # normalize_person_name strips spaces, so exact normalized match works.
        assert result == 'Imortal Joe'

    def test_last_name_only_unambiguous(self):
        """'Cambium' should resolve to 'Ben Cambium' (unique last name)."""
        result = resolve_partner_name('Cambium', NAME_INDEX)
        assert result == 'Ben Cambium'

    def test_last_name_only_ambiguous_returns_raw(self):
        """If multiple people share a last name, last-name fallback should not resolve."""
        # Build an index with two people sharing the last name 'Smith'.
        idx = build_name_index(['Alice Smith', 'Bob Smith', 'Carol Davis'])
        result = resolve_partner_name('Smith', idx)
        # Should return the raw input since it is ambiguous.
        assert result == 'Smith'

    def test_fuzzy_match_close_spelling(self):
        """A slight misspelling should still resolve via difflib."""
        result = resolve_partner_name('Imortol Joe', NAME_INDEX)
        assert result == 'Imortal Joe'

    def test_no_match_returns_raw(self):
        result = resolve_partner_name('Nonexistent Person', NAME_INDEX)
        assert result == 'Nonexistent Person'

    def test_empty_string(self):
        result = resolve_partner_name('', NAME_INDEX)
        assert result == ''

    def test_first_initial_last_name(self):
        """'B. Cambium' should match 'Ben Cambium'."""
        result = resolve_partner_name('B. Cambium', NAME_INDEX)
        assert result == 'Ben Cambium'

    def test_first_initial_no_dot(self):
        """'B Cambium' should match 'Ben Cambium'."""
        result = resolve_partner_name('B Cambium', NAME_INDEX)
        assert result == 'Ben Cambium'

    def test_normalize_strips_punctuation(self):
        assert normalize_person_name("O'Brien") == 'obrien'
        assert normalize_person_name('Mc-Donald') == 'mcdonald'


# ---------------------------------------------------------------------------
# 5. TestCollegeGearNotes
# ---------------------------------------------------------------------------

class TestCollegeGearNotes:
    """Test that free-text notes about equipment map to categories."""

    def test_crosscut_note(self):
        cats = infer_equipment_categories('We only have one crosscut')
        assert 'crosscut' in cats

    def test_chainsaw_note(self):
        cats = infer_equipment_categories('sharing a chainsaw with my partner')
        assert 'chainsaw' in cats

    def test_hot_saw_note(self):
        cats = infer_equipment_categories('We share a hot saw between us')
        assert 'chainsaw' in cats

    def test_springboard_note(self):
        cats = infer_equipment_categories('Only one springboard between us')
        assert 'springboard' in cats

    def test_jack_and_jill_note(self):
        cats = infer_equipment_categories('Sharing jack and jill saw')
        assert 'crosscut' in cats

    def test_double_buck_note(self):
        cats = infer_equipment_categories('Same double buck saw for both events')
        assert 'crosscut' in cats

    def test_power_saw_note(self):
        cats = infer_equipment_categories('We share a power saw')
        assert 'chainsaw' in cats

    def test_handsaw_note(self):
        cats = infer_equipment_categories('We share a handsaw')
        assert 'crosscut' in cats

    def test_unrelated_note(self):
        """Text without equipment keywords should return empty."""
        cats = infer_equipment_categories('Please put us in different heats')
        assert len(cats) == 0
