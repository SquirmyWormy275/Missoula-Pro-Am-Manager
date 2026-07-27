"""
Regression tests for the stale-shape auto-migration on birling brackets.

Context: V2.14.14 rewrote the bracket generator so a 9-entrant field
produces 5 round-1 matches (1 bye + 4 pairs) instead of the old
power-of-two shape (8 matches with seeds 8 and 9 stacked into W1_8).
The generator fix was correct — but existing brackets in production were
generated BEFORE V2.14.14 shipped, so the stored Event.payouts still
carries the old power-of-two layout. Loading the manage page still
renders the stacked slots.

Race-weekend operator screenshot (2026-04-23): Women's College birling
bracket with 9 entrants shows W1_1..W1_7 each holding ONE competitor
and W1_8 holding TWO — exactly the pre-V2.14.14 shape.

Fix: BirlingBracket gains is_stale_power_of_two_shape(),
has_any_results_recorded(), and rebuild_if_stale_shape(). The manage
route and the print-context builder both call rebuild_if_stale_shape()
so the next render uses the compact layout.

Guard: rebuild only fires when zero results have been recorded. A
bracket mid-play is never silently torn down.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from services.birling_bracket import BirlingBracket


def _stale_9_entrant_payload():
    """Hand-build the pre-V2.14.14 power-of-two bracket shape for 9 entrants.

    Mirrors what race-weekend prod DBs still carry: 8 round-1 matches, with
    W1_1..W1_7 holding lone seeds and W1_8 stacking seeds 8 and 9 together.
    """
    competitors = [{"id": i + 1, "name": f"Seed{i + 1}"} for i in range(9)]
    seeding = [c["id"] for c in competitors]
    winners_r1 = []
    for i in range(7):
        winners_r1.append(
            {
                "match_id": f"W1_{i + 1}",
                "round": "winners_1",
                "competitor1": i + 1,
                "competitor2": None,
                "winner": None,
                "loser": None,
                "falls": [],
                "is_bye": False,
            }
        )
    winners_r1.append(
        {
            "match_id": "W1_8",
            "round": "winners_1",
            "competitor1": 8,
            "competitor2": 9,
            "winner": None,
            "loser": None,
            "falls": [],
            "is_bye": False,
        }
    )
    return {
        "bracket": {
            "winners": [winners_r1, [], [], []],
            "losers": [],
            "finals": None,
            "true_finals": None,
        },
        "competitors": competitors,
        "seeding": seeding,
        "current_round": "winners_1",
        "placements": {},
    }


def _mock_event(payouts: dict):
    ev = MagicMock()
    ev.payouts = json.dumps(payouts)
    ev.event_type = "college"
    ev.id = 1
    ev.status = "pending"
    ev.scoring_type = "bracket"
    return ev


class TestStaleShapeDetection:
    def test_detects_pre_v2_14_14_power_of_two_shape(self):
        with patch("services.birling_bracket.db"):
            bb = BirlingBracket(_mock_event(_stale_9_entrant_payload()))
            assert bb.is_stale_power_of_two_shape() is True

    def test_fresh_v2_14_14_bracket_is_not_stale(self):
        comps = [{"id": i + 1, "name": f"Seed{i + 1}"} for i in range(9)]
        with patch("services.birling_bracket.db"):
            ev = _mock_event({})
            bb = BirlingBracket(ev)
            bb.generate_bracket(comps)
            # Simulate reloading the event so bracket_data comes from payouts.
            ev.payouts = json.dumps(bb.bracket_data)
            bb2 = BirlingBracket(ev)
            assert bb2.is_stale_power_of_two_shape() is False

    def test_no_bracket_generated_is_not_stale(self):
        with patch("services.birling_bracket.db"):
            bb = BirlingBracket(_mock_event({}))
            assert bb.is_stale_power_of_two_shape() is False


class TestRebuildIfStaleShape:
    def test_rebuilds_9_entrant_stale_bracket_to_compact_shape(self):
        with patch("services.birling_bracket.db"):
            bb = BirlingBracket(_mock_event(_stale_9_entrant_payload()))
            rebuilt = bb.rebuild_if_stale_shape()

        assert rebuilt is True
        first_round = bb.bracket_data["bracket"]["winners"][0]
        assert len(first_round) == 5, (
            "N=9 compact shape must be 1 bye + 4 pairs — got "
            f"{len(first_round)} round-1 matches"
        )
        # Exactly one bye, holding the top seed.
        byes = [m for m in first_round if m.get("is_bye")]
        assert len(byes) == 1 and byes[0]["competitor1"] == 1
        # W1_8 must no longer exist — that's the stacked-slot tombstone.
        ids = {m["match_id"] for m in first_round}
        assert "W1_8" not in ids
        # Mirror pairings after the seed-1 bye: (2,9) (3,8) (4,7) (5,6).
        non_bye = [m for m in first_round if not m.get("is_bye")]
        pairs = {tuple(sorted((m["competitor1"], m["competitor2"]))) for m in non_bye}
        assert pairs == {(2, 9), (3, 8), (4, 7), (5, 6)}

    def test_refuses_to_rebuild_when_results_recorded(self):
        """If the bracket already has a winner recorded, DO NOT tear it down.

        Race-day operator with an in-progress bracket must never lose state
        to a silent migration. They can always reset manually via the UI.
        """
        payload = _stale_9_entrant_payload()
        # Mark one first-round match with a recorded winner (simulates a
        # played match before the operator hit the page post-deploy).
        payload["bracket"]["winners"][0][1]["winner"] = 2

        with patch("services.birling_bracket.db"):
            bb = BirlingBracket(_mock_event(payload))
            rebuilt = bb.rebuild_if_stale_shape()

        assert rebuilt is False
        # Shape stays exactly as it was — stale but intact.
        assert len(bb.bracket_data["bracket"]["winners"][0]) == 8
        assert bb.is_stale_power_of_two_shape() is True

    def test_refuses_to_rebuild_when_placements_present(self):
        """Completed bracket with final placements — also no silent rebuild."""
        payload = _stale_9_entrant_payload()
        payload["placements"] = {"1": 1, "2": 2}
        with patch("services.birling_bracket.db"):
            bb = BirlingBracket(_mock_event(payload))
            assert bb.rebuild_if_stale_shape() is False

    def test_noop_on_already_compact_bracket(self):
        """Rebuild must be idempotent — fresh brackets stay untouched."""
        comps = [{"id": i + 1, "name": f"S{i + 1}"} for i in range(9)]
        with patch("services.birling_bracket.db"):
            ev = _mock_event({})
            bb = BirlingBracket(ev)
            bb.generate_bracket(comps)
            ev.payouts = json.dumps(bb.bracket_data)
            bb2 = BirlingBracket(ev)
            rebuilt = bb2.rebuild_if_stale_shape()

        assert rebuilt is False
        assert len(bb2.bracket_data["bracket"]["winners"][0]) == 5


class TestHasAnyResultsRecorded:
    def test_false_on_fresh_bracket(self):
        payload = _stale_9_entrant_payload()
        with patch("services.birling_bracket.db"):
            bb = BirlingBracket(_mock_event(payload))
            assert bb.has_any_results_recorded() is False

    def test_true_when_winners_round_match_recorded(self):
        payload = _stale_9_entrant_payload()
        payload["bracket"]["winners"][0][0]["winner"] = 1
        with patch("services.birling_bracket.db"):
            bb = BirlingBracket(_mock_event(payload))
            assert bb.has_any_results_recorded() is True

    def test_true_when_placements_populated(self):
        payload = _stale_9_entrant_payload()
        payload["placements"] = {"3": 1}
        with patch("services.birling_bracket.db"):
            bb = BirlingBracket(_mock_event(payload))
            assert bb.has_any_results_recorded() is True

    def test_recorded_falls_block_rebuild_even_without_winner(self):
        """CODEX P1: birling is best-of-3. A match with one fall recorded but
        no winner yet is in-progress operator state — the rebuild must NOT
        silently destroy those falls when migrating the bracket shape.
        """
        payload = _stale_9_entrant_payload()
        # Operator entered ONE fall in the W1_2 match against seed 2.
        # No winner yet (best-of-3 not decided).
        payload["bracket"]["winners"][0][1]["falls"] = [{"winner": 2, "fall_number": 1}]
        with patch("services.birling_bracket.db"):
            bb = BirlingBracket(_mock_event(payload))
            assert bb.has_any_results_recorded() is True, (
                "Recorded falls must count as in-progress state — without this "
                "guard the rebuild silently loses match progress."
            )
            assert bb.rebuild_if_stale_shape() is False
            # Falls survive: shape stays at the stale 8-match layout.
            assert len(bb.bracket_data["bracket"]["winners"][0]) == 8

    def test_bye_auto_advance_does_not_count_as_played_result(self):
        """Round-1 byes auto-advance at generation time — their winner field
        is set but that's not a played match. Treating this as 'played'
        would block the stale-shape migration on every stale 9-entrant
        bracket, defeating the fix. Guard against that."""
        payload = _stale_9_entrant_payload()
        payload["bracket"]["winners"][0][0]["is_bye"] = True
        payload["bracket"]["winners"][0][0]["winner"] = 1
        with patch("services.birling_bracket.db"):
            bb = BirlingBracket(_mock_event(payload))
            assert bb.has_any_results_recorded() is False
