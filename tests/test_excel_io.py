"""
Unit tests for pure helper functions in services/excel_io.py.

No database required. Only stateless string/pandas helpers are tested.
DB-dependent functions (process_college_entry_form, export_results_to_excel,
_validate_college_entry_constraints, _generate_team_code) are excluded.

Run:  pytest tests/test_excel_io.py -v
"""
import pandas as pd
import pytest

from services.excel_io import (
    _abbreviate_school,
    _canonicalize_event_name,
    _event_column_gender_hint,
    _infer_default_gender,
    _is_valid_competitor_name,
    _looks_like_team_code,
    _normalize_label,
    _normalize_person_name,
    _parse_event_markers,
    _parse_events,
    _parse_gender,
    _parse_relay_opt_in,
)

# ---------------------------------------------------------------------------
# _normalize_label
# ---------------------------------------------------------------------------

class TestNormalizeLabel:
    def test_strips_whitespace(self):
        assert _normalize_label('  Name  ') == 'name'

    def test_lowercases(self):
        assert _normalize_label('School Name') == 'school name'

    def test_replaces_special_chars_with_space(self):
        assert _normalize_label('M/F') == 'm f'

    def test_collapses_multiple_spaces(self):
        assert _normalize_label('First   Last') == 'first last'

    def test_none_returns_empty_string(self):
        assert _normalize_label(None) == ''

    def test_empty_string_returns_empty(self):
        assert _normalize_label('') == ''

    def test_numbers_preserved(self):
        result = _normalize_label('Team1')
        assert 'team' in result and '1' in result

    def test_unicode_stripped_to_alphanum(self):
        # Non-ascii letters become spaces
        result = _normalize_label('André')
        assert 'andr' in result or result == 'andr'


# ---------------------------------------------------------------------------
# _looks_like_team_code
# ---------------------------------------------------------------------------

class TestLooksLikeTeamCode:
    def test_standard_two_letter_code(self):
        assert _looks_like_team_code('UM-A') is True

    def test_three_letter_code(self):
        assert _looks_like_team_code('MSU-B') is True

    def test_longer_prefix(self):
        # Regex allows 2–6 alpha chars before the separator: STRATH (6) is the max
        assert _looks_like_team_code('STRATH-A') is True

    def test_number_suffix(self):
        assert _looks_like_team_code('UM-1') is True

    def test_space_separator(self):
        assert _looks_like_team_code('UM A') is True

    def test_plain_school_name_not_a_code(self):
        assert _looks_like_team_code('University of Montana') is False

    def test_single_word_not_a_code(self):
        assert _looks_like_team_code('Montana') is False

    def test_empty_string_not_a_code(self):
        assert _looks_like_team_code('') is False

    def test_leading_whitespace_ignored(self):
        # strip() is applied inside the function
        assert _looks_like_team_code('  UM-A  ') is True


# ---------------------------------------------------------------------------
# _parse_gender
# ---------------------------------------------------------------------------

class TestParseGender:
    def test_f_returns_f(self):
        assert _parse_gender('F') == 'F'

    def test_female_returns_f(self):
        assert _parse_gender('FEMALE') == 'F'

    def test_w_returns_f(self):
        assert _parse_gender('W') == 'F'

    def test_woman_returns_f(self):
        assert _parse_gender('WOMAN') == 'F'

    def test_women_returns_f(self):
        assert _parse_gender('WOMEN') == 'F'

    def test_lowercase_female_returns_f(self):
        assert _parse_gender('female') == 'F'

    def test_m_returns_m(self):
        assert _parse_gender('M') == 'M'

    def test_male_returns_m(self):
        assert _parse_gender('MALE') == 'M'

    def test_unknown_defaults_to_m(self):
        assert _parse_gender('X') == 'M'

    def test_nan_defaults_to_m(self):
        assert _parse_gender(float('nan')) == 'M'

    def test_none_treated_as_nan_returns_m(self):
        # pd.isna(None) is True → returns 'M'
        assert _parse_gender(None) == 'M'


# ---------------------------------------------------------------------------
# _parse_relay_opt_in
# ---------------------------------------------------------------------------

