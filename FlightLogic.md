# Flight Logic — Missoula Pro Am Manager

This document is the **source of truth** for all rules governing how flights are built, ordered,
and displayed for the Missoula Pro Am. Update this file when rules change. The code in
`services/flight_builder.py` and `services/heat_generator.py` is expected to match what is
written here. If you find a conflict, this document wins — fix the code.

---

## Table of Contents

1. [What a Flight Is](#1-what-a-flight-is)
2. [Competition Format Overview](#2-competition-format-overview)
3. [Pro Day Flight Rules](#3-pro-day-flight-rules)
   - 3.1 [Default Flight Size](#31-default-flight-size)
   - 3.2 [Competitor Spacing](#32-competitor-spacing)
   - 3.3 [Stand Conflict Rules](#33-stand-conflict-rules)
   - 3.4 [Event Order and Variety](#34-event-order-and-variety)
   - 3.5 [Partnered Axe Throw](#35-partnered-axe-throw)
   - 3.6 [Gear Sharing](#36-gear-sharing)
   - 3.7 [Dual-Run Pro Events](#37-dual-run-pro-events)
4. [College Saturday Overflow Rules](#4-college-saturday-overflow-rules)
   - 4.1 [Chokerman's Race (Mandatory Saturday)](#41-chokerma-ns-race-mandatory-saturday)
   - 4.2 [Other College Spillover Events](#42-other-college-spillover-events)
   - 4.3 [Priority Order for Saturday Overflow](#43-priority-order-for-saturday-overflow)
5. [Heat Generation Rules](#5-heat-generation-rules)
   - 5.1 [Snake Draft Distribution](#51-snake-draft-distribution)
   - 5.2 [Dual-Run Event Heat Generation](#52-dual-run-event-heat-generation)
   - 5.3 [Springboard Left-Hand Grouping](#53-springboard-left-hand-grouping)
   - 5.4 [Saw Stand Groups](#54-saw-stand-groups)
   - 5.5 [Partner Unit Placement](#55-partner-unit-placement)
   - 5.6 [Springboard Slow Heat Flag](#56-springboard-slow-heat-flag)
6. [Stand Configurations](#6-stand-configurations)
7. [Algorithm Details](#7-algorithm-details)
   - 7.1 [Greedy Heat Ordering](#71-greedy-heat-ordering)
   - 7.2 [Heat Scoring Formula](#72-heat-scoring-formula)
   - 7.3 [Constants](#73-constants)
8. [What the Flight Builder Does Not Do](#8-what-the-flight-builder-does-not-do)
9. [Build Order and Execution Sequence](#9-build-order-and-execution-sequence)
10. [Edge Cases and Special Handling](#10-edge-cases-and-special-handling)

---

## 1. What a Flight Is

A **flight** is a named group of heats that run sequentially during the pro competition show.
Flights are the primary unit of crowd management — the director calls each flight, the heats within
it run back-to-back, and then there is a break before the next flight.

- A flight contains heats from **multiple different events** to maintain crowd variety.
- A flight has a **status**: `pending`, `in_progress`, or `completed`.
- Flights are numbered sequentially starting at 1.
- Each heat belongs to exactly one flight (via `Heat.flight_id`).

Heats from the college day (Friday) do **not** get assigned to flights — flights are a Saturday
pro-day construct, except for Saturday college overflow heats (see Section 4).

---

## 2. Competition Format Overview

| Day | Division | Format |
|-----|----------|--------|
| Friday | College | Individual heats per event, no flights |
| Saturday | Pro | All heats grouped into flights |
| Saturday (end of day) | College overflow | Selected college heats placed into the last pro flight(s) |

The Saturday show has two distinct phases:
1. **Pro flights** — the main show. Heats from all pro events interleaved.
2. **College overflow** — a small number of college heats run on Saturday, placed at the back of
   the last flight or distributed across flights depending on the event (see Section 4).

---

## 3. Pro Day Flight Rules

### 3.1 Default Flight Size

- **Default:** 8 heats per flight.
- Flight count is calculated as `ceil(total_pro_heats / heats_per_flight)`.
- If there are no standard pro heats but there are Partnered Axe heats, one flight is created.
- The Partnered Axe Throw gets one heat per flight (see Section 3.5); this does not count toward
  the 8-heat cap.

### 3.2 Competitor Spacing

Every pro competitor must have adequate rest between their appearances in the heat schedule.

| Rule | Value | Behavior |
|------|-------|----------|
| Minimum spacing | 4 heats | A competitor cannot appear in two heats fewer than 4 positions apart in the ordered list. Violations are penalized in scoring but can still occur if unavoidable. |
| Target spacing | 5 heats | The preferred gap. Heats that meet or exceed 5-heat spacing for all competitors receive a score bonus. |
| First appearance | — | Heats where all competitors are appearing for the first time receive the highest possible score (1000) and are placed before heats with returning competitors. |

Spacing is measured in the **global heat sequence across all flights**, not within a single flight.

### 3.3 Stand Conflict Rules

**Cookie Stack and Standing Block** share the same 5 physical stands. These two events are mutually
exclusive — they cannot run simultaneously, and no heat from one event should be scheduled within
the same approximate flight slot as the other.

| Rule | Detail |
|------|--------|
| Enforced in | `flight_builder.py` (`_CONFLICTING_STANDS` dict, `_calculate_heat_score`) |
| Gap required | 8 heats minimum between a Cookie Stack heat and a Standing Block heat |
| Enforcement type | Hard disqualification: score returns `-1.0`, blocking placement at that position |

**NOTE:** This conflict is enforced during flight *ordering* but not during heat *generation*
(`heat_generator.py` does not check it). Heats are generated first, then the flight builder orders
them respecting the gap. As long as both events have heats, the builder will separate them.

No other events currently have stand conflict rules. If a new conflict needs to be enforced, add
the pair to `_CONFLICTING_STANDS` in `flight_builder.py` and update this document.

### 3.4 Event Order and Variety

**First principle: each event's heats spread across flights as evenly as possible.** No flight
may be dominated by one event.

The flight builder enforces a per-event per-flight cap:

| Quantity | Value |
|---|---|
| Cap per flight per event | `ceil(N_e / target_flights)` where `N_e` is that event's heat count |
| Step penalty | `EVENT_FLIGHT_CAP_PENALTY = 2000` per heat that would exceed the cap, applied in `_calculate_heat_score` |
| Ordering penalty | `EVENT_FLIGHT_CAP_SCORE_PENALTY = 500` per heat over cap, applied in `_score_ordering` for multi-pass comparison |

The cap is large enough to override the `+1000` first-appearance bonus and `+500` springboard
opener bonus — without it, a heat whose competitors appear in no other event always scored `1000`
(nothing forces spacing), so the greedy stacked all same-event heats together. Observed 2026-04-21
on a 3-flight show where all women's underhand + most of men's underhand landed in flight 1.

`ceil(N_e / F)` satisfies `F * cap >= N_e` always, so a feasible distribution exists. The per-event
queue still guarantees heats within an event appear in ascending `heat_number` order.

Events whose heats don't saturate a flight (e.g. 1 heat, or `N_e < F`) will still appear in only
some flights — the cap is an upper bound, not a floor. In that case there is nothing to spread.

### 3.5 Partnered Axe Throw

The Partnered Axe Throw has a special two-phase flow:

**Prelims (pre-show):** All registered pairs throw. Scores are recorded as hits. The top 4 pairs
advance to the show. This is managed by the `PartneredAxeThrow` state machine (`services/partnered_axe.py`).

**Show heats (during flights):** When flights are built, the top 4 pairs from prelims are rebuilt
into 4 individual show heats. One pair throws per flight. Placement rules:

- Show heats are distributed randomly across flights (one per flight, starting from flight 1).
- If there are more axe heats than flights, extra heats are double-booked into flights
  (modulo assignment).
- Prelim data is read from `Event.payouts` JSON. If prelim data is absent or malformed,
  existing axe heats are used as-is.
- Existing axe heats are **deleted and recreated** when prelim data is available. Do not rely on
  axe heat IDs persisting across a flight rebuild.

### 3.6 Gear Sharing

Competitors who share expensive equipment (springboards, crosscut saws, chainsaws) cannot be
placed in the same heat. This is enforced **during heat generation** in `heat_generator.py`.

- Gear sharing is stored as a JSON dict on each competitor: `{event_id: partner_name}`.
- The heat generator checks for conflicts before placing competitors into heats.
- If every heat would create a conflict (no valid placement), the constraint is relaxed as a
  fallback and the competitor is placed despite the conflict.
- The flight builder does **not** re-check gear sharing conflicts. The assumption is that the
  heat generator already handled them.

### 3.7 Dual-Run Pro Events

As of the current pro event list, **no pro events require dual runs**. The
`requires_dual_runs` flag on Event is only set for college events (Speed Climb, Chokerman's Race).

If a pro event is ever added with `requires_dual_runs=True`, the flight builder currently only
collects `run_number=1` heats. Run 2 heats would not be assigned to flights. This is a known
gap — update both this document and the flight builder if dual-run pro events are added.

### 3.8 Springboard Flight Opener

**Every flight should open with a pro springboard heat when one is available.**

This is enforced as a post-processing step after the greedy ordering:

1. After the global heat sequence is computed, scan each flight-sized block (default 8 heats).
2. If the first position (position 0) is already a pro springboard heat, do nothing.
3. If a pro springboard heat exists elsewhere in the block, move it to position 0.
4. If no springboard heat exists in the block, leave the block as-is.

**Sequential order is preserved:** only the first springboard heat in a block is moved.
Because the greedy algorithm has already placed springboard heats in ascending heat_number order
globally, the first springboard heat found in any block is always the correct next sequential one.
Promoting it to the front of its block does not skip or invert any heat_number.

**When there are no more springboard heats** (e.g., last flight after all springboard cuts are
done), the flight simply starts with whatever event the greedy algorithm placed there.

**Display order requirement:** once flights are assigned, each heat stores `flight_position`
within its flight. All flight/heat sheet pages must use `flight_position` order so the printed
or displayed first heat is the true opener from the builder.

---

## 4. College Saturday Overflow Rules

Some college events run on Saturday rather than Friday. These heats are generated as part of the
college event flow but must be placed into Saturday pro flights.

Overflow events are **judge-selected** using the Saturday Priority route. The judge decides which
college events spill to Saturday (except Chokerman's Race, which is automatic).

After the judge selects overflow events, the integration route (`integrate_college_spillover_into_flights`)
assigns those heats into the existing pro flights.

### 4.1 Chokerman's Race (Mandatory Saturday)

**The second run of Chokerman's Race must always occur on Saturday. This is non-negotiable.**

Rules:
- Only **Run 2** heats are placed on Saturday. Run 1 happens on Friday with the rest of college.
- All Run 2 Chokerman's Race heats are placed **together at the end of the last flight**.
- The heat order within the flight matches the Run 1 heat sequence (Heat 1 first, Heat 2 second,
  etc.), so competitors run the same relative order in both directions.
- Stand assignments for Run 2 are already reversed by the heat generator (Course 1 ↔ Course 2),
  so no additional stand swap is needed at flight assignment time.
- Chokerman's Race is automatically included in Saturday overflow — the judge does not need to
  manually select it.

**Why the last flight?** Chokerman's Race is a crowd-favorite finish event. Grouping all heats
together at the end of the day creates a clean climax rather than scattering them across the show.

### 4.2 Other College Spillover Events

All other college overflow events (Standing Block, Obstacle Pole, etc.) are distributed
**round-robin** across all pro flights in alphabetical+gender order.

- Only heats without an existing `flight_id` are assigned (previously integrated heats are not moved).
- Heats are ordered by `run_number` then `heat_number` within each event.
- No competitor spacing check is applied to college overflow heats. The TD should be aware that
  college competitors appearing in both Friday and Saturday events will have tighter overall
  schedules.

### 4.3 Priority Order for Saturday Overflow

When Friday scheduling is tight and additional events must move to Saturday, the preferred order
for which events are candidates (beyond Chokerman's Race Run 2) is:

1. Men's Standing Block Speed
2. Men's Standing Block Hard Hit
3. Women's Standing Block Speed
4. Women's Standing Block Hard Hit
5. Men's Obstacle Pole

This order is a guideline for the judge, not automatically enforced.

---

## 5. Heat Generation Rules

Heat generation (`services/heat_generator.py`) runs before flight building. Heats must exist
before flights can be built. The heat generator determines who is in each heat and what stand
they are assigned. The flight builder then orders and groups those heats into flights.

### 5.1 Snake Draft Distribution

Competitors are distributed across heats using a **snake draft** to ensure balanced skill levels:

```
Heat 1: Competitor A, F, K, P
Heat 2: Competitor B, G, J, O
Heat 3: Competitor C, H, I, N
...
```

The direction reverses at each end so no single heat is stacked with only top or only bottom
competitors.

Competitors are ordered as they appear in the database (registration order) unless a future
ability-grouping integration provides a sorted list.

### 5.2 Dual-Run Event Heat Generation

Events with `requires_dual_runs=True` generate **two sets of heats** — one for Run 1 (`run_number=1`)
and one for Run 2 (`run_number=2`).

- The competitor composition in each heat is **identical** between runs (same people, same heats).
- Stand assignments are **reversed** between runs so competitors swap lanes/courses.
  - Example: Run 1 uses stands [1, 2]; Run 2 uses stands [2, 1].
- Only Run 1 heats are used to build the pro flight order. Run 2 heats for college events
  are handled separately via Saturday overflow logic.
- The scoring system records `run1_value` and `run2_value`; the best (lowest) time counts as
  `result_value`.

### 5.3 Springboard Left-Hand Grouping

Left-handed springboard cutters require assignment to the same dummy. To prevent conflicts:

- Left-handed cutters are identified via `ProCompetitor.is_left_handed_springboard`.
- All left-handed cutters are grouped into a dedicated heat whenever capacity allows.
- Right-handed cutters fill remaining spots using snake draft.
- If there are no left-handed cutters, standard snake draft applies.

### 5.4 Saw Stand Groups

Hand saw events (Single Buck, Double Buck, Jack & Jill) use stands in two groups of 4:

- Stands 1–4 run while Stands 5–8 reset, then they swap.
- Maximum **4 competitors per heat** regardless of `max_stands` setting.
- `num_heats` is recalculated based on 4 per heat.

### 5.5 Partner Unit Placement

For partnered events (Double Buck, Jack & Jill, Peavey Log Roll, Pulp Toss, Partnered Axe):

- Partners are identified from `competitor.partners` JSON (`{event_id: partner_name}`).
- The heat generator attempts to find bidirectional partner references (A lists B, and/or B lists A).
- Recognized pairs are kept **together** in the same heat as a single unit during snake draft.
- An unpaired competitor (no recognized partner) is treated as a solo unit and placed normally.
- Gear sharing conflicts are still checked — even a recognized pair cannot be placed in a heat
  where one of them would share gear with another competitor already in that heat.

### 5.6 Springboard Slow Heat Flag

Judges may mark pro competitors for the springboard "slow heat" during pro entry import.

- Slow-heat competitors are identified via `ProCompetitor.springboard_slow_heat`.
- The springboard heat generator groups slow-heat competitors into a dedicated slow heat
  (typically the last springboard heat), then fills remaining spots via snake draft.
- Slow-heat grouping and left-handed grouping are both applied before the general fill pass.

---

## 6. Stand Configurations

Stand capacities and physical constraints per event type, as defined in `config.STAND_CONFIGS`:

| Stand Type | Total Stands | Notes |
|------------|-------------|-------|
| `springboard` | 4 | Supports handedness grouping; see Section 5.3 |
| `underhand` | 5 | Standard distribution |
| `standing_block` | 5 | Shares physical location with `cookie_stack` — mutually exclusive |
| `cookie_stack` | 5 | Shares physical location with `standing_block` — mutually exclusive |
| `saw_hand` | 8 | Two groups of 4 (Stands 1–4 and 5–8); 4 per heat max |
| `stock_saw` | 2 | Stands 1–2 only |
| `hot_saw` | 4 | Stands 1–4 only |
| `obstacle_pole` | 2 | Pole 1 and Pole 2 |
| `speed_climb` | 2 | Pole 2 and Pole 4 |
| `birling` | 1 | One pond; bracket format, not flight-based |

`Event.max_stands` overrides the `total` from `STAND_CONFIGS` when set. If neither is set,
the default is 4.

**College Stock Saw special rule:** College Stock Saw runs on stands 7 and 8 only
(the remaining saw stands after hand saw groups), not stands 1–2.

---

## 7. Algorithm Details

### 7.1 Greedy Heat Ordering

The flight builder uses a **per-event sequential greedy algorithm** to order heats before grouping
them into flights.

**Sequential constraint (required):** Within any event, heats must appear in ascending
`heat_number` order in the global sequence. Heat 1 must come before Heat 2, which must come before
Heat 3, and so on. It is never valid for a later heat number to appear before an earlier one for
the same event.

**Algorithm:**

1. Build a sorted queue for each event, ordered by `heat_number` then `run_number`.
2. Maintain a pointer per event to the next unplaced heat in that event's queue.
3. At each step, the set of **eligible candidates** is exactly one heat per event: the front of
   each event's queue (the next unplaced heat for that event).
4. Score all eligible candidates (see Section 7.2).
5. Select the candidate with the highest score and append it to the ordered list.
6. Advance that event's queue pointer by one.
7. Repeat until all queues are exhausted.

**Stand conflict fallback:** If all eligible candidates score `-1.0` (all blocked by stand
conflicts at this position), re-score ignoring stand conflicts and take the best-spacing choice.
This prevents deadlocks while still maintaining sequential order.

**Result:** Heats within every event always appear in order (1, 2, 3…) across the show schedule.
The greedy step still maximises competitor rest between appearances.

8. The final ordered list is the global heat sequence for the show.
9. Heats are then grouped into flights sequentially: the first 8 become Flight 1, the next 8
   become Flight 2, and so on.
10. A post-processing pass promotes the first pro springboard heat in each flight block to
    position 0 of that block (see Section 3.8), then persisted `flight_position` values are written.

This is O(n·e) per step where e is the number of events, giving overall O(n·e·n) = O(n²e).
For a typical Missoula Pro Am (30–60 heats, 15–20 events) this completes in well under one second.

### 7.2 Heat Scoring Formula

Each remaining heat is scored for placement at position `p` in the ordered list. Higher score =
better placement.

**Step 1 — Stand conflict check:**
If the heat's `stand_type` conflicts with a recently placed stand type (see `_CONFLICTING_STANDS`),
and the gap is less than `_STAND_CONFLICT_GAP` heats, **return -1.0** (disqualify for this position).

**Step 2 — Empty heat:**
If the heat has no competitors, **return 100.0** (can go anywhere).

**Step 3 — All-new competitors:**
If none of the competitors in this heat have appeared before, **return 1000.0** (best possible;
front-load new competitors).

**Step 4 — Minimum spacing check:**
For each competitor who has appeared before, calculate `spacing = p - last_position`.
Track the minimum spacing across all returning competitors in this heat.

If `min_spacing < MIN_HEAT_SPACING (4)`:
- Apply an exponential penalty: `score = max(0, 50 - (MIN_HEAT_SPACING - min_spacing) * 100)`
- Return this penalized score (does not disqualify; just deprioritizes).

**Step 5 — Normal score:**
If `min_spacing >= MIN_HEAT_SPACING`:
- `avg_spacing = total_spacing / returning_competitor_count`
- `score = (min_spacing * 10) + avg_spacing`
- If `min_spacing >= TARGET_HEAT_SPACING (5)`: add `+50` bonus.

### 7.3 Constants

These constants live in `services/flight_builder.py`. To change them, update the code **and**
this document.

| Constant | Value | Meaning |
|----------|-------|---------|
| `MIN_HEAT_SPACING` | 4 | Absolute minimum heats between a competitor's appearances |
| `TARGET_HEAT_SPACING` | 5 | Preferred spacing; bonus applied at this level |
| `_STAND_CONFLICT_GAP` | 8 | Minimum heats between conflicting stand types (approx. one full flight) |
| `EVENT_FLIGHT_CAP_PENALTY` | 2000 | Per-candidate penalty per heat over a flight's per-event cap |
| `EVENT_FLIGHT_CAP_SCORE_PENALTY` | 500 | Per-ordering penalty per heat over cap (multi-pass comparison) |
| `PARTNERED_AXE_SHOW_TEAM_COUNT` | 4 | Number of pairs that advance from prelims to the show |
| Default `heats_per_flight` | 8 | Target heats per flight (passed as argument to `build_pro_flights`) |

---

## 8. What the Flight Builder Does Not Do

These are confirmed gaps. Do not assume the system handles these automatically.

| Item | Notes |
|------|-------|
| Ability-based heat grouping | `optimize_flight_for_ability()` exists but is a no-op stub. Planned STRATHMARK integration point. |
| Gear sharing re-check at flight time | Assumed clean from heat generation. A gear-conflict heat that slipped through will not be caught here. |
| College-to-Saturday competitor spacing | No minimum spacing check is applied when college overflow heats are placed into pro flights. |
| Stand conflict check in heat generator | `_CONFLICTING_STANDS` is only enforced in flight ordering, not during initial heat generation. |
| Dual-run pro events | No pro events currently require dual runs. If one is added, Run 2 heats will not be placed in flights without code changes. |
| Friday Night Feature | No flight generation exists for Friday Night Feature events. |
| Manual flight editing | Drag-and-drop heat reordering within flights is supported via SortableJS UI. Event order within the day schedule is also manually reorderable. |
| Status validation on Flight.status | Any string can be written to `Flight.status`; the code does not enforce the allowed set. |

---

## 9. Build Order and Execution Sequence

The correct sequence to produce a valid Saturday schedule is:

1. **Configure events** — set up all pro events with stand types, competitor lists, payout structures.
2. **Generate heats per event** — run heat generation for each pro event. This creates
   `run_number=1` heats (and `run_number=2` for dual-run college events if needed).
3. **Run preflight checks** (optional but recommended) — verify no heat/table sync issues,
   no odd partner pools.
4. **Build pro flights** — call `build_pro_flights(tournament)`. This:
   - Deletes all existing flights.
   - Collects pro heats (run 1 only, excluding Partnered Axe).
   - Orders them using the greedy spacing algorithm.
   - Groups into flights of 8.
   - Rebuilds and distributes Partnered Axe show heats.
   - Commits to database.
5. **Integrate college overflow** (if any) — after the judge selects Saturday overflow events,
   call `integrate_college_spillover_into_flights()`. This:
   - Automatically includes Chokerman's Race Run 2 heats, placed at the end of the last flight
     in run-1 heat-number order.
   - Distributes other selected events round-robin across all flights.

**Rebuilding flights** at step 4 clears and recreates all flights. College overflow heats that
were previously integrated (have a non-null `flight_id`) are preserved — the integration step
skips heats that already have a flight assigned.

---

## 10. Edge Cases and Special Handling

**No heats exist:** `build_pro_flights` returns 0 and creates no flights. College overflow
integration will also return early with no-flights message.

**Only Partnered Axe heats exist:** One flight is created to hold the axe heats.

**Chokerman's Race has no Run 2 heats:** Integration skips the event silently. This should not
happen if heat generation was run correctly.

**More Partnered Axe heats than flights:** Heats are distributed modulo the flight count — some
flights will contain two axe heats. This is rare (should only be 4 show heats for 4+ flights).

**Competitor in only one event:** They appear in one heat total. Spacing constraints don't apply
to them.

**All competitors share gear:** The heat generator falls back to ignoring gear conflicts if no
valid placement exists. This is logged implicitly by the fallback branch.

**Event with `max_stands = 1`:** Each heat has one competitor. Heat count equals competitor count.
The spacing algorithm handles this normally.

**Empty heat in the pool:** Scored 100.0 (can go anywhere). Empty heats should not appear under
normal operation; they indicate a data issue.

**Score tie between two heats:** Python's `max()` over a list with `score > best_score` takes the
first-encountered winner. Tie-breaking is effectively first-in-list, which is acceptable.
