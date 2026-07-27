"""
Tests for services/birling_print.py.

Covers the show-prep blank-bracket deliverable (2026-04-20):
  - build_birling_print_context returns a deep-copied, result-scrubbed
    bracket view (round-1 competitors preserved, everything else reset).
  - Ungenerated bracket → None sentinel (caller flashes + redirects).
  - Live Event.payouts is never mutated, even on partially-played brackets.

Run:  pytest tests/test_birling_print.py -v
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services.birling_print import build_birling_print_context


def _event_with_bracket(bracket_payload: dict):
    """Return a fake Event-like object with scoring_type='bracket' and the
    given payouts JSON serialised on it.  Avoids touching the DB for
    pure-function tests."""
    return SimpleNamespace(
        id=1,
        scoring_type="bracket",
        payouts=json.dumps(bracket_payload),
    )


class TestUngenerated:
    def test_none_event_returns_none(self):
        assert build_birling_print_context(None) is None

    def test_non_bracket_event_returns_none(self):
        ev = SimpleNamespace(id=1, scoring_type="time", payouts="{}")
        assert build_birling_print_context(ev) is None

    def test_empty_payouts_returns_none(self):
        ev = _event_with_bracket({})
        assert build_birling_print_context(ev) is None

    def test_generated_but_empty_winners_returns_none(self):
        ev = _event_with_bracket({"bracket": {"winners": []}})
        assert build_birling_print_context(ev) is None


class TestScrubBehavior:
    def _seeded_payload(self) -> dict:
        """Minimal generate_bracket()-style payload with two rounds and a
        finals block — enough to verify the scrub across all bracket areas."""
        return {
            "bracket": {
                "winners": [
                    [
                        {
                            "match_id": "W1_1",
                            "round": "winners_1",
                            "competitor1": 1,
                            "competitor2": 2,
                            "winner": 1,
                            "loser": 2,
                            "falls": [{"fall_number": 1, "winner": 1}],
                            "is_bye": False,
                        },
                        {
                            "match_id": "W1_2",
                            "round": "winners_1",
                            "competitor1": 3,
                            "competitor2": 4,
                            "winner": None,
                            "loser": None,
                            "falls": [],
                            "is_bye": False,
                        },
                    ],
                    [
                        {
                            "match_id": "W2_1",
                            "round": "winners_2",
                            "competitor1": 1,
                            "competitor2": None,
                            "winner": None,
                            "loser": None,
                            "falls": [],
                            "is_bye": False,
                        },
                    ],
                ],
                "losers": [
                    [
                        {
                            "match_id": "L1_1",
                            "round": "losers_1",
                            "competitor1": 2,
                            "competitor2": None,
                            "winner": None,
                            "loser": None,
                            "falls": [],
                            "eliminated_position": 5,
                        },
                    ],
                ],
                "finals": {
                    "match_id": "F1",
                    "round": "finals",
                    "competitor1": 1,
                    "competitor2": None,
                    "winner": None,
                    "loser": None,
                    "falls": [],
                },
                "true_finals": {
                    "match_id": "F2",
                    "round": "true_finals",
                    "competitor1": 1,
                    "competitor2": 2,
                    "winner": 1,
                    "loser": 2,
                    "falls": [],
                    "needed": True,
                },
            },
            "competitors": [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "Bob"},
                {"id": 3, "name": "Charlie"},
                {"id": 4, "name": "Dave"},
            ],
            "seeding": [1, 2, 3, 4],
            "placements": {"2": 4},
        }

    def test_round_1_winners_preserved(self):
        ev = _event_with_bracket(self._seeded_payload())
        ctx = build_birling_print_context(ev)
        round1 = ctx["bracket"]["winners"][0]
        assert round1[0]["competitor1"] == 1
        assert round1[0]["competitor2"] == 2
        assert round1[1]["competitor1"] == 3
        assert round1[1]["competitor2"] == 4

    def test_round_1_winners_results_stripped(self):
        ev = _event_with_bracket(self._seeded_payload())
        ctx = build_birling_print_context(ev)
        round1 = ctx["bracket"]["winners"][0]
        assert round1[0]["winner"] is None
        assert round1[0]["loser"] is None
        assert round1[0]["falls"] == []

    def test_round_2_plus_winners_all_tbd(self):
        ev = _event_with_bracket(self._seeded_payload())
        ctx = build_birling_print_context(ev)
        round2 = ctx["bracket"]["winners"][1]
        for match in round2:
            assert match["competitor1"] is None
            assert match["competitor2"] is None
            assert match["winner"] is None

    def test_losers_bracket_all_tbd(self):
        ev = _event_with_bracket(self._seeded_payload())
        ctx = build_birling_print_context(ev)
        for round_matches in ctx["bracket"]["losers"]:
            for match in round_matches:
                assert match["competitor1"] is None
                assert match["competitor2"] is None
                assert match["winner"] is None
                assert match["eliminated_position"] is None

    def test_finals_stripped_and_blank(self):
        ev = _event_with_bracket(self._seeded_payload())
        ctx = build_birling_print_context(ev)
        for key in ("finals", "true_finals"):
            match = ctx["bracket"][key]
            assert match["competitor1"] is None
            assert match["competitor2"] is None
            assert match["winner"] is None
        # true_finals 'needed' must be reset to False so the blank print
        # doesn't imply the Losers champ beat the Winners champ.
        assert ctx["bracket"]["true_finals"]["needed"] is False

    def test_placements_not_rendered_in_context(self):
        """placements was only useful for the live bracket; the blank print
        context should not surface it (no one has placed yet, by definition)."""
        ev = _event_with_bracket(self._seeded_payload())
        ctx = build_birling_print_context(ev)
        assert "placements" not in ctx["bracket"]

    def test_live_event_payouts_not_mutated(self):
        """Deep copy guarantee: mutating the returned context must not
        write back to event.payouts."""
        payload = self._seeded_payload()
        ev = _event_with_bracket(payload)

        ctx = build_birling_print_context(ev)
        # Mutate the context aggressively.
        ctx["bracket"]["winners"][0][0]["competitor1"] = 99999
        ctx["competitors"].clear()

        # event.payouts must still parse back to the original payload.
        import json as _json

        still = _json.loads(ev.payouts)
        assert still["bracket"]["winners"][0][0]["competitor1"] == 1
        assert len(still["competitors"]) == 4

    def test_comp_lookup_includes_seeded_names(self):
        ev = _event_with_bracket(self._seeded_payload())
        ctx = build_birling_print_context(ev)
        assert ctx["comp_lookup"]["1"] == "Alice"
        assert ctx["comp_lookup"]["4"] == "Dave"
