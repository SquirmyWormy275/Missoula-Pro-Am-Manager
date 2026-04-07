"""
Unit tests for services/birling_bracket.py

The BirlingBracket class stores state in event.payouts (a JSON TEXT field) and
calls db.session.commit() to persist.  Both are mocked here so no database is
needed.

Run:  pytest tests/test_birling_bracket.py -v
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from services.birling_bracket import BirlingBracket

# ---------------------------------------------------------------------------
# Helper: create a mock event with a mutable payouts field
# ---------------------------------------------------------------------------

def _mock_event(payouts='{}', event_type='college'):
    ev = MagicMock()
    ev.payouts = payouts
    ev.event_type = event_type
    ev.id = 1
    ev.status = 'pending'
    return ev


def _bracket(num_competitors=4, seeding=None, event_type='college'):
    """Convenience: create and generate a bracket with mock DB."""
    with patch('services.birling_bracket.db'):
        ev = _mock_event(event_type=event_type)
        b = BirlingBracket(ev)
        comps = [{'id': i, 'name': f'Comp{i}'} for i in range(1, num_competitors + 1)]
        b.generate_bracket(comps, seeding)
    return b


# ---------------------------------------------------------------------------
# generate_bracket — structure
# ---------------------------------------------------------------------------

class TestGenerateBracket:
    def test_requires_at_least_two_competitors(self):
        with patch('services.birling_bracket.db'):
            ev = _mock_event()
            b = BirlingBracket(ev)
            with pytest.raises(ValueError):
                b.generate_bracket([{'id': 1, 'name': 'Solo'}])

    def test_four_competitors_creates_two_first_round_matches(self):
        b = _bracket(4)
        first_round = b.bracket_data['bracket']['winners'][0]
        assert len(first_round) == 2

    def test_eight_competitors_creates_four_first_round_matches(self):
        b = _bracket(8)
        first_round = b.bracket_data['bracket']['winners'][0]
        assert len(first_round) == 4

    def test_competitors_stored(self):
        b = _bracket(4)
        assert len(b.bracket_data['competitors']) == 4

    def test_seeding_respected(self):
        b = _bracket(4, seeding=[4, 3, 2, 1])
        assert b.bracket_data['seeding'] == [4, 3, 2, 1]

    def test_default_seeding_is_registration_order(self):
        b = _bracket(4)
        assert b.bracket_data['seeding'] == [1, 2, 3, 4]

    def test_finals_structure_created(self):
        b = _bracket(4)
        finals = b.bracket_data['bracket']['finals']
        assert finals['match_id'] == 'F1'
        assert finals['winner'] is None

    def test_true_finals_structure_created(self):
        b = _bracket(4)
        true_finals = b.bracket_data['bracket']['true_finals']
        assert true_finals['match_id'] == 'F2'
        assert true_finals['needed'] is False

    def test_byes_auto_advance_when_present(self):
        # 3 competitors → bracket_size=4, one bye expected
        b = _bracket(3)
        first_round = b.bracket_data['bracket']['winners'][0]
        bye_match = next((m for m in first_round if m.get('is_bye')), None)
        assert bye_match is not None
        assert bye_match['winner'] is not None  # auto-advanced

    def test_losers_bracket_created(self):
        b = _bracket(4)
        assert len(b.bracket_data['bracket']['losers']) > 0

    def test_match_ids_are_unique(self):
        b = _bracket(8)
        ids = []
        for round_matches in b.bracket_data['bracket']['winners']:
            ids.extend(m['match_id'] for m in round_matches)
        for round_matches in b.bracket_data['bracket']['losers']:
            ids.extend(m['match_id'] for m in round_matches)
        ids.append(b.bracket_data['bracket']['finals']['match_id'])
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# _find_match
# ---------------------------------------------------------------------------

class TestFindMatch:
    def test_find_winners_match(self):
        b = _bracket(4)
        match = b._find_match('W1_1')
        assert match is not None
        assert match['match_id'] == 'W1_1'

    def test_find_finals(self):
        b = _bracket(4)
        match = b._find_match('F1')
        assert match is not None
        assert match['match_id'] == 'F1'

    def test_find_true_finals(self):
        b = _bracket(4)
        match = b._find_match('F2')
        assert match is not None
        assert match['match_id'] == 'F2'

    def test_nonexistent_match_returns_none(self):
        b = _bracket(4)
        match = b._find_match('W99_99')
        assert match is None


# ---------------------------------------------------------------------------
# get_current_matches — ready matches have both competitors and no winner
# ---------------------------------------------------------------------------

class TestGetCurrentMatches:
    def test_first_round_matches_ready_after_generation(self):
        b = _bracket(4)
        ready = b.get_current_matches()
        # First round should have non-bye matches with both slots filled
        assert len(ready) > 0

    def test_bye_matches_not_in_ready_list(self):
        b = _bracket(3)  # 3 comps → 1 bye
        ready = b.get_current_matches()
        for match in ready:
            assert not match.get('is_bye', False)


# ---------------------------------------------------------------------------
# record_match_result — advancement logic
# ---------------------------------------------------------------------------

class TestRecordMatchResult:
    def _populate_first_round(self, b):
        """Ensure first-round matches have two real competitors (handles byes)."""
        first_round = b.bracket_data['bracket']['winners'][0]
        for match in first_round:
            if match['competitor1'] is None:
                match['competitor1'] = 99
            if match['competitor2'] is None:
                match['competitor2'] = 98

    def test_winner_recorded(self):
        b = _bracket(4)
        match = b.bracket_data['bracket']['winners'][0][0]
        comp1 = match['competitor1']
        comp2 = match['competitor2']
        with patch('services.birling_bracket.db'):
            b.record_match_result(match['match_id'], comp1)
        assert match['winner'] == comp1
        assert match['loser'] == comp2

    def test_invalid_winner_raises(self):
        b = _bracket(4)
        match = b.bracket_data['bracket']['winners'][0][0]
        with patch('services.birling_bracket.db'):
            with pytest.raises(ValueError):
                b.record_match_result(match['match_id'], 9999)

    def test_winner_advances_to_next_round(self):
        b = _bracket(4)
        first_round = b.bracket_data['bracket']['winners'][0]
        # Record both first-round matches so a second-round slot fills
        for match in first_round:
            comp1, comp2 = match['competitor1'], match['competitor2']
            if comp1 is not None and comp2 is not None:
                with patch('services.birling_bracket.db'):
                    b.record_match_result(match['match_id'], comp1)

        # At least one slot in winners round 2 should be filled
        if len(b.bracket_data['bracket']['winners']) > 1:
            second_round = b.bracket_data['bracket']['winners'][1]
            filled = any(
                m['competitor1'] is not None or m['competitor2'] is not None
                for m in second_round
            )
            assert filled

    def test_loser_drops_to_losers_bracket(self):
        b = _bracket(4)
        match = b.bracket_data['bracket']['winners'][0][0]
        comp1 = match['competitor1']
        comp2 = match['competitor2']
        if comp1 is None or comp2 is None:
            pytest.skip("bye match — not applicable")
        with patch('services.birling_bracket.db'):
            b.record_match_result(match['match_id'], comp1)
        # The loser (comp2) should appear in the losers bracket
        loser_id = comp2
        found_in_losers = False
        for round_matches in b.bracket_data['bracket']['losers']:
            for lm in round_matches:
                if lm['competitor1'] == loser_id or lm['competitor2'] == loser_id:
                    found_in_losers = True
        assert found_in_losers


# ---------------------------------------------------------------------------
# _record_elimination — placement tracking
# ---------------------------------------------------------------------------

class TestRecordElimination:
    def test_first_elimination_is_last_place(self):
        b = _bracket(4)
        b._record_elimination(1)
        placements = b.bracket_data['placements']
        assert placements['1'] == 4  # 4 competitors → last place = 4

    def test_second_elimination_is_second_to_last(self):
        b = _bracket(4)
        b._record_elimination(1)
        b._record_elimination(2)
        assert b.bracket_data['placements']['2'] == 3

    def test_placements_are_unique_positions(self):
        b = _bracket(6)
        for i in range(1, 5):
            b._record_elimination(i)
        positions = list(b.bracket_data['placements'].values())
        assert len(positions) == len(set(positions))


# ---------------------------------------------------------------------------
# Grand finals — winners champ beats losers champ (no true finals needed)
# ---------------------------------------------------------------------------

class TestGrandFinals:
    def test_winners_champ_wins_grand_finals(self):
        b = _bracket(4)
        finals = b.bracket_data['bracket']['finals']
        finals['competitor1'] = 10  # winners champ
        finals['competitor2'] = 20  # losers champ
        with patch('services.birling_bracket.db'):
            b.record_match_result('F1', 10)
        assert b.bracket_data['placements']['10'] == 1
        assert b.bracket_data['placements']['20'] == 2
        assert b.bracket_data['bracket']['true_finals']['needed'] is False

    def test_losers_champ_wins_grand_finals_triggers_true_finals(self):
        b = _bracket(4)
        finals = b.bracket_data['bracket']['finals']
        finals['competitor1'] = 10  # winners champ (competitor1)
        finals['competitor2'] = 20  # losers champ (competitor2)
        with patch('services.birling_bracket.db'):
            b.record_match_result('F1', 20)  # losers champ wins
        assert b.bracket_data['bracket']['true_finals']['needed'] is True
        tf = b.bracket_data['bracket']['true_finals']
        assert tf['competitor1'] == 10
        assert tf['competitor2'] == 20

    def test_true_finals_records_champion(self):
        b = _bracket(4)
        tf = b.bracket_data['bracket']['true_finals']
        tf['competitor1'] = 10
        tf['competitor2'] = 20
        tf['needed'] = True
        with patch('services.birling_bracket.db'):
            b.record_match_result('F2', 20)
        assert b.bracket_data['placements']['20'] == 1
        assert b.bracket_data['placements']['10'] == 2
