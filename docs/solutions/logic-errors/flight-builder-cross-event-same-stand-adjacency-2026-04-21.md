---
title: Cross-event same-stand heats placed back-to-back in flights
date: 2026-04-21
category: logic-errors
module: flight_builder
problem_type: logic_error
component: service_object
severity: medium
symptoms:
  - "Men's Underhand heat immediately followed by Women's Underhand heat (both use the 5 underhand stands)"
  - "Single Buck → Jack & Jill back-to-back in the same flight (both use 8 hand-saw stands)"
  - "Same physical stands reused with no reset time and no crowd-variety break"
root_cause: logic_error
resolution_type: code_fix
related_components:
  - testing_framework
tags:
  - flight-builder
  - greedy-scheduling
  - stand-adjacency
  - cross-event
  - crowd-variety
---

# Cross-event same-stand heats placed back-to-back in flights

## Problem

Even after the V2.12.0 per-event distribution cap spread heats across flights, consecutive placements could still reuse the same physical stands. Men's Underhand followed immediately by Women's Underhand both drew from the 5 underhand stands with no reset; same for any pair among Single Buck / Double Buck / Jack & Jill sharing the 8 hand-saw stands.

## Symptoms

- On a 3-flight / 53-heat show, Flight 1 would contain Men's UH H1 → Women's UH H1 back-to-back at positions 4-5
- Same issue between sawing events: Men's Single Buck → Jack & Jill at gap=1
- Crew had to reset the same stands with no break; crowd saw two structurally identical events in sequence
- Judge reported visually as "same stands keep getting reused"

## What Didn't Work

The V2.12.0 fix added a per-event distribution cap (`ceil(N_e/F)` with a 2000-point penalty per over-cap heat). That correctly spread each event across flights, but said nothing about WITHIN-flight ordering of events that share a `stand_type`. The existing `_CONFLICTING_STANDS` dict handled only Cookie Stack ↔ Standing Block mutual exclusion (stand_type conflict between different types). It didn't penalize two events that share the same stand_type.

An earlier attempt applied the penalty to ALL same-stand-type adjacencies including intra-event (Men's UH H1 → Men's UH H2). That broke the existing scoring balance: in `_score_ordering`, the cumulative penalty across every adjacent pair of same-event same-stand heats outweighed the distribution cap penalty, causing clumping to score BETTER than interleaving. Two unit tests failed (`test_well_spaced_competitors_score_higher`, `test_two_lh_heats_in_same_flight_trigger_penalty`) because both used single-event-type scenarios where the sum-across-pairs penalty inverted the comparison.

## Solution

Scope the penalty to **cross-event** same-stand-type adjacency only. Intra-event sequential heats (MUH H1 → H2) are already handled by the competitor-spacing scoring and the distribution cap — re-penalizing them fights the existing balance.

New constants in `services/flight_builder.py`:

```python
# Same-stand-type CROSS-event adjacency: events sharing a stand_type use the same
# physical stands. Back-to-back (gap=1) forces the crew to reset with no break.
_SAME_STAND_TYPE_MIN_GAP = 2
_SAME_STAND_TYPE_PENALTY = 200.0
```

New tracker threaded through `_single_pass_optimize` → `_calculate_heat_score`:

```python
# (stand_type) -> (last_position, last_event_id)
stand_type_last_event: dict[str, tuple[int, int]] = {}
```

Penalty block at the end of `_calculate_heat_score`, scoped to different-event only:

```python
if stand_type and stand_type_last_event is not None:
    last_event_of_stand = stand_type_last_event.get(stand_type)
    if last_event_of_stand is not None:
        last_pos, last_eid = last_event_of_stand
        event_id = getattr(event, 'id', None)
        if event_id is not None and last_eid != event_id:
            gap = current_position - last_pos
            if gap < _SAME_STAND_TYPE_MIN_GAP:
                score -= _SAME_STAND_TYPE_PENALTY * (_SAME_STAND_TYPE_MIN_GAP - gap)
```

Matching penalty in `_score_ordering` for the best-of-N multi-pass comparison, also scoped to cross-event.

## Why This Works

**Penalty magnitude:** 200 × (2 - 1) = 200 for adjacent cross-event same-stand. Smaller than the 2000-point distribution cap penalty, so variety-across-flights still dominates scheduling. Large enough to outweigh the +30 recency and +50 target-spacing bonuses that would otherwise prefer an adjacent placement.

**Why cross-event only:** Intra-event sequential heats are the NATURAL order of a competition — Men's UH H1, H2, H3 are expected to run in sequence (with competitor spacing between them via the existing `min_spacing`/`target_spacing` scoring). The cap already limits how many end up in one flight. Penalizing intra-event same-stand would double-count concerns the other mechanisms already handle, and it empirically broke the scoring balance.

**Feasibility:** With the PDF's 11 underhand heats across 42 non-underhand positions on a 53-heat show, a non-adjacent interleave exists for every underhand heat. Verified empirically: zero cross-event same-stand adjacencies in the final flight order.

## Prevention

- Regression test `test_no_same_stand_type_adjacency` in `tests/test_flight_builder_integration.py` — 12-heat / 6-event scenario, asserts ≤1 back-to-back cross-event same-stand pair in the full ordering.
- Memory feedback entry `feedback_flight_even_distribution.md` still stands as the durable rule for per-event distribution cap. This doc is the adjacency companion.
- If adding new stand_types or scoring bonuses in the future: the penalty magnitude (200) must remain smaller than the cap penalty (2000) and larger than any single positive bonus that would want to place an adjacent heat. Recalibrate in lockstep.
- Do NOT re-scope the penalty to include intra-event adjacency without updating the two scoring unit tests that were adjusted for this fix — they test single-event scenarios where intra-event cumulative penalty inverts the comparison.

## Related Issues

- PR #55 (merged as `e4e45a0`): this fix bundled with rebuild-chains-spillover and drag-drop
- PR #54 (`89f7f5b`, V2.12.0): prerequisite — per-event distribution cap. Without that, this fix doesn't make sense in isolation.
- [flight-builder-per-event-stacking-2026-04-21.md](./flight-builder-per-event-stacking-2026-04-21.md): the V2.12.0 doc. The cross-event adjacency rule is a follow-up to that first principle.
- FlightLogic.md §3.4 — updated to explain both the cap AND the adjacency penalty as the two mechanisms enforcing crowd variety.