class TestParseRelayOptIn:
    def test_x_is_true(self):
        assert _parse_relay_opt_in('x') is True

    def test_y_is_true(self):
        assert _parse_relay_opt_in('y') is True

    def test_yes_is_true(self):
        assert _parse_relay_opt_in('yes') is True

    def test_one_is_true(self):
        assert _parse_relay_opt_in('1') is True

    def test_true_is_true(self):
        assert _parse_relay_opt_in('true') is True

    def test_t_is_true(self):
        assert _parse_relay_opt_in('t') is True

    def test_uppercase_x_is_true(self):
        assert _parse_relay_opt_in('X') is True

    def test_no_is_false(self):
        assert _parse_relay_opt_in('no') is False

    def test_empty_string_is_false(self):
        assert _parse_relay_opt_in('') is False

    def test_nan_is_false(self):
        assert _parse_relay_opt_in(float('nan')) is False

    def test_none_is_false(self):
        assert _parse_relay_opt_in(None) is False

    def test_zero_is_false(self):
        assert _parse_relay_opt_in('0') is False


# ---------------------------------------------------------------------------
# _abbreviate_school
# ---------------------------------------------------------------------------

class TestAbbreviateSchool:
    def test_university_of_montana(self):
        assert _abbreviate_school('University of Montana') == 'UM'

    def test_montana_state_university(self):
        assert _abbreviate_school('Montana State University') == 'MSU'

    def test_colorado_state_university(self):
        assert _abbreviate_school('Colorado State University') == 'CSU'

    def test_university_of_idaho(self):
        assert _abbreviate_school('University of Idaho') == 'UI'

    def test_oregon_state_university(self):
        assert _abbreviate_school('Oregon State University') == 'OSU'

    def test_university_of_washington(self):
        assert _abbreviate_school('University of Washington') == 'UW'

    def test_humboldt_state(self):
        assert _abbreviate_school('Humboldt State') == 'HSU'

    def test_cal_poly(self):
        assert _abbreviate_school('Cal Poly') == 'CP'

    def test_unknown_two_word_school_gets_initials(self):
        # "Lake Superior" → 'LS'
        assert _abbreviate_school('Lake Superior') == 'LS'

    def test_of_and_the_skipped_in_initials(self):
        # "University of Montana" not in dict → initials skipping 'of': 'UM'
        # But a different school: "State of Maine" → 'SM'
        result = _abbreviate_school('State of Maine')
        assert result == 'SM'

    def test_single_word_school_truncated_to_3(self):
        assert _abbreviate_school('Forestry') == 'FOR'

    def test_case_insensitive_lookup(self):
        # The dict lookup normalizes to lowercase
        assert _abbreviate_school('university of montana') == 'UM'


# ---------------------------------------------------------------------------
# _canonicalize_event_name
# ---------------------------------------------------------------------------

class TestCanonicalizeEventName:
    def test_jack_and_jill(self):
        assert _canonicalize_event_name('Jack & Jill Sawing') == 'Jack & Jill Sawing'

    def test_jack_jill_lowercase(self):
        assert _canonicalize_event_name('jack jill') == 'Jack & Jill Sawing'

    def test_double_buck(self):
        assert _canonicalize_event_name('Double Buck') == 'Double Buck'

    def test_single_buck(self):
        assert _canonicalize_event_name('Single Buck') == 'Single Buck'

    def test_stock_saw(self):
        assert _canonicalize_event_name('Stock Saw') == 'Stock Saw'

    def test_power_saw_maps_to_stock_saw(self):
        assert _canonicalize_event_name('Power Saw') == 'Stock Saw'

    def test_obstacle_pole(self):
        assert _canonicalize_event_name('Obstacle Pole') == 'Obstacle Pole'

    def test_choker_maps_to_chokermans(self):
        assert _canonicalize_event_name('Choker Race') == "Chokerman's Race"

    def test_climb_maps_to_speed_climb(self):
        assert _canonicalize_event_name('Speed Climb') == 'Speed Climb'

    def test_birling(self):
        assert _canonicalize_event_name('Birling') == 'Birling'

    def test_kaber_toss(self):
        assert _canonicalize_event_name('Kaber Toss') == 'Caber Toss'

    def test_caber_toss(self):
        assert _canonicalize_event_name('Caber Toss') == 'Caber Toss'

    def test_axe_throw(self):
        assert _canonicalize_event_name('Axe Throw') == 'Axe Throw'

    def test_pulp_toss(self):
        assert _canonicalize_event_name('Pulp Toss') == 'Pulp Toss'

    def test_peavey_log_roll(self):
        assert _canonicalize_event_name('Peavey Log Roll') == 'Peavey Log Roll'

    def test_underhand_hard_hit_horizontal(self):
        result = _canonicalize_event_name('Horizontal H Hit')
        assert result == 'Underhand Hard Hit'

    def test_underhand_speed_horizontal(self):
        result = _canonicalize_event_name('Horizontal Speed')
        assert result == 'Underhand Speed'

    def test_standing_block_hard_hit(self):
        result = _canonicalize_event_name('Vertical H Hit')
        assert result == 'Standing Block Hard Hit'

    def test_standing_block_speed(self):
        result = _canonicalize_event_name('Vertical Speed')
        assert result == 'Standing Block Speed'

    def test_springboard_1_board(self):
        result = _canonicalize_event_name('1 Board Springboard')
        assert result == '1-Board Springboard'

    def test_unknown_name_returned_as_is(self):
        result = _canonicalize_event_name('Log Roll Relay')
        assert result == 'Log Roll Relay'


