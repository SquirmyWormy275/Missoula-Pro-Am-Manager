---
module: scheduling
date: 2026-04-22
problem_type: best_practice
component: heat_generator
applies_when: "Spreading a constrained subset of competitors across heats using a deterministic split point (first N vs remaining). When the pool contains more constrained competitors than available slots."
severity: medium
resolution_type: pattern
tags:
  - heat-generation
  - ability-ranking
  - ProEventRank
  - springboard
  - lh-dummy
  - scheduling
  - sibling: lh-stand-4
---

# Sort by predicted ability BEFORE applying a resource-constraint spread

## Context

V2.14.0 Phase 5 codified the rule that Stand 4 is the LH springboard dummy and LH cutters spread one per heat to time-multiplex that single physical resource. When LH_count > num_heats the tail of the list overflows into the final heat, pooling with any `is_slow_springboard`-flagged cutters already clustering there.

Original implementation (V2.5.0 through V2.14.0 Phase 5):

```python
# services/heat_generator.py::_generate_springboard_heats
left_handed = [c for c in competitors if c.get('is_left_handed', False)]
spread = left_handed[:num_heats]        # who gets their own heat
overflow = left_handed[num_heats:]      # who piles into the final heat
```

The `competitors` list order was whatever `_get_event_competitors` produced — roughly registration / name-alpha order. With 4 LH cutters named Alice/Bob/Chris/Dan + 3 heats, Alice-Bob-Chris got their own LH-dummy slots and Dan overflowed into the final heat — purely by accident of alphabet.

If Alice is fast and Dan is slow, fine. If Dan is fast and Alice is slow, you just put the slow cutter on her own dedicated dummy while the fast cutter shares the overflow cluster. Operators want the opposite: fast cutters get dedicated slots, slow cutters share the slow-heat cluster.

## Guidance

**Whenever you split a constrained subset by a rule like "first N get their own slot, rest go to an overflow pool," sort the subset by predicted ability first.**

Concretely for this codebase:

```python
# services/heat_generator.py
from services.heat_generator import _sort_by_ability  # same helper _generate_standard_heats uses

left_handed = [c for c in competitors if c.get('is_left_handed', False)]
if left_handed:
    left_handed = _sort_by_ability(left_handed, event)  # rank 1 = fastest first

spread = left_handed[:num_heats]      # fastest N get their own heat
overflow = left_handed[num_heats:]    # slowest go to the overflow / slow-heat cluster
```

`_sort_by_ability` reads from `ProEventRank` (per-tournament, per-event-category ability ranks) and falls back to input order when no ranks are configured. So this is safe to add unconditionally — tournaments that haven't set up rankings keep the old behavior.

## Why This Matters

The pattern generalizes beyond LH springboard. Whenever the scheduler takes a "prefix gets resource, suffix doesn't" slice of a competitor list, the prefix is an implicit priority decision. If input order is registration order, you're ranking by **alphabet**. If it's random, you're ranking by **luck**. Either one makes the schedule worse than an operator with a pen and paper could produce.

Three concrete consequences for the Missoula Pro Am specifically:

1. **LH dummy utilization stays high.** Fast LH cutters each get a dedicated LH-dummy time-slot → the dummy sees its fastest runs on separate heats → crowd sees the best LH performances spread across the show instead of clumped together.
2. **Slow-heat clustering stays intentional.** LH overflow now lands in the final heat alongside `is_slow_springboard`-flagged cutters. The final heat becomes a consistent "slow heat" in the best sense — nobody sandbagged into a fast slot, nobody fast stuck with the slow group.
3. **Operator doesn't have to manually fix it.** Before PR #78, judges would rebuild flights, see the degenerate alphabetical LH distribution, and hand-swap competitors. The scheduler should win the first time.

The rule also applies to other one-per-heat spreadings that exist today and don't yet use ability sort:

- Slow-heat cluster placement itself — currently it takes `is_slow_springboard`-flagged competitors in input order. If ability rankings are fine-grained enough to distinguish "slow" from "slowest," applying `_sort_by_ability` to the `slow_heat` list lets the slowest cutter close the slow heat.
- Potential future use in partner/team building where paired units need priority spreading across heats.

## When to Apply

Apply `_sort_by_ability` whenever your code does `some_subset[:N]` / `some_subset[N:]` or a round-robin pick from a constrained pool where the pick order determines *who gets the better slot*. Check for this pattern during code review:

- `some_competitors[:num_heats]` — who gets the prioritized heat slot?
- `pool.pop(0)` inside a round-robin — is the first-pop deterministic by ability or by accident?
- Overflow handlers that fall back to "the rest" — the order of "the rest" is a real decision.

Do NOT apply when:

- The input list is already ability-sorted (e.g. already passed through `_sort_by_ability` upstream). `_sort_by_ability` is idempotent but extra calls pay a DB query.
- The ordering is explicitly supposed to be random (e.g. relay team draw), registration order (e.g. check-in roster), or a specific domain order (e.g. Birling bracket seeding which has its own `pre_seedings` input).

## Examples

**Before (V2.14.0 Phase 5 pre-release, name-order overflow):**

```python
left_handed = [c for c in competitors if c.get('is_left_handed', False)]
spread = left_handed[:num_heats]
overflow = left_handed[num_heats:]
```

Four LH cutters in registration order `[Alice, Bob, Chris, Dan]`, three heats → spread=`[Alice, Bob, Chris]`, overflow=`[Dan]`. The split point is alphabetical.

**After (PR #78, ability-order overflow):**

```python
left_handed = [c for c in competitors if c.get('is_left_handed', False)]
if left_handed:
    left_handed = _sort_by_ability(left_handed, event)
spread = left_handed[:num_heats]
overflow = left_handed[num_heats:]
```

Same four cutters, but with ranks `{Alice: 4, Bob: 1, Chris: 3, Dan: 2}` (1 = fastest) → sorted list = `[Bob, Dan, Chris, Alice]` → spread=`[Bob, Dan, Chris]`, overflow=`[Alice]`. Slowest LH cutter overflows. Fastest three each get their own LH-dummy slot.

**Behavior with no ranks configured:**

```python
# When ProEventRank has no rows for this tournament + category,
# _sort_by_ability returns the input list unchanged.
left_handed = _sort_by_ability([], event)  # -> []
left_handed = _sort_by_ability([a, b, c], event_with_no_ranks)  # -> [a, b, c]
```

No regression for tournaments that haven't configured rankings.

## Related Docs

- `services/heat_generator.py::_sort_by_ability` — the reusable helper
- `docs/solutions/best-practices/flight-builder-per-event-stacking-2026-04-21.md` — the V2.11.0 fix that introduced the "each event's heats spread evenly" first principle; this doc is the per-competitor analog
- `tests/test_lh_ability_ordering.py` — regression guards for this specific application
- `models/pro_event_rank.py` — the data model backing ability ranks
- `CLAUDE.md` §5.2 "Known Gaps" — `optimize_flight_for_ability()` in `flight_builder.py` remains a no-op stub; this doc is a partial resolution at the heat-generator layer. Flight-builder-level ability weighting across all event categories is still outstanding and remains the designated STRATHMARK integration point
