"""Property coverage for the compact Birling double-elimination lifecycle.

The old standalone diagnostic compared every field size with a power-of-two
losers-bracket oracle. It could not see the entrant count, so it rejected the
compact layouts that the service deliberately creates for fields with byes.

This test drives real playable matches for fields of four through sixteen
entrants. The only invariant that matters on race day is that every bracket
reaches one, complete placement for every entrant without getting stuck.
"""

from __future__ import annotations

import random
from unittest.mock import MagicMock

import pytest

from services.birling_bracket import BirlingBracket
from tests.conftest import patched_bracket_deps

FIELD_SIZES = range(4, 17)
SIMULATIONS_PER_FIELD = 60


def _make_bracket(field_size: int) -> BirlingBracket:
    event = MagicMock()
    event.payouts = "{}"
    event.event_type = "college"
    event.id = 1
    event.status = "pending"

    with patched_bracket_deps():
        bracket = BirlingBracket(event)
        bracket.generate_bracket(
            [
                {"id": competitor_id, "name": f"Competitor {competitor_id}"}
                for competitor_id in range(1, field_size + 1)
            ]
        )
    return bracket


def _play_to_completion(field_size: int, seed: int) -> tuple[dict[str, int], bool]:
    bracket = _make_bracket(field_size)
    randomizer = random.Random(field_size * 1000 + seed)
    true_finals_played = False

    for _ in range(100):
        ready_matches = bracket.get_current_matches()
        if not ready_matches:
            return bracket.get_placements(), true_finals_played

        match = randomizer.choice(ready_matches)
        winner_id = randomizer.choice([match["competitor1"], match["competitor2"]])
        with patched_bracket_deps():
            bracket.record_match_result(match["match_id"], winner_id)

        if match["match_id"] == "F1":
            true_finals_played = bracket.bracket_data["bracket"]["true_finals"]["needed"]

    pytest.fail(f"N={field_size}, seed={seed}: bracket did not reach completion")


@pytest.mark.parametrize("field_size", FIELD_SIZES)
def test_compact_bracket_lifecycle_places_every_competitor_once(field_size: int):
    true_final_resets = 0
    expected_competitors = {str(competitor_id) for competitor_id in range(1, field_size + 1)}

    for seed in range(SIMULATIONS_PER_FIELD):
        placements, played_true_finals = _play_to_completion(field_size, seed)

        assert set(placements) == expected_competitors
        assert sorted(placements.values()) == list(range(1, field_size + 1))
        true_final_resets += played_true_finals

    assert true_final_resets > 0
