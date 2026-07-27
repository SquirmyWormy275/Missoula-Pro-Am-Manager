"""
Unit tests for pure helper functions in services/pro_entry_importer.py.

Covers _yes(), _get(), _find_column_index(), _EVENT_MAP coverage and fee
correctness, _PARTNER_COLS mapping, _TRUE_MARKERS set, compute_review_flags(),
and the critical check that _EVENT_MAP canonical names match Event.display_name
values that would be generated from config.PRO_EVENTS.

Run:  pytest tests/test_pro_entry_importer.py -v
"""
import pytest

from services.pro_entry_importer import (
    _EVENT_MAP,
    _PARTNER_COLS,
    _TRUE_MARKERS,
    _find_column_index,
    _get,
    _yes,
    compute_review_flags,
)

# ---------------------------------------------------------------------------
# _yes() helper
# ---------------------------------------------------------------------------

class TestYes:
    def test_yes_lowercase(self):
        assert _yes('yes') is True

    def test_yes_uppercase(self):
        assert _yes('YES') is True

    def test_yes_mixed_case(self):
        assert _yes('Yes') is True

    def test_yes_with_whitespace(self):
        assert _yes('  yes  ') is True

    def test_no_returns_false(self):
        assert _yes('No') is False

    def test_no_lowercase(self):
        assert _yes('no') is False

    def test_empty_string_returns_false(self):
        assert _yes('') is False

    def test_none_returns_false(self):
        assert _yes(None) is False

    def test_true_string_returns_false(self):
        """_yes() only accepts 'yes', not 'true'."""
        assert _yes('true') is False

    def test_one_string_returns_false(self):
        """_yes() only accepts 'yes', not '1'."""
        assert _yes('1') is False

    def test_x_returns_false(self):
        assert _yes('x') is False

    def test_y_returns_false(self):
        """_yes() only accepts 'yes', not 'y'."""
        assert _yes('y') is False

    def test_integer_returns_false(self):
        assert _yes(1) is False

    def test_boolean_true_returns_false(self):
        assert _yes(True) is False

    def test_boolean_false_returns_false(self):
        assert _yes(False) is False

    def test_random_string_returns_false(self):
        assert _yes('maybe') is False


# ---------------------------------------------------------------------------
# _get() helper
# ---------------------------------------------------------------------------

class TestGet:
    def test_valid_index(self):
        row = ('a', 'b', 'c')
        assert _get(row, 1) == 'b'

    def test_first_index(self):
        row = ('first', 'second')
        assert _get(row, 0) == 'first'

    def test_last_index(self):
        row = ('a', 'b', 'c')
        assert _get(row, 2) == 'c'

    def test_none_col_returns_none(self):
        row = ('a', 'b')
        assert _get(row, None) is None

    def test_out_of_range_returns_none(self):
        row = ('a', 'b')
        assert _get(row, 5) is None

    def test_empty_row_returns_none(self):
        row = ()
        assert _get(row, 0) is None

    def test_zero_index_on_single_element(self):
        row = ('only',)
        assert _get(row, 0) == 'only'

    def test_returns_none_value_when_cell_is_none(self):
        row = ('a', None, 'c')
        assert _get(row, 1) is None


# ---------------------------------------------------------------------------
# _find_column_index()
# ---------------------------------------------------------------------------

class TestFindColumnIndex:
    def test_exact_match(self):
        headers = ['Timestamp', 'Full Name', 'Gender']
        assert _find_column_index(headers, ['Full Name']) == 1

    def test_case_insensitive_match(self):
        headers = ['Timestamp', 'full name', 'Gender']
        assert _find_column_index(headers, ['Full Name']) == 1

    def test_contains_match_fallback(self):
        headers = ['Timestamp', 'Springboard Slow Heat Preference', 'Gender']
        assert _find_column_index(headers, ['springboard slow heat']) == 1

    def test_first_candidate_wins(self):
        headers = ['slow heat springboard', 'springboard slow heat']
        result = _find_column_index(headers, ['springboard slow heat', 'slow heat springboard'])
        assert result == 1  # exact match for first candidate

    def test_no_match_returns_none(self):
        headers = ['Timestamp', 'Full Name']
        assert _find_column_index(headers, ['Nonexistent Column']) is None

    def test_empty_headers(self):
        assert _find_column_index([], ['Full Name']) is None

    def test_empty_candidates(self):
        headers = ['Full Name']
        assert _find_column_index(headers, []) is None

    def test_none_in_headers_handled(self):
        headers = [None, 'Full Name', '']
        assert _find_column_index(headers, ['Full Name']) == 1

    def test_empty_string_candidates_ignored(self):
        headers = ['Full Name']
        assert _find_column_index(headers, ['', '  ', 'Full Name']) == 0


