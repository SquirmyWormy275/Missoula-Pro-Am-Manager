"""
Unit tests for pure helper functions in services/partner_matching.py.

No database required. _normalize_name, _is_entered, and _read_partner_name
are all pure functions or can be exercised via SimpleNamespace mocks.

DB-dependent functions (auto_assign_event_partners, auto_assign_pro_partners,
_event_pool, _set_partner_bidirectional) are excluded.

Run:  pytest tests/test_partner_matching.py -v
"""
from types import SimpleNamespace

import pytest

from services.partner_matching import (
    _is_entered,
    _normalize_name,
    _read_partner_name,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(id=1, name='Springboard', display_name=None):
    return SimpleNamespace(
        id=id,
        name=name,
        display_name=display_name or name,
    )


def _competitor(partners: dict = None):
    """Lightweight ProCompetitor-alike with get_partners()."""
    _partners = partners or {}

    class Comp:
        def get_partners(self):
            return _partners

    return Comp()


# ---------------------------------------------------------------------------
# _normalize_name
# ---------------------------------------------------------------------------

class TestNormalizeName:
    def test_lowercases(self):
        assert _normalize_name('Alice') == 'alice'

    def test_removes_spaces(self):
        assert _normalize_name('Alice Smith') == 'alicesmith'

    def test_removes_hyphens(self):
        assert _normalize_name('Mary-Jane') == 'maryjane'

    def test_removes_punctuation(self):
        assert _normalize_name("O'Brien") == 'obrien'

    def test_strips_leading_trailing_space(self):
        assert _normalize_name('  Bob  ') == 'bob'

    def test_handles_none(self):
        assert _normalize_name(None) == ''

    def test_handles_empty_string(self):
        assert _normalize_name('') == ''

    def test_numbers_preserved(self):
        assert _normalize_name('Comp1') == 'comp1'

    def test_two_equivalent_names_match(self):
        assert _normalize_name('Alice Smith') == _normalize_name('alice smith')


# ---------------------------------------------------------------------------
# _is_entered
# ---------------------------------------------------------------------------

class TestIsEntered:
    def test_matches_by_event_id_string(self):
        ev = _event(id=7)
        assert _is_entered(ev, ['7']) is True

    def test_matches_by_normalized_name(self):
        ev = _event(id=7, name='Springboard')
        assert _is_entered(ev, ['springboard']) is True

    def test_matches_by_normalized_display_name(self):
        ev = _event(id=7, name='Springboard', display_name='1-Board Springboard')
        assert _is_entered(ev, ['1boardspringboard']) is True

    def test_case_insensitive_name_match(self):
        ev = _event(name='Stock Saw')
        assert _is_entered(ev, ['Stock Saw']) is True

    def test_not_entered_returns_false(self):
        ev = _event(id=7, name='Springboard')
        assert _is_entered(ev, ['Birling', '99']) is False

    def test_empty_list_returns_false(self):
        ev = _event(id=7)
        assert _is_entered(ev, []) is False

    def test_none_list_returns_false(self):
        ev = _event(id=7)
        assert _is_entered(ev, None) is False

    def test_none_value_in_list_skipped(self):
        ev = _event(id=7, name='Springboard')
        assert _is_entered(ev, [None, '7']) is True

    def test_empty_string_in_list_skipped(self):
        ev = _event(id=7, name='Springboard')
        assert _is_entered(ev, ['', '7']) is True

    def test_multiple_entries_first_match_suffices(self):
        ev = _event(id=7, name='Springboard')
        assert _is_entered(ev, ['Birling', 'Springboard']) is True


# ---------------------------------------------------------------------------
# _read_partner_name
# ---------------------------------------------------------------------------

class TestReadPartnerName:
    def test_reads_by_event_id_string(self):
        ev = _event(id=5)
        comp = _competitor({'5': 'Bob Jones'})
        assert _read_partner_name(comp, ev) == 'Bob Jones'

    def test_reads_by_event_name(self):
        ev = _event(id=99, name='Springboard')
        comp = _competitor({'Springboard': 'Carol White'})
        assert _read_partner_name(comp, ev) == 'Carol White'

    def test_reads_by_display_name(self):
        ev = _event(id=99, name='Springboard', display_name='1-Board Springboard')
        comp = _competitor({'1-Board Springboard': 'Dave Green'})
        assert _read_partner_name(comp, ev) == 'Dave Green'

    def test_reads_by_lowercase_name(self):
        ev = _event(id=99, name='Springboard')
        comp = _competitor({'springboard': 'Eve Black'})
        assert _read_partner_name(comp, ev) == 'Eve Black'

    def test_reads_by_lowercase_display_name(self):
        ev = _event(id=99, name='Springboard', display_name='1-Board Springboard')
        comp = _competitor({'1-board springboard': 'Fred Blue'})
        assert _read_partner_name(comp, ev) == 'Fred Blue'

    def test_no_matching_key_returns_empty(self):
        ev = _event(id=5, name='Springboard')
        comp = _competitor({'Birling': 'Grace'})
        assert _read_partner_name(comp, ev) == ''

    def test_empty_partners_dict_returns_empty(self):
        ev = _event(id=5)
        comp = _competitor({})
        assert _read_partner_name(comp, ev) == ''

    def test_non_dict_partners_returns_empty(self):
        ev = _event(id=5)

        class BadComp:
            def get_partners(self):
                return None

        assert _read_partner_name(BadComp(), ev) == ''

    def test_whitespace_in_partner_name_is_preserved(self):
        ev = _event(id=5, name='Double Buck')
        comp = _competitor({'Double Buck': '  Alice Smith  '})
        # strip() is applied inside the function
        assert _read_partner_name(comp, ev) == 'Alice Smith'

    def test_id_key_takes_priority_over_name(self):
        """Event id string key is checked first in the key list."""
        ev = _event(id=3, name='Stock Saw')
        comp = _competitor({'3': 'Partner A', 'Stock Saw': 'Partner B'})
        assert _read_partner_name(comp, ev) == 'Partner A'
