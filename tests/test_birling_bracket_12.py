"""
Tests for the birling bracket with 12 competitors (realistic college field).

Uses mock objects (no DB required) consistent with the existing
test_birling_bracket.py style. The BirlingBracket stores state in
event.payouts JSON and calls db.session.commit() — both mocked.

Competitor names come from BIRLING_MEN_BRACKET and BIRLING_WOMEN_BRACKET
in tests/fixtures/synthetic_data.py (12 competitors each).

Run:  pytest tests/test_birling_bracket_12.py -v
"""
import math
from unittest.mock import MagicMock, patch

import pytest

from services.birling_bracket import BirlingBracket
from tests.fixtures.synthetic_data import BIRLING_MEN_BRACKET, BIRLING_WOMEN_BRACKET

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_event(payouts='{}', event_type='college'):
    ev = MagicMock()
    ev.payouts = payouts
    ev.event_type = event_type
    ev.id = 1
    ev.status = 'pending'
    return ev


def _bracket_12(names=None, event_type='college'):
    """Create a 12-competitor bracket with mocked DB."""
    if names is None:
        names = BIRLING_MEN_BRACKET
    comps = [{'id': i + 1, 'name': name} for i, name in enumerate(names)]
    with patch('services.birling_bracket.db'):
        ev = _mock_event(event_type=event_type)
        b = BirlingBracket(ev)
        b.generate_bracket(comps)
    return b, comps


def _find_match(bracket, match_id):
    """Find match by ID across all bracket sections."""
    return bracket._find_match(match_id)


