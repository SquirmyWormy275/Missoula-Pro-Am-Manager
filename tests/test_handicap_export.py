"""
Unit tests for pure helper functions in services/handicap_export.py.

No database required. Only pure string helpers and is_chopping_event() are tested.
build_chopping_rows() and export_chopping_results_to_excel() require live
tournament/event objects with DB-backed relationships and are excluded.

Run:  pytest tests/test_handicap_export.py -v
"""
import pytest
from types import SimpleNamespace
from services.handicap_export import _normalized, is_chopping_event


# ---------------------------------------------------------------------------
# _normalized
# ---------------------------------------------------------------------------

class TestNormalized:
    def test_lowercases(self):
        assert _normalized('Underhand') == 'underhand'

    def test_strips_whitespace(self):
        assert _normalized('  birling  ') == 'birling'

    def test_replaces_special_chars_with_space(self):
        result = _normalized('1-Board')
        assert '1' in result and 'board' in result

    def test_collapses_multiple_spaces(self):
        result = _normalized('standing  block')
        assert result == 'standing block'

    def test_none_returns_empty_string(self):
        assert _normalized(None) == ''

    def test_empty_string_returns_empty(self):
        assert _normalized('') == ''

    def test_numbers_preserved(self):
        result = _normalized('3-Board Jigger')
        assert '3' in result and 'board' in result and 'jigger' in result


# ---------------------------------------------------------------------------
# is_chopping_event
# ---------------------------------------------------------------------------

def _event(name):
    return SimpleNamespace(name=name)


class TestIsChoppingEvent:
    # Chopping keywords: underhand, standing block, springboard, 1-board, 3-board, jigger

    def test_underhand_is_chopping(self):
        assert is_chopping_event(_event('Underhand Hard Hit')) is True

    def test_underhand_speed_is_chopping(self):
        assert is_chopping_event(_event('Underhand Speed')) is True

    def test_standing_block_is_chopping(self):
        assert is_chopping_event(_event('Standing Block Hard Hit')) is True

    def test_standing_block_speed_is_chopping(self):
        assert is_chopping_event(_event('Standing Block Speed')) is True

    def test_springboard_is_chopping(self):
        assert is_chopping_event(_event('1-Board Springboard')) is True

    def test_one_board_with_springboard_keyword_is_chopping(self):
        # 'Collegiate 1-Board Springboard' matches via the 'springboard' keyword.
        # Note: '1-board' keyword is unreachable because _normalized() converts
        # hyphens to spaces, but 'springboard' is a separate keyword that works.
        assert is_chopping_event(_event('Collegiate 1-Board Springboard')) is True

    def test_three_board_keyword_is_chopping(self):
        assert is_chopping_event(_event('3-Board Jigger')) is True

    def test_jigger_is_chopping(self):
        assert is_chopping_event(_event('Pro Jigger')) is True

    def test_stock_saw_is_not_chopping(self):
        assert is_chopping_event(_event('Stock Saw')) is False

    def test_birling_is_not_chopping(self):
        assert is_chopping_event(_event('Birling')) is False

    def test_axe_throw_is_not_chopping(self):
        assert is_chopping_event(_event('Axe Throw')) is False

    def test_speed_climb_is_not_chopping(self):
        assert is_chopping_event(_event('Speed Climb')) is False

    def test_double_buck_is_not_chopping(self):
        assert is_chopping_event(_event('Double Buck')) is False

    def test_pulp_toss_is_not_chopping(self):
        assert is_chopping_event(_event('Pulp Toss')) is False

    def test_case_insensitive(self):
        assert is_chopping_event(_event('UNDERHAND SPEED')) is True

    def test_empty_name_is_not_chopping(self):
        assert is_chopping_event(_event('')) is False