# ---------------------------------------------------------------------------
# _TRUE_MARKERS
# ---------------------------------------------------------------------------

class TestTrueMarkers:
    def test_expected_markers_present(self):
        for marker in ['yes', 'y', 'true', '1', 'x']:
            assert marker in _TRUE_MARKERS, f"'{marker}' should be in _TRUE_MARKERS"

    def test_no_is_not_a_marker(self):
        assert 'no' not in _TRUE_MARKERS

    def test_empty_is_not_a_marker(self):
        assert '' not in _TRUE_MARKERS


# ---------------------------------------------------------------------------
# _EVENT_MAP coverage
# ---------------------------------------------------------------------------

class TestEventMap:
    """Verify all expected form headers are present and map correctly."""

    def test_springboard_left_maps_to_springboard(self):
        assert _EVENT_MAP['Springboard (L)'] == ('Springboard', 10)

    def test_springboard_right_maps_to_springboard(self):
        assert _EVENT_MAP['Springboard (R)'] == ('Springboard', 10)

    def test_intermediate_1board_maps_to_pro_1board(self):
        assert _EVENT_MAP['Intermediate 1-Board Springboard'] == ('Pro 1-Board', 10)

    def test_1board_springboard_maps_to_pro_1board(self):
        assert _EVENT_MAP['1-Board Springboard'] == ('Pro 1-Board', 10)

    def test_pro_1board_maps_to_pro_1board(self):
        assert _EVENT_MAP['Pro 1-Board'] == ('Pro 1-Board', 10)

    def test_mens_underhand(self):
        assert _EVENT_MAP["Men's Underhand"] == ("Men's Underhand", 10)

    def test_womens_underhand(self):
        assert _EVENT_MAP["Women's Underhand"] == ("Women's Underhand", 10)

    def test_womens_standing_block(self):
        assert _EVENT_MAP["Women's Standing Block"] == ("Women's Standing Block", 10)

    def test_mens_standing_block(self):
        assert _EVENT_MAP["Men's Standing Block"] == ("Men's Standing Block", 10)

    def test_mens_single_buck(self):
        assert _EVENT_MAP["Men's Single Buck"] == ("Men's Single Buck", 5)

    def test_womens_single_buck(self):
        assert _EVENT_MAP["Women's Single Buck"] == ("Women's Single Buck", 5)

    def test_mens_double_buck(self):
        assert _EVENT_MAP["Men's Double Buck"] == ("Men's Double Buck", 5)

    def test_womens_double_buck(self):
        assert _EVENT_MAP["Women's Double Buck"] == ("Women's Double Buck", 5)

    def test_double_buck_alias(self):
        assert _EVENT_MAP['Double Buck'] == ("Men's Double Buck", 5)

    def test_jack_and_jill(self):
        assert _EVENT_MAP['Jack & Jill'] == ('Jack & Jill Sawing', 5)

    def test_jack_and_jill_sawing(self):
        assert _EVENT_MAP['Jack & Jill Sawing'] == ('Jack & Jill Sawing', 5)

    def test_jack_jill_alias(self):
        assert _EVENT_MAP['Jack Jill'] == ('Jack & Jill Sawing', 5)

    def test_hot_saw(self):
        assert _EVENT_MAP['Hot Saw'] == ('Hot Saw', 5)

    def test_obstacle_pole(self):
        assert _EVENT_MAP['Obstacle Pole'] == ('Obstacle Pole', 5)

    def test_speed_climb_maps_to_pole_climb(self):
        assert _EVENT_MAP['Speed Climb'] == ('Pole Climb', 5)

    def test_pole_climb(self):
        assert _EVENT_MAP['Pole Climb'] == ('Pole Climb', 5)

    def test_cookie_stack(self):
        assert _EVENT_MAP['Cookie Stack'] == ('Cookie Stack', 5)

    def test_3board_jigger_hyphen(self):
        assert _EVENT_MAP['3-Board Jigger'] == ('3-Board Jigger', 5)

    def test_3board_jigger_space(self):
        assert _EVENT_MAP['3 Board Jigger'] == ('3-Board Jigger', 5)

    def test_partnered_axe_throw(self):
        assert _EVENT_MAP['Partnered Axe Throw'] == ('Partnered Axe Throw', 5)

    def test_axe_throw_alias(self):
        assert _EVENT_MAP['Axe Throw'] == ('Partnered Axe Throw', 5)

    def test_mens_stock_saw(self):
        assert _EVENT_MAP["Men's Stock Saw"] == ("Men's Stock Saw", 5)

    def test_womens_stock_saw(self):
        assert _EVENT_MAP["Women's Stock Saw"] == ("Women's Stock Saw", 5)

    def test_stock_saw_alias(self):
        assert _EVENT_MAP['Stock Saw'] == ("Men's Stock Saw", 5)