# ---------------------------------------------------------------------------
# _is_valid_competitor_name
# ---------------------------------------------------------------------------

class TestIsValidCompetitorName:
    def test_real_name_is_valid(self):
        assert _is_valid_competitor_name('Alice Smith') is True

    def test_nan_is_invalid(self):
        assert _is_valid_competitor_name(float('nan')) is False

    def test_empty_string_is_invalid(self):
        assert _is_valid_competitor_name('') is False

    def test_whitespace_only_is_invalid(self):
        assert _is_valid_competitor_name('   ') is False

    def test_gear_shared_marker_is_invalid(self):
        assert _is_valid_competitor_name('Gear Being Shared: crosscut') is False

    def test_a_team_marker_is_invalid(self):
        assert _is_valid_competitor_name('A Team') is False

    def test_b_team_marker_is_invalid(self):
        assert _is_valid_competitor_name('B Team') is False

    def test_team_marker_is_invalid(self):
        assert _is_valid_competitor_name('Team') is False

    def test_pro_am_lottery_marker_is_invalid(self):
        assert _is_valid_competitor_name('Pro Am Lottery entry') is False

    def test_do_not_count_marker_is_invalid(self):
        assert _is_valid_competitor_name('Do Not Count This') is False


# ---------------------------------------------------------------------------
# _parse_events
# ---------------------------------------------------------------------------

class TestParseEvents:
    def test_comma_separated(self):
        result = _parse_events('Birling, Stock Saw')
        assert 'Birling' in result
        assert 'Stock Saw' in result

    def test_semicolon_separated(self):
        result = _parse_events('Birling; Axe Throw')
        assert 'Birling' in result
        assert 'Axe Throw' in result

    def test_newline_separated(self):
        result = _parse_events('Birling\nAxe Throw')
        assert 'Birling' in result
        assert 'Axe Throw' in result

    def test_returns_sorted_list(self):
        result = _parse_events('Stock Saw, Birling, Axe Throw')
        assert result == sorted(result)

    def test_deduplicates(self):
        result = _parse_events('Birling, Birling')
        assert result.count('Birling') == 1

    def test_nan_returns_empty(self):
        result = _parse_events(float('nan'))
        assert result == []

    def test_empty_string_returns_empty(self):
        result = _parse_events('')
        assert result == []

    def test_canonicalizes_names(self):
        # 'single buck' gets canonicalized to 'Single Buck'
        result = _parse_events('single buck')
        assert 'Single Buck' in result


# ---------------------------------------------------------------------------
# _parse_event_markers
# ---------------------------------------------------------------------------

