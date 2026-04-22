---
title: Flight builder stacked all of one event's heats into a single flight
date: 2026-04-21
category: logic-errors
module: flight_builder
problem_type: logic_error
component: service_object
severity: high
symptoms:
  - "Flight 1 held all 4 women's underhand heats + 5 of 7 men's underhand heats (9 of 11 underhand heats in one flight)"
  - "Greedy scheduler clumped same-event heats instead of spreading them across the 3 configured flights"
  - "FlightLogic.md §3.4 claim 'variety emerges naturally from competitor spacing' failed for events with disjoint competitor pools"
root_cause: logic_error
resolution_type: code_fix
related_components:
  - testing_framework
tags:
  - flight-builder
  - greedy-scheduling
  - scoring-heuristic
  - crowd-variety
  - per-event-cap
  - pro-flights
---

# Flight builder stacked all of one event's heats into a single flight

## Problem

Flight builder clumped every heat of single-event specialist events into one flight instead of spreading them across all three, producing a broken run-of-show where Flight 1 had all 4 Women's Underhand heats and 5 of 7 Men's Underhand heats while Flights 2 and 3 were nearly devoid of underhand content.

## Symptoms

- On a 3-flight, 53-heat, 9-event show, Flight 1 = 4/4 Women's Underhand + 5/7 Men's Underhand + Obstacle Pole + Pole Climb + Hot Saw.
- Flights 2 and 3 contained almost exclusively non-underhand events.
- Crowd-facing variety was destroyed: spectators saw the same stand type for 40+ minutes, then never again that day.
- Judge reported it as a bug against V2.11.x with a PDF showing the degenerate Saturday ordering.
- Affected any event whose competitor pool was disjoint from other events' pools (single-event specialists).

## What Didn't Work

`FlightLogic.md §3.4` claimed "variety emerges naturally from competitor spacing" — the greedy scorer would supposedly space events out via the `+1000` first-appearance bonus and the `+30` event-recency bonus. That assumption holds only when competitor pools overlap between events. For single-event specialists, every heat of their event is an "all-new-competitors" heat and scores `+1000` until the pool is exhausted. The `+30` variety bonus is dwarfed by the `+1000` first-appearance signal, so the greedy clumped consecutively with zero pressure against it.

## Solution

Added a hard per-event-per-flight cap penalty to `services/flight_builder.py` (commit `89f7f5b`, V2.12.0):

```python
EVENT_FLIGHT_CAP_PENALTY = 2000.0          # per-candidate step penalty per heat over cap
EVENT_FLIGHT_CAP_SCORE_PENALTY = 500.0     # per-ordering penalty for multi-pass comparison
```

In `_optimize_heat_order`, compute the cap per event based on how many flights the show will have:

```python
total_heats = len(all_heats)
target_flights = (
    max(1, math.ceil(total_heats / heats_per_flight))
    if heats_per_flight > 0 else 1
)
event_per_flight_cap: dict[int, int] = {
    eid: max(1, math.ceil(len(queue) / target_flights))
    for eid, queue in event_queues.items()
}
```

Threaded `event_per_flight_cap` and a new `event_heats_in_block: dict[tuple[int, int], int]` tracker through `_single_pass_optimize` and `_calculate_heat_score`, and added the penalty at the end of the scorer:

```python
if (event_per_flight_cap is not None and event_heats_in_block is not None
        and heats_per_flight > 0):
    event_id = getattr(event, 'id', None)
    if event_id is not None:
        current_block = current_position // heats_per_flight
        cap = event_per_flight_cap.get(event_id)
        if cap is not None:
            already = event_heats_in_block.get((current_block, event_id), 0)
            if already >= cap:
                score -= EVENT_FLIGHT_CAP_PENALTY * (already - cap + 1)
```

A matching penalty lives in `_score_ordering` for the best-of-N multi-pass comparison.

## Why This Works

The cap `ceil(N_e / F)` is the ceiling of even distribution for event `e` across `F` flights. Feasibility is guaranteed: `F × ceil(N_e/F) ≥ N_e` for all positive integers, so the sum of per-flight caps covers every heat. Per-event FIFO queues preserve sequential heat numbering within each event.

Penalty magnitude is deliberate: `2000` exceeds `+1000` (first-appearance) plus `+500` (springboard block-boundary opener) combined. The first over-cap placement takes a `-2000` hit, so any under-cap alternative always wins the greedy step, even when the over-cap candidate has maximum first-appearance and opener value.

The first-appearance vs variety tradeoff is now correctly ordered: first-appearance still shapes placement *within* a flight's allotment, but it can no longer override flight-level balance. Variety-by-spacing is demoted from load-bearing to cosmetic.

Verification against the reported PDF: Women's Underhand 1-1-2 (was 4-0-0), Men's Underhand 3-2-2 (was 5-1-1), Springboard 1-1-1, Hot Saw 1-1-1. Flight sizes 18/18/17 preserved. All events at or under cap.

## Prevention

- Regression test `test_even_event_distribution_across_flights` in `tests/test_flight_builder_integration.py` builds a 3-flight, 9-event show with disjoint competitor pools and asserts each event's per-flight count never exceeds `ceil(N_e/F)`. CI will fail if the penalty ever regresses.
- `FlightLogic.md §3.4` updated: the "variety emerges naturally from competitor spacing" claim removed and replaced with explicit language about the hard cap being the primary mechanism for event spread, with first-appearance as a secondary in-flight shaper.
- Memory feedback entry `feedback_flight_even_distribution.md` (indexed in MEMORY.md): "Flight first principle: each event's heats spread ceil(N_e/F) per flight, never clump." (auto memory [claude])
- Do NOT lower `EVENT_FLIGHT_CAP_PENALTY` below `1500`. Anything under `1500` fails to dominate the combined `+1000` first-appearance + `+500` springboard-opener score, and the clumping bug returns silently (test passes on synthetic inputs, breaks on real tournament data with springboard events). If future scoring signals are added above `+500`, raise the cap penalty in lockstep — it must exceed the sum of every positive score contribution.

## Related Issues

- PR #54 (this fix): `89f7f5b` — feat: V2.12.0 — even flight distribution + drag-drop + print polish
- Prior flight-builder PRs (context, not duplicates):
  - PR #47 — one-click Saturday build + FNF exclusion (added `MIN_HEATS_PER_FLIGHT = 2` clamp, different problem)
  - PR #41 — left-handed springboard constraint (different scoring concern)
- Stale docs to refresh (Phase 2.5 candidates):
  - `docs/HEAT_FLIGHT_AUDIT.md` line 29 — describes "greedy multi-pass flight ordering" without the cap-and-penalty layer
  - `CLAUDE.md` Section 5 flight-builder bullet — describes "greedy competitor-spacing" without the even-event-distribution guarantee