# ---------------------------------------------------------------------------
# _EVENT_MAP fees
# ---------------------------------------------------------------------------

class TestEventFees:
    """Verify fee amounts are correct for each event category."""

    def test_chopping_events_cost_10(self):
        """Springboard, 1-Board, Underhand, Standing Block should be $10."""
        chopping_canonical = {'Springboard', 'Pro 1-Board',
                              "Men's Underhand", "Women's Underhand",
                              "Men's Standing Block", "Women's Standing Block"}
        for header, (canonical, fee) in _EVENT_MAP.items():
            if canonical in chopping_canonical:
                assert fee == 10, f"'{header}' -> '{canonical}' should cost $10, got ${fee}"

    def test_other_events_cost_5(self):
        """Non-chopping events should be $5."""
        chopping_canonical = {'Springboard', 'Pro 1-Board',
                              "Men's Underhand", "Women's Underhand",
                              "Men's Standing Block", "Women's Standing Block"}
        for header, (canonical, fee) in _EVENT_MAP.items():
            if canonical not in chopping_canonical:
                assert fee == 5, f"'{header}' -> '{canonical}' should cost $5, got ${fee}"


# ---------------------------------------------------------------------------
# _EVENT_MAP canonical names vs Event.display_name
# ---------------------------------------------------------------------------

class TestEventMapMatchesDisplayNames:
    """
    The canonical event names in _EVENT_MAP must match Event.display_name
    values that the app generates from config.PRO_EVENTS.

    For gendered events, config has e.g. {'name': 'Standing Block', 'is_gendered': True}
    and Event.display_name prepends "Men's" / "Women's".  Non-gendered events
    use the base name directly.

    This test catches the class of bug where _EVENT_MAP has a canonical name
    like "Women's Standing Block" that doesn't match the generated display_name.
    """

    def _build_expected_display_names(self):
        """Build the set of display names the app would generate from config."""
        from config import PRO_EVENTS
        names = set()
        for evt_cfg in PRO_EVENTS:
            base_name = evt_cfg['name']
            if evt_cfg.get('is_gendered'):
                names.add(f"Men's {base_name}")
                names.add(f"Women's {base_name}")
            else:
                names.add(base_name)
        return names

    def test_all_canonical_names_are_valid_display_names(self):
        expected = self._build_expected_display_names()
        canonical_names = {canonical for _, (canonical, _) in _EVENT_MAP.items()}
        for name in canonical_names:
            assert name in expected, (
                f"_EVENT_MAP canonical name '{name}' does not match any "
                f"Event.display_name from config.PRO_EVENTS. "
                f"Valid names: {sorted(expected)}"
            )

    def test_gendered_underhand_names_match(self):
        """Verify Men's/Women's Underhand in _EVENT_MAP match display_name."""
        expected = self._build_expected_display_names()
        assert "Men's Underhand" in expected
        assert "Women's Underhand" in expected
        assert _EVENT_MAP["Men's Underhand"][0] == "Men's Underhand"
        assert _EVENT_MAP["Women's Underhand"][0] == "Women's Underhand"

    def test_gendered_standing_block_names_match(self):
        """Verify Men's/Women's Standing Block in _EVENT_MAP match display_name."""
        expected = self._build_expected_display_names()
        assert "Men's Standing Block" in expected
        assert "Women's Standing Block" in expected
        assert _EVENT_MAP["Men's Standing Block"][0] == "Men's Standing Block"
        assert _EVENT_MAP["Women's Standing Block"][0] == "Women's Standing Block"

    def test_gendered_stock_saw_names_match(self):
        expected = self._build_expected_display_names()
        assert "Men's Stock Saw" in expected
        assert "Women's Stock Saw" in expected
        assert _EVENT_MAP["Men's Stock Saw"][0] == "Men's Stock Saw"
        assert _EVENT_MAP["Women's Stock Saw"][0] == "Women's Stock Saw"

    def test_gendered_single_buck_names_match(self):
        expected = self._build_expected_display_names()
        assert "Men's Single Buck" in expected
        assert "Women's Single Buck" in expected

    def test_gendered_double_buck_names_match(self):
        expected = self._build_expected_display_names()
        assert "Men's Double Buck" in expected
        assert "Women's Double Buck" in expected

    def test_non_gendered_events_match(self):
        expected = self._build_expected_display_names()
        for name in ['Springboard', 'Pro 1-Board', '3-Board Jigger',
                      'Hot Saw', 'Obstacle Pole', 'Pole Climb',
                      'Cookie Stack', 'Partnered Axe Throw', 'Jack & Jill Sawing']:
            assert name in expected, f"'{name}' not found in expected display names"