class TestParseEventMarkers:
    def _row(self, data: dict) -> pd.Series:
        return pd.Series(data)

    def test_x_marks_event(self):
        row = self._row({'Speed Climb': 'x', 'Birling': ''})
        result = _parse_event_markers(row, ['Speed Climb', 'Birling'])
        assert 'Speed Climb' in result

    def test_yes_marks_event(self):
        row = self._row({'Birling': 'yes'})
        result = _parse_event_markers(row, ['Birling'])
        assert 'Birling' in result

    def test_1_marks_event(self):
        row = self._row({'Axe Throw': '1'})
        result = _parse_event_markers(row, ['Axe Throw'])
        assert 'Axe Throw' in result

    def test_nan_does_not_mark(self):
        row = pd.Series({'Birling': float('nan')})
        result = _parse_event_markers(row, ['Birling'])
        assert 'Birling' not in result

    def test_empty_string_does_not_mark(self):
        row = self._row({'Birling': ''})
        result = _parse_event_markers(row, ['Birling'])
        assert 'Birling' not in result

    def test_no_marker_columns_returns_empty(self):
        row = self._row({'Name': 'Alice'})
        result = _parse_event_markers(row, [])
        assert result == []

    def test_multiple_events_marked(self):
        row = self._row({'Birling': 'X', 'Axe Throw': 'Y', 'Stock Saw': 'N'})
        result = _parse_event_markers(row, ['Birling', 'Axe Throw', 'Stock Saw'])
        assert 'Birling' in result
        assert 'Axe Throw' in result
        assert 'Stock Saw' not in result


# ---------------------------------------------------------------------------
# _event_column_gender_hint
# ---------------------------------------------------------------------------

class TestEventColumnGenderHint:
    def test_w_prefix_returns_f(self):
        assert _event_column_gender_hint('W Speed Climb') == 'F'

    def test_women_prefix_returns_f(self):
        assert _event_column_gender_hint('Women Stock Saw') == 'F'

    def test_female_prefix_returns_f(self):
        assert _event_column_gender_hint('Female Birling') == 'F'

    def test_m_prefix_returns_m(self):
        assert _event_column_gender_hint('M Axe Throw') == 'M'

    def test_men_prefix_returns_m(self):
        assert _event_column_gender_hint('Men Underhand') == 'M'

    def test_male_prefix_returns_m(self):
        assert _event_column_gender_hint('Male Springboard') == 'M'

    def test_neutral_column_returns_none(self):
        assert _event_column_gender_hint('Stock Saw') is None

    def test_empty_returns_none(self):
        assert _event_column_gender_hint('') is None


# ---------------------------------------------------------------------------
# _normalize_person_name
# ---------------------------------------------------------------------------

class TestNormalizePersonName:
    def test_lowercases_and_strips_special_chars(self):
        assert _normalize_person_name('Alice Smith') == 'alicesmith'

    def test_removes_hyphens(self):
        assert _normalize_person_name('Mary-Jane') == 'maryjane'

    def test_removes_spaces(self):
        assert _normalize_person_name('Bob Jones') == 'bobjones'

    def test_handles_none(self):
        assert _normalize_person_name(None) == ''

    def test_handles_empty_string(self):
        assert _normalize_person_name('') == ''

    def test_two_names_match_after_normalization(self):
        n1 = _normalize_person_name('Alice Smith')
        n2 = _normalize_person_name('alice smith')
        assert n1 == n2


# ---------------------------------------------------------------------------
# _infer_default_gender
# ---------------------------------------------------------------------------

class TestInferDefaultGender:
    def test_gender_col_present_returns_m(self):
        df = pd.DataFrame({'Gender': ['M', 'F'], 'Name': ['A', 'B']})
        assert _infer_default_gender(df, gender_col='Gender') == 'M'

    def test_female_heavy_columns_returns_f(self):
        df = pd.DataFrame({
            'Women Speed Climb': [],
            'Women Birling': [],
            'Women Stock Saw': [],
            'Men Axe Throw': [],
        })
        assert _infer_default_gender(df, gender_col=None) == 'F'

    def test_male_heavy_columns_returns_m(self):
        df = pd.DataFrame({
            'Men Speed Climb': [],
            'Men Birling': [],
            'Women Axe Throw': [],
        })
        assert _infer_default_gender(df, gender_col=None) == 'M'

    def test_equal_markers_defaults_to_m(self):
        df = pd.DataFrame({
            'Women Speed Climb': [],
            'Men Birling': [],
        })
        # Equal count → defaults to 'M'
        assert _infer_default_gender(df, gender_col=None) == 'M'

    def test_no_gender_markers_defaults_to_m(self):
        df = pd.DataFrame({'Name': [], 'Events': []})
        assert _infer_default_gender(df, gender_col=None) == 'M'