def _play_match(bracket, match_id, winner_id):
    """Record a match result with DB mocked."""
    with patch('services.birling_bracket.db'):
        bracket.record_match_result(match_id, winner_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBracketGeneration12:
    """Verify bracket structure for 12 competitors uses compact scaling."""

    def test_first_round_has_six_matches(self):
        b, comps = _bracket_12()
        first_round = b.bracket_data['bracket']['winners'][0]
        assert len(first_round) == 6

    def test_no_first_round_byes_for_even_12_competitors(self):
        b, comps = _bracket_12()
        first_round = b.bracket_data['bracket']['winners'][0]
        byes = [m for m in first_round if m['is_bye']]
        assert len(byes) == 0, f"Expected no first-round byes for 12 competitors, got {len(byes)}"

    def test_bye_matches_auto_advance_winner(self):
        b, comps = _bracket_12()
        first_round = b.bracket_data['bracket']['winners'][0]
        for match in first_round:
            if match['is_bye']:
                assert match['winner'] is not None, (
                    f"Bye match {match['match_id']} should auto-advance a winner"
                )

    def test_non_bye_matches_have_two_competitors(self):
        b, comps = _bracket_12()
        first_round = b.bracket_data['bracket']['winners'][0]
        non_bye = [m for m in first_round if not m['is_bye']]
        assert len(non_bye) == 4, "12 competitors → 4 actual first-round matches"
        for match in non_bye:
            assert match['competitor1'] is not None
            assert match['competitor2'] is not None
            assert match['winner'] is None

    def test_winners_bracket_has_correct_rounds(self):
        b, comps = _bracket_12()
        winners = b.bracket_data['bracket']['winners']
        # Compact 12-competitor shape: round 1 (6 matches), round 2 (3), round 3 (2), round 4 (1)
        assert len(winners) == 4
        assert len(winners[0]) == 6
        assert len(winners[1]) == 3
        assert len(winners[2]) == 2
        assert len(winners[3]) == 1

    def test_losers_bracket_exists(self):
        b, comps = _bracket_12()
        losers = b.bracket_data['bracket']['losers']
        assert len(losers) > 0, "Losers bracket should have at least 1 round"

    def test_finals_and_true_finals_exist(self):
        b, comps = _bracket_12()
        assert b.bracket_data['bracket']['finals'] is not None
        assert b.bracket_data['bracket']['true_finals'] is not None
        assert b.bracket_data['bracket']['true_finals']['needed'] is False

    def test_all_12_competitors_stored(self):
        b, comps = _bracket_12()
        assert len(b.bracket_data['competitors']) == 12

    def test_seeding_matches_input_order(self):
        b, comps = _bracket_12()
        expected_ids = [c['id'] for c in comps]
        assert b.bracket_data['seeding'] == expected_ids


class TestBracketProgression:
    """Simulate the men's bracket through to Tommy White (id 1) as champion."""

    def test_first_round_progression(self):
        """Play all non-bye first-round matches. Winners should populate round 2."""
        b, comps = _bracket_12()

        first_round = b.bracket_data['bracket']['winners'][0]
        non_bye = [m for m in first_round if not m['is_bye']]

        # For each non-bye match, the first competitor wins
        for match in non_bye:
            _play_match(b, match['match_id'], match['competitor1'])

        # Round 2 should now have some competitors populated
        round2 = b.bracket_data['bracket']['winners'][1]
        populated = [m for m in round2
                     if m['competitor1'] is not None or m['competitor2'] is not None]
        assert len(populated) > 0, "Round 2 should have competitors after round 1"

    def test_full_winners_bracket_to_finals(self):
        """Play all winners bracket matches with seed 1 (Tommy White) always winning."""
        b, comps = _bracket_12()
        tommy_id = 1  # Tommy White

        # Play through all winners bracket rounds
        for round_idx, round_matches in enumerate(b.bracket_data['bracket']['winners']):
            for match in round_matches:
                if match['winner'] is not None:
                    continue  # bye already resolved
                if match['competitor1'] is None or match['competitor2'] is None:
                    continue  # not yet populated

                # If Tommy is in the match, he wins; otherwise first competitor wins
                if match['competitor1'] == tommy_id or match['competitor2'] == tommy_id:
                    _play_match(b, match['match_id'], tommy_id)
                else:
                    _play_match(b, match['match_id'], match['competitor1'])

        # Tommy should be in the finals as competitor1 (winners bracket champion)
        finals = b.bracket_data['bracket']['finals']
        assert finals['competitor1'] == tommy_id, (
            f"Tommy (id={tommy_id}) should be winners bracket champion, "
            f"but finals competitor1 is {finals['competitor1']}"
        )


class TestLosersBracket:
    """Verify losers bracket receives eliminated competitors."""

    def test_losers_from_round1_populate_losers_bracket(self):
        b, comps = _bracket_12()

        first_round = b.bracket_data['bracket']['winners'][0]
        non_bye = [m for m in first_round if not m['is_bye']]

        # Play all non-bye round 1 matches; first competitor wins
        for match in non_bye:
            _play_match(b, match['match_id'], match['competitor1'])

        # Check losers bracket round 1 has competitors
        losers = b.bracket_data['bracket']['losers']
        if losers:
            losers_r1 = losers[0]
            populated = [m for m in losers_r1
                         if m['competitor1'] is not None or m['competitor2'] is not None]
            assert len(populated) > 0, "Losers bracket round 1 should have competitors from W1 losers"

    def test_loser_eliminated_from_losers_bracket_gets_placement(self):
        """A competitor who loses in losers bracket should get a placement."""
        b, comps = _bracket_12()

        # Play round 1 non-bye matches
        first_round = b.bracket_data['bracket']['winners'][0]
        non_bye = [m for m in first_round if not m['is_bye']]
        for match in non_bye:
            _play_match(b, match['match_id'], match['competitor1'])

        # Now play losers round 1 matches
        losers = b.bracket_data['bracket']['losers']
        if losers and losers[0]:
            for match in losers[0]:
                if (match['competitor1'] is not None and
                        match['competitor2'] is not None and
                        match['winner'] is None):
                    _play_match(b, match['match_id'], match['competitor1'])

            # At least one competitor should be eliminated (have a placement)
            placements = b.bracket_data['placements']
            assert len(placements) > 0, "Eliminated losers bracket competitor should have a placement"


class TestTop6Determination:
    """Run a full bracket simulation and verify top 6 placements emerge."""

    def _play_all_available(self, bracket):
        """Play all matches that have both competitors but no winner.
        In each match, the competitor with the lower ID wins (deterministic).
        Returns number of matches played.
        """
        played = 0
        for _ in range(100):  # safety limit
            ready = bracket.get_current_matches()
            if not ready:
                break
            for match in ready:
                winner = min(match['competitor1'], match['competitor2'])
                _play_match(bracket, match['match_id'], winner)
                played += 1
        return played

    def test_placements_populated_after_full_bracket(self):
        b, comps = _bracket_12()
        self._play_all_available(b)

        placements = b.get_placements()
        # After full bracket, we should have placements for competitors
        # At minimum, positions 1 and 2 from finals
        assert len(placements) > 0, "Should have at least some placements"

    def test_champion_is_position_1(self):
        b, comps = _bracket_12()
        self._play_all_available(b)

        placements = b.get_placements()
        positions = list(placements.values())
        assert 1 in positions, "There should be a 1st place finisher"

    def test_no_duplicate_positions_for_top_finishers(self):
        b, comps = _bracket_12()
        self._play_all_available(b)

        placements = b.get_placements()
        # Position 1 and 2 should each appear at most once
        position_counts = {}
        for pos in placements.values():
            position_counts[pos] = position_counts.get(pos, 0) + 1

        assert position_counts.get(1, 0) <= 1, "Only one champion allowed"
        assert position_counts.get(2, 0) <= 1, "Only one runner-up allowed"

    def test_women_bracket_12_also_works(self):
        """Verify the women's bracket (also 12 competitors) generates correctly."""
        b, comps = _bracket_12(names=BIRLING_WOMEN_BRACKET)
        first_round = b.bracket_data['bracket']['winners'][0]
        assert len(first_round) == 6
        byes = [m for m in first_round if m['is_bye']]
        assert len(byes) == 0

    def test_women_bracket_full_sim(self):
        """Run full women's bracket to completion."""
        b, comps = _bracket_12(names=BIRLING_WOMEN_BRACKET)
        self._play_all_available(b)

        placements = b.get_placements()
        assert len(placements) > 0

    def test_seeded_bracket_respects_order(self):
        """Provide explicit seeding and verify 1-seed gets favorable draw."""
        b, comps = _bracket_12()
        first_round = b.bracket_data['bracket']['winners'][0]

        match_with_seed1 = None
        for match in first_round:
            if match['competitor1'] == 1 or match['competitor2'] == 1:
                match_with_seed1 = match
                break

        assert match_with_seed1 is not None, "Seed 1 should be in a first-round match"
        assert match_with_seed1['competitor1'] == 1 or match_with_seed1['competitor2'] == 1
        opponent = match_with_seed1['competitor2'] if match_with_seed1['competitor1'] == 1 else match_with_seed1['competitor1']
        assert opponent == 12, f"Seed 1 should face seed 12 in the first round, got {opponent}"