# ---------------------------------------------------------------------------
# _PARTNER_COLS
# ---------------------------------------------------------------------------

class TestPartnerCols:
    def test_double_buck_partner(self):
        assert _PARTNER_COLS["men's double buck partner name"] == "Men's Double Buck"

    def test_jack_and_jill_partner(self):
        assert _PARTNER_COLS["jack & jill partner name"] == "Jack & Jill Sawing"

    def test_partnered_axe_throw(self):
        assert _PARTNER_COLS["partnered axe throw 2"] == "Partnered Axe Throw"

    def test_all_keys_are_lowercase(self):
        for key in _PARTNER_COLS:
            assert key == key.lower(), f"Key '{key}' should be lowercase"


# ---------------------------------------------------------------------------
# compute_review_flags()
# ---------------------------------------------------------------------------

def _make_entry(**overrides):
    """Create a minimal entry dict with sensible defaults."""
    base = {
        'name': 'John Doe',
        'email': 'john@example.com',
        'gender': 'M',
        'events': ["Men's Standing Block"],
        'partners': {},
        'gear_sharing': False,
        'gear_sharing_details': None,
        'waiver_accepted': True,
        'waiver_signature': 'John Doe',
        'notes': None,
    }
    base.update(overrides)
    return base


class TestComputeReviewFlagsNoWaiver:
    def test_no_waiver_flags_danger(self):
        entry = _make_entry(waiver_accepted=False)
        result = compute_review_flags([entry])
        assert 'NO WAIVER' in result[0]['flags']
        assert result[0]['flag_class'] == 'table-danger'

    def test_waiver_accepted_no_flag(self):
        entry = _make_entry(waiver_accepted=True)
        result = compute_review_flags([entry])
        assert 'NO WAIVER' not in result[0]['flags']


class TestComputeReviewFlagsPartnerNotFound:
    def test_partner_in_batch_no_flag(self):
        e1 = _make_entry(name='Alice Smith')
        e2 = _make_entry(
            name='Bob Jones',
            partners={"Men's Double Buck": 'Alice Smith'}
        )
        result = compute_review_flags([e1, e2])
        bob_flags = result[1]['flags']
        assert not any('PARTNER NOT FOUND' in f for f in bob_flags)

    def test_partner_not_in_batch_flags_warning(self):
        entry = _make_entry(
            partners={"Men's Double Buck": 'Missing Person'}
        )
        result = compute_review_flags([entry])
        assert any('PARTNER NOT FOUND: Missing Person' in f for f in result[0]['flags'])
        # Should be warning unless overridden by danger
        assert result[0]['flag_class'] in ('table-warning', 'table-danger')

    def test_partner_match_is_case_insensitive(self):
        e1 = _make_entry(name='alice smith')
        e2 = _make_entry(
            name='Bob Jones',
            partners={"Men's Double Buck": 'Alice Smith'}
        )
        result = compute_review_flags([e1, e2])
        bob_flags = result[1]['flags']
        assert not any('PARTNER NOT FOUND' in f for f in bob_flags)


