"""
Flight builder left-handed-springboard constraint tests.

Domain rule (2026-04-20): only one physical left-handed springboard dummy
exists on site, so at most one LH-containing heat can be in the same flight
time-window.  services/flight_builder.py enforces this via a scoring penalty
in _score_ordering plus a post-slice sanity-check warning.

These tests exercise the scoring logic directly — they are pure helpers
and require no database.

Run:  pytest tests/test_flight_builder_lh_constraint.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.flight_builder import _score_ordering


def _event(stand_type: str = "springboard", id: int = 1, name: str = "Springboard"):
    return SimpleNamespace(stand_type=stand_type, id=id, name=name)


def _heat_data(event, competitors, heat_number: int = 1, contains_lh: bool = False):
    heat = SimpleNamespace(event_id=event.id, heat_number=heat_number, run_number=1)
    return {
        "heat": heat,
        "event": event,
        "competitors": set(competitors),
        "contains_lh": contains_lh,
    }


# ---------------------------------------------------------------------------
# Scoring penalty dominates spreading LH across flights
# ---------------------------------------------------------------------------


class TestLhFlightScoringPenalty:
    def test_two_lh_heats_in_same_flight_trigger_penalty(self):
        """Two LH-tagged heats in the first flight block → 1000-point penalty."""
        ev = _event()
        # heats_per_flight=4.  Two LH heats in positions 0 and 1 → same block.
        ordered = [
            _heat_data(ev, {1}, heat_number=1, contains_lh=True),
            _heat_data(ev, {2}, heat_number=2, contains_lh=True),
            _heat_data(ev, {3}, heat_number=3, contains_lh=False),
            _heat_data(ev, {4}, heat_number=4, contains_lh=False),
        ]
        score_same_flight = _score_ordering(ordered, heats_per_flight=4)

        # Spread: LH heats in different flights.
        spread = [
            _heat_data(ev, {1}, heat_number=1, contains_lh=True),
            _heat_data(ev, {3}, heat_number=3, contains_lh=False),
            _heat_data(ev, {4}, heat_number=4, contains_lh=False),
            _heat_data(ev, {5}, heat_number=5, contains_lh=False),
            _heat_data(ev, {2}, heat_number=2, contains_lh=True),
            _heat_data(ev, {6}, heat_number=6, contains_lh=False),
            _heat_data(ev, {7}, heat_number=7, contains_lh=False),
            _heat_data(ev, {8}, heat_number=8, contains_lh=False),
        ]
        score_spread = _score_ordering(spread, heats_per_flight=4)

        # Spread must score higher — penalty dominates.
        assert score_spread > score_same_flight
        # Concretely, the penalty is -1000 per extra LH heat in a block.
        assert (score_spread - score_same_flight) >= 1000

    def test_three_lh_heats_in_same_flight_double_penalty(self):
        """Three LH heats in one block → -1000 × 2 = -2000 extra penalty vs one."""
        ev = _event()
        three_in_one = [
            _heat_data(ev, {1}, heat_number=1, contains_lh=True),
            _heat_data(ev, {2}, heat_number=2, contains_lh=True),
            _heat_data(ev, {3}, heat_number=3, contains_lh=True),
            _heat_data(ev, {4}, heat_number=4, contains_lh=False),
        ]
        one_lh = [
            _heat_data(ev, {1}, heat_number=1, contains_lh=True),
            _heat_data(ev, {2}, heat_number=2, contains_lh=False),
            _heat_data(ev, {3}, heat_number=3, contains_lh=False),
            _heat_data(ev, {4}, heat_number=4, contains_lh=False),
        ]
        score_three = _score_ordering(three_in_one, heats_per_flight=4)
        score_one = _score_ordering(one_lh, heats_per_flight=4)
        # Three LH in one block pays two extra -1000 penalties vs a single LH.
        assert (score_one - score_three) >= 2000

    def test_lh_heats_across_different_flights_no_penalty(self):
        """One LH heat per flight → no penalty beyond the usual scoring."""
        ev = _event()
        ordered = [
            _heat_data(ev, {1}, heat_number=1, contains_lh=True),
            _heat_data(ev, {3}, heat_number=3, contains_lh=False),
            _heat_data(ev, {4}, heat_number=4, contains_lh=False),
            _heat_data(ev, {5}, heat_number=5, contains_lh=False),
            # Flight 2 boundary
            _heat_data(ev, {2}, heat_number=2, contains_lh=True),
            _heat_data(ev, {6}, heat_number=6, contains_lh=False),
            _heat_data(ev, {7}, heat_number=7, contains_lh=False),
            _heat_data(ev, {8}, heat_number=8, contains_lh=False),
        ]
        no_lh = [
            _heat_data(ev, {1}, heat_number=1, contains_lh=False),
            _heat_data(ev, {3}, heat_number=3, contains_lh=False),
            _heat_data(ev, {4}, heat_number=4, contains_lh=False),
            _heat_data(ev, {5}, heat_number=5, contains_lh=False),
            _heat_data(ev, {2}, heat_number=2, contains_lh=False),
            _heat_data(ev, {6}, heat_number=6, contains_lh=False),
            _heat_data(ev, {7}, heat_number=7, contains_lh=False),
            _heat_data(ev, {8}, heat_number=8, contains_lh=False),
        ]
        score_spread = _score_ordering(ordered, heats_per_flight=4)
        score_no_lh = _score_ordering(no_lh, heats_per_flight=4)
        # Scores should be effectively equal — no LH penalty.
        assert score_spread == score_no_lh

    def test_heats_without_contains_lh_key_default_to_false(self):
        """Old-shape heat dicts (no contains_lh key) must not crash the scorer."""
        ev = _event()
        ordered = [
            # Explicitly drop the contains_lh key to simulate old data.
            {
                "heat": SimpleNamespace(event_id=ev.id, heat_number=1, run_number=1),
                "event": ev,
                "competitors": {1},
            },
            {
                "heat": SimpleNamespace(event_id=ev.id, heat_number=2, run_number=1),
                "event": ev,
                "competitors": {2},
            },
        ]
        # Must not raise.
        score = _score_ordering(ordered, heats_per_flight=4)
        assert isinstance(score, float)

    def test_empty_ordering_returns_zero(self):
        assert _score_ordering([], heats_per_flight=4) == 0.0