class TestComputeReviewFlagsGearSharing:
    def test_gear_sharing_without_details_flags_warning(self):
        entry = _make_entry(gear_sharing=True, gear_sharing_details=None)
        result = compute_review_flags([entry])
        assert 'GEAR SHARING DETAILS MISSING' in result[0]['flags']

    def test_gear_sharing_with_details_no_missing_flag(self):
        entry = _make_entry(
            gear_sharing=True,
            gear_sharing_details='Sharing springboard with Jane Doe'
        )
        result = compute_review_flags([entry])
        assert 'GEAR SHARING DETAILS MISSING' not in result[0]['flags']

    def test_gear_sharing_false_no_flag(self):
        entry = _make_entry(gear_sharing=False, gear_sharing_details=None)
        result = compute_review_flags([entry])
        assert 'GEAR SHARING DETAILS MISSING' not in result[0]['flags']

    def test_ambiguous_gear_details_flags_warning(self):
        """Details with no equipment category signal get flagged."""
        entry = _make_entry(
            gear_sharing=True,
            gear_sharing_details='yes I share'
        )
        result = compute_review_flags([entry])
        assert any('AMBIGUOUS' in f for f in result[0]['flags'])


class TestComputeReviewFlagsDuplicate:
    def test_duplicate_against_existing_names(self):
        entry = _make_entry(name='John Doe')
        result = compute_review_flags([entry], existing_names=['John Doe'])
        assert any('POSSIBLE DUPLICATE' in f for f in result[0]['flags'])

    def test_close_match_flags_duplicate(self):
        entry = _make_entry(name='Jon Doe')
        result = compute_review_flags([entry], existing_names=['John Doe'])
        assert any('POSSIBLE DUPLICATE' in f for f in result[0]['flags'])

    def test_no_existing_names_no_duplicate_flag(self):
        entry = _make_entry(name='John Doe')
        result = compute_review_flags([entry], existing_names=None)
        assert not any('POSSIBLE DUPLICATE' in f for f in result[0]['flags'])

    def test_completely_different_name_no_duplicate(self):
        entry = _make_entry(name='Xyz Abc')
        result = compute_review_flags([entry], existing_names=['John Doe'])
        assert not any('POSSIBLE DUPLICATE' in f for f in result[0]['flags'])

    def test_duplicate_preserves_original_name_casing(self):
        entry = _make_entry(name='john doe')
        result = compute_review_flags([entry], existing_names=['John Doe'])
        dup_flags = [f for f in result[0]['flags'] if 'POSSIBLE DUPLICATE' in f]
        if dup_flags:
            assert 'John Doe' in dup_flags[0]


class TestComputeReviewFlagsPriority:
    """Danger class (no waiver) should take precedence over warning class."""

    def test_no_waiver_overrides_partner_warning(self):
        entry = _make_entry(
            waiver_accepted=False,
            partners={"Men's Double Buck": 'Missing Person'}
        )
        result = compute_review_flags([entry])
        assert result[0]['flag_class'] == 'table-danger'
        assert 'NO WAIVER' in result[0]['flags']
        assert any('PARTNER NOT FOUND' in f for f in result[0]['flags'])

    def test_clean_entry_no_flags(self):
        entry = _make_entry()
        result = compute_review_flags([entry])
        assert result[0]['flags'] == []
        assert result[0]['flag_class'] == ''


class TestComputeReviewFlagsMultipleEntries:
    def test_flags_applied_independently(self):
        e1 = _make_entry(name='Good Person', waiver_accepted=True)
        e2 = _make_entry(name='Bad Person', waiver_accepted=False)
        result = compute_review_flags([e1, e2])
        assert result[0]['flags'] == []
        assert 'NO WAIVER' in result[1]['flags']

    def test_returns_same_list(self):
        entries = [_make_entry()]
        result = compute_review_flags(entries)
        assert result is entries

    def test_empty_list_returns_empty(self):
        result = compute_review_flags([])
        assert result == []
