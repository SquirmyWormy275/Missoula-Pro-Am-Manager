# Heat & Flight Generation Audit

**Generated:** 2026-04-09
**Auditor:** Claude Code (systematic code read)
**Version:** V2.8.0

---

## Step 1: Files Discovered

### Core Models
| File | Role |
|------|------|
| `models/heat.py` | Heat, HeatAssignment, Flight model definitions |
| `models/event.py` | Event, EventResult model definitions |
| `models/competitor.py` | CollegeCompetitor, ProCompetitor models |
| `models/pro_event_rank.py` | ProEventRank — ability rankings for heat sorting |

### Heat Generation
| File | Role |
|------|------|
| `services/heat_generator.py` (688 lines) | Snake-draft heat generation with stand constraints |
| `services/gear_sharing.py` | Gear-family cascade conflict checking |
| `config.py` | STAND_CONFIGS, GEAR_FAMILIES, event lists |

### Flight Building
| File | Role |
|------|------|
| `services/flight_builder.py` (1035 lines) | Greedy multi-pass flight ordering, college spillover integration |
| `services/schedule_builder.py` (345 lines) | Day schedule assembly (Friday/Saturday blocks) |

### Route Layer
| File | Role |
|------|------|
| `routes/scheduling/__init__.py` (269 lines) | Blueprint, shared helpers, `_generate_all_heats`, `_build_pro_flights_if_possible` |
| `routes/scheduling/heats.py` (358 lines) | generate_heats, generate_college_heats, move_competitor_between_heats, heat_sync_check/fix |
| `routes/scheduling/flights.py` (238 lines) | build_flights, reorder_flight_heats, start/complete_flight, SMS |
| `routes/scheduling/birling.py` (262 lines) | Birling bracket generation and match recording (no heats/flights) |
| `routes/scheduling/events.py` | Event list, setup_events, day_schedule |
| `routes/scheduling/preflight.py` | Preflight validation |
| `routes/scoring.py` | Heat result entry (reads normalized HeatAssignment rosters) |

### Documentation
| File | Role |
|------|------|
| `FlightLogic.md` | Source-of-truth rules document for flight builder |

---

## Step 2 + 3: Per-File Analysis

---

### `services/heat_generator.py` — The Heat Engine

#### A. Algorithm: How competitors are assigned to heats

**Primary algorithm: Snake draft** (`_generate_standard_heats`, lines 287-340).

1. Competitors are sorted by **ProEventRank** ability ranking (rank 1 = best) via `_sort_by_ability()`. Unranked competitors sort to the end alphabetically. College events and pro events without rankings use registration order.
2. For **partnered events**, `_build_partner_units()` groups recognized pairs into 2-person units. Units are then re-sorted by composite rank via `_sort_units_by_ability()` (best member's rank drives position).
3. Units are placed using a snake draft: heat index bounces 0→N→0→N. Direction reverses at each end.
4. **Gear-sharing separation**: placement considers only heats with capacity and no declared gear conflict. College competitors are matched by their bare names rather than team-suffixed display names. When all existing heats conflict, generation expands the heat count and continues the snake draft. It never forces competitors sharing gear into the same heat; an invalid current-event declaration or unsatisfiable event fails before its existing schedule is replaced.

**Springboard variant** (`_generate_springboard_heats`, lines 435-533):
- Left-handed cutters are placed first into a dedicated heat (heat index 0).
- Slow-heat cutters (`springboard_slow_heat=True`) are placed into a dedicated heat (last heat index).
- Remaining cutters fill via snake draft with gear-conflict avoidance.

**Saw variant** (`_generate_saw_heats`, lines 536-549):
- Forces max 4 per heat (saw stand groups).
- Recalculates `num_heats` based on 4 per heat.
- Delegates to `_generate_standard_heats`.

#### B. Heat size rules

| Source | Rule | Enforcement |
|--------|------|-------------|
| `event.max_stands` | Authoritative when set | `heat_generator.py:106` |
| `config.STAND_CONFIGS[stand_type]['total']` | Fallback when `max_stands` is None | `heat_generator.py:106` |
| Hard default | 4 | When neither is set |
| Saw events | Max 4 regardless of config | `_generate_saw_heats` line 545 |
| All Stock Saw (pro and college) | Stands 7 and 8 only (max 2 per heat) | `_stand_numbers_for_event` |

**No minimum heat size is enforced.** A heat can have 1 competitor. The last heat in any snake draft will have fewer if competitors don't divide evenly. Empty heats are never created (the competitor list must be non-empty or `ValueError` is raised).

#### C. Competitor removal after generation

The route `move_competitor_between_heats` moves a competitor between normalized heat rosters:

- Removes the source `HeatAssignment` row
- Writes the destination `HeatAssignment` row with the next open stand
- For dual-run events, mirrors the move across both run_number=1 and run_number=2 heats
- Commits in a single transaction
- Checks for gear-sharing conflicts in the destination (warns but does not block)

Scratch and late-entry routes update the normalized roster and `EventResult` together. DNF/DQ
remain scoring statuses and do not imply a roster removal unless the operator uses Scratch.

#### D. SB/UH alternation logic

**There is no SB/UH alternation logic.** The heat generator treats each event independently. A competitor entered in both Standing Block and Underhand will have independent heats in each event. The only cross-event constraint is **gear-sharing conflict checking**, which prevents gear-sharing partners from being in the same heat. The `GEAR_FAMILIES` config groups underhand, standing_block, and springboard into the `'chopping'` family with `cascade: True`, so a gear conflict in any one cascades to all three.

The **flight builder** handles cross-event spacing (minimum 4 heats between a competitor's appearances), but within heat generation, each event is self-contained.

#### E. Marks/handicaps on add/remove

**Marks and handicaps are NOT automatically recalculated when a competitor is added to or removed from a heat.**

- `EventResult.handicap_factor` is populated by `services/mark_assignment.py` → `assign_handicap_marks()`, which is a separate manual action triggered via the `/scheduling/<tid>/events/<eid>/assign-marks` POST route.
- Moving a competitor between heats does not touch `handicap_factor` or `predicted_time`.
- Regenerating heats does not clear or recalculate marks. The `_delete_event_heats()` function deletes Heat and HeatAssignment rows but does NOT touch EventResult rows. Marks survive regeneration.
- A new competitor still begins with `handicap_factor=0.0`, but this no longer
  silently acts as a scratch. `EventResult.mark_assigned_at` stays empty until
  a judge or STRATHMARK explicitly reviews the row. Preflight lists such
  entrants and scoring blocks their heat until the mark-review page resolves
  them. Existing legacy rows also require a one-time operator confirmation.

#### F. How flights are composed from heats

See the Flight Builder section below.

#### G. Regeneration path

**Yes, there is a clean regeneration path.** `generate_event_heats(event)` (line 71):

1. Calls `_get_event_competitors(event)` which re-scans ALL active competitors (not just existing EventResult rows) — catches new registrations.
2. Creates missing `EventResult` rows for new entrants.
3. Builds and validates a complete conflict-free placement plan.
4. Calls `_delete_event_heats(event.id)` only after that plan succeeds, deleting all HeatAssignment rows then all Heat rows for the event.
5. Writes the new heats and calls `flush()` — it does NOT commit. The calling route owns the transaction.

**Important:** Regeneration preserves EventResult data (scores, marks, positions) but replaces heat assignments. Every generation call joins the
tournament schedule-writer lock. Standalone and bulk regeneration refuse any
event whose heats are already assigned to flights; operators must use Generate
All / One-Click Generate so heat replacement and flight rebuilding commit as a
single transaction. Finalized, completed, and scored history is always blocked.

The route (`heats.py:71-135`) wraps this in try/except with `db.session.rollback()` on failure and `db.session.commit()` on success.

#### H. Transaction safety

| Operation | Safety |
|-----------|--------|
| `generate_event_heats()` | `flush()` only — caller commits. Good. |
| `_generate_all_heats()` in `__init__.py` | Uses one savepoint per event, then raises on any non-skippable failure so the owning schedule transaction rolls every generated event back. |
| `generate_heats` route | Locks the Tournament row, refuses flighted heats, commits once after success, and rolls back on exception. |
| `generate_college_heats` route | Locks the Tournament row, rejects the entire bulk request before mutation if any selected event is already flighted, and uses one savepoint per event before the final commit. |
| `move_competitor_between_heats` | Single `commit()` after all moves. Good. |

#### I. HeatAssignment authority

`HeatAssignment` rows are the only roster store. Generation and all day-of mutations write them
directly through `Heat.set_roster()` or row-level helpers. The former heat JSON roster columns and
the sync-check/sync-fix repair path have been removed, so there is no second copy that can drift.

---

### `services/flight_builder.py` — The Flight Engine

#### A. Algorithm

**Multi-pass greedy with per-event sequential queues** (`_optimize_heat_order`, `_single_pass_optimize`):

1. One sorted queue per event (by heat_number, run_number).
2. At each step, only the NEXT unplaced heat from each event is eligible (sequential guarantee — Heat 1 before Heat 2 before Heat 3).
3. Each candidate is scored by `_calculate_heat_score()` considering:
   - Stand conflict (cookie_stack / standing_block: -1 disqualification within 8 heats)
   - Per-event tiered spacing (springboard min=6/target=8, saw min=5/target=7, others min=4/target=5)
   - Springboard opener bonus (+500 at position 0 of flight block)
   - Hot Saw closer bonus (+300 at last position of flight block)
   - Event recency bonus (+30 for new-to-block events)
   - Gear adjacency penalty (-200 per back-to-back gear partner)
4. Runs N_OPTIMIZATION_PASSES=5 passes with rotated event order. Keeps the best result.
5. Post-processing promotes springboard heats to flight opener position.

#### B. Heat size in flights

Default 8 heats per flight. Can be overridden by the judge via `num_flights` form field — `heats_per_flight = ceil(total / num_flights)`.

Partnered Axe Throw heats are inserted AFTER flight creation (one per flight, not counted in the 8-heat cap). Pair coverage, prelim completion, and finalist advancement are validated from `Event.event_state`; ordinary competitor partner fields do not govern this state-machine event. Completed prelim result rows do not protect the first finals-card build, but any finals score, completed finals heat, or completed state does.

#### C. College spillover integration

`integrate_college_spillover_into_flights()`:
- Chokerman's Race Run 2: all heats placed at end of last flight (show climax).
- Repeated integration inserts new spillover before an existing pending Chokerman closing block; completed flight history rejects new placement.
- Other spillover events: selected only from the current tournament's college events and distributed using saved round-robin or earliest-flight cluster mode.
- Every candidate is evaluated in the true projected global Saturday order, including both earlier and later appearances of the same namespaced assignment UID.
- Candidate ranking preserves MIN_HEAT_SPACING first, then avoids new shared-stand conflicts; unavoidable fallbacks minimize conflict count and gap shortfall before applying the mode preference.
- Newly introduced unavoidable shared-stand conflicts are returned explicitly and surfaced by preflight.
- Flight rows are locked during integration, existing flight heats are batch-loaded once, and missing, non-positive, or duplicate positions are rejected before mutation.
- Preserves existing placements (skips heats with non-null `flight_id`).

#### D. Transaction safety

`build_pro_flights()` can still commit when invoked as a standalone service. Every active route that builds the complete Saturday show passes `commit=False` through flight build, relay placement, and college spillover, then commits once after all three phases succeed. A spillover or closer-invariant failure therefore restores the pre-request flight assignments instead of leaving a rebuilt show without mandatory closing heats.

Synchronous and background full-show builds use the same service sequence: a
fresh input-phase preflight, heat generation, flight build, Pro-Am Relay,
college spillover, saw-block assignment, and completed-show validation. They
commit once after every phase succeeds. Operator success messages and build-diff
snapshots are emitted only after that commit; any blocker or downstream failure
rolls the whole build back and discards success messages from the failed attempt.

---

### `routes/scheduling/heats.py` — Route-Level Operations

#### move_competitor_between_heats (lines 185-279)

The only supported day-of competitor movement operation:
- POST with `competitor_id`, `from_heat_id`, `to_heat_id`
- Validates competitor is in source heat
- For dual-run events, mirrors move across both runs
- Assigns next available stand in destination
- Writes the normalized HeatAssignment roster rows
- Checks gear-sharing conflicts (warn only, doesn't block)
- Single commit

---

### `models/heat.py` — Data Model

**Heat model** stores:
- `flight_id` / `flight_position`: flight membership
- `locked_by_user_id` / `locked_at`: scoring lock
- `version_id`: optimistic locking for concurrent edits

**HeatAssignment model** is the authoritative roster:
- `heat_id`, namespaced `uid`, legacy `competitor_id` / `competitor_type`, and `stand_number`
- Row order is roster order; `Heat.get_competitors()` and `get_stand_assignments()` render directly from these rows
- The former heat JSON roster columns have been dropped and are not a fallback store
- No manual synchronization is required or available

**Flight model** is lightweight:
- `tournament_id`, `flight_number`, `name`, `status`, `notes`
- Heats reference flights via `Heat.flight_id`

---

## Step 4: Gap Analysis

### Day-of Operations Currently Supported

| Operation | Route/Method | Notes |
|-----------|-------------|-------|
| Move competitor between heats | `POST /scheduling/<tid>/event/<eid>/move-competitor` | Mirrors dual-run heats; blocks gear conflicts, completed rosters, capacity, and conflicting scoring locks. |
| **Scratch competitor from heat** | `POST /scheduling/<tid>/event/<eid>/scratch-competitor` | Removes the normalized roster row, frees stand, sets EventResult.status='scratched', cleans gear refs, recalcs positions if scored, mirrors dual-run. Lock check. Audit logged. |
| **Add late entry to heat** | `POST /scheduling/<tid>/event/<eid>/add-to-heat` | Adds competitor, assigns stand, creates EventResult if missing, re-add of scratched resets to pending + clears derived fields. Blocks completed rosters, conflicting locks, and gear conflicts; mirrors dual-run. Audit logged. |
| **Delete empty heat** | `POST /scheduling/<tid>/event/<eid>/delete-heat/<hid>` | Validates 0 competitors, blocks any event with a completed heat, then deletes and renumbers the remaining pending heats. Mirrors dual-run delete. Audit logged. |
| Regenerate heats for one event | `POST /scheduling/<tid>/event/<eid>/generate-heats` | Rebuilds only an unscored, non-finalized, unflighted event. Flighted heats require the atomic full-schedule workflow. |
| Bulk regenerate college heats | `POST /scheduling/<tid>/generate-college-heats` | All non-completed, non-finalized college events. The entire request is rejected before mutation if any target event is flighted. |
| Rebuild all flights | `POST /scheduling/<tid>/flights/build` | Destroys and rebuilds all flights |
| Reorder heats within a flight | `POST /scheduling/<tid>/flights/<fid>/reorder` | Drag-and-drop via JSON before operations start; rejects sequence, stand, closer, and active-flight violations. |
| Mark flight started/completed | `POST /scheduling/<tid>/flights/<fid>/start` | Sends SMS to competitors in upcoming flights |

### Day-of Operations NOT Supported

| Operation | Impact | Workaround |
|-----------|--------|------------|
| **Swap two competitors between heats** | The move route only supports one-directional moves. A true swap (A→Heat2, B→Heat1 atomically) requires two sequential moves. | Two separate move operations. |
| **Re-assign marks after heat change** | Moving a competitor does not calculate a new handicap mark. Existing marks remain subject to explicit review, and a new entrant must be assigned and reviewed before scoring. | Use the mark-review page; scoring blocks unreviewed handicap entrants. |
| **Insert a heat into an existing flight** | No route adds a single heat to a flight. Flight rebuild is all-or-nothing. | Rebuild all flights. |
| **Partially regenerate (one heat only)** | Regeneration is all-or-nothing per event. No way to rebuild just Heat 3 of 5. | Regenerate all heats for the event. |

### Enforcement Gaps (Intent vs Implementation)

| Rule | Intent | Actual Enforcement |
|------|--------|--------------------|
| **Heat size maximum** | Competitors per heat ≤ max_stands | Enforced during generation AND on move/add operations via `_max_per_heat()`. |
| **Same-heat gear separation** | Every competitor or partnered unit sharing declared gear must run in a different heat | **ENFORCED.** Generation expands heat count instead of forcing a conflict and fails before replacing existing heats if no valid plan exists. Manual move, add, and flight-board drag reject conflicts. |
| **Gear conflict in destination** | Moving/adding a competitor must not create a shared-gear conflict | **ENFORCED.** Manual move, add, and flight-board drag reject the conflict. |
| **Cookie Stack / Standing Block shared stands** | Keep an eight-heat separation whenever another heat is available | The flight builder blocks the close placement while alternatives remain; if only conflicts remain, it deliberately schedules sequentially because a flight has one heat per slot. Heat generation is event-local. |
| **Event.is_finalized guard on regeneration** | Finalized or scored events should not have heats regenerated | **ENFORCED.** Finalized and completed-result events are hard blocks. Partnered Axe prelim results are the deliberate exception until finals scoring starts. |
| **Heat lock on mutations** | Locked heats should not be mutated by other judges | **ENFORCED** on scratch, add, delete, manual move, and flight-board drag operations. |
| **HeatAssignment roster authority** | One durable roster source | Generation, move, scratch, add, and scoring readers use normalized rows directly. |
| **Competitor spacing in college overflow** | College overflow should respect spacing | Implemented in `integrate_college_spillover_into_flights` with MIN_HEAT_SPACING check and fallback. |
| **Sequential heat order** | Heat 1 before Heat 2 within each event | Enforced in the builder and on both manual flight reorder APIs. |
| **Show-start lockout on additions** | No late entries after show starts | **ENFORCED** on add-to-heat. Blocked when tournament.status is active for that division. Scratches always allowed. |
| **Show-start schedule freeze** | Running order, rosters, and flight-generation inputs become race-day record when a flight starts | **ENFORCED** on single/bulk flight reorder, drag move, manual move, late add, empty-heat deletion, saw-block reassignment, gear-sharing heat synchronization, event setup, Friday Feature selection, and Saturday priority. Scratch cascade remains available. Display-order preferences remain separate serialized configuration. |
| **Destructive registration history guard** | Hard delete must not erase scoring history | College competitor/team deletion locks the tournament and refuses any non-pending result or heat history, plus active operations; judges use scratch instead. |
| **Birling publication integrity** | Final standings must represent the complete bracket | Publication requires exactly one contiguous placement for every entrant. Once non-pending Birling results exist, bracket reset and regeneration are blocked, and contradictory existing results cannot be overwritten. |

### Critical Day-of Risks

1. **Regeneration replaces heat assignments but not results**: Direct regeneration could otherwise detach a published flight or orphan scored history. **Mitigated:** flighted heats require the atomic full-schedule workflow, and finalized/completed/scored history remains immutable.

2. **Undo window race condition**: result undo is revision-bound to the heat and result records. A concurrent change returns a conflict instead of clearing a later edit.

3. **Schedule writers are serialized**: SQLite uses a per-tournament process guard; PostgreSQL
   writers lock and refresh the Tournament row first. Scoring then reloads the locked Heat and
   Event before checking the posted revision. The form also carries the exact heat-lock instance
   timestamp, so SQLite primary-key reuse cannot bind an old form to a replacement heat. Roster
   moves, scratch/undo, CSV result import, throw-offs, event finalization, payout edits,
   points-cache repair, Partnered Axe state, Birling bracket state, flight order, generation,
   and event-order config writes use the same parent-first protocol. Birling result publication and
   standings rebuild commit atomically only after the bracket has a complete placement set; published
   Birling history blocks reset and regeneration. Generation inputs such as event setup, ability rankings,
   registration event/partner edits, and college entry import hold that lock through final commit;
   the Excel service supports caller-owned commit so it cannot release the route lock midway.
   Direct heat generation refreshes its Event after locking. Background builds receive one immutable
   flight-sizing and spillover-selection snapshot captured under that lock. Primary generation and
   rebuild chains also assign saw blocks before their final commit, so a saw-assignment failure rolls
   back the heat and flight schedule instead of leaving a partially usable show.

---

## Day-Of Operations Runbook

### Decision Tree: When to Scratch vs Move vs Regenerate

```
Competitor no-shows or is injured?
  └─ YES → Use SCRATCH (removes from heat, preserves result record)
      └─ If partnered event: scratch partner too or find substitute

Competitor in wrong heat?
  └─ YES → Use MOVE (transfers between heats with stand assignment)

Late registration, competitor not in any heat?
  └─ YES → Use ADD TO HEAT (creates EventResult if needed)
      └─ BLOCKED if show has started for that division

Heat is empty after scratches?
  └─ YES → Use DELETE EMPTY HEAT (cleans up, renumbers remaining)

Need to completely redo heat assignments for an event?
  └─ YES → Use REGENERATE (destroys all heats, rebuilds from scratch)
      └─ WARNING: Orphans any scored results. Blocked if finalized.
```

### Step-by-Step: Scratch a Competitor

1. Navigate to Events → [Event Name] → Heats
2. Find the heat containing the competitor
3. Expand "Scratch Competitor" in the heat card footer
4. Select the competitor from the dropdown
5. Click "Scratch" and confirm the dialog
6. The competitor is removed from the heat and marked (SCR) on heat sheets
7. If the event had scored results, positions are automatically recalculated

### Step-by-Step: Add a Late Entry

1. Navigate to Events → [Event Name] → Heats
2. Find the target heat (must have room — check stand count)
3. Expand "Add Competitor" in the heat card footer
4. Select the competitor from the dropdown (shows only unassigned competitors)
5. Click "Add"
6. The competitor is added with the next available stand number
7. For dual-run events, they are added to both run heats automatically

### Step-by-Step: Delete an Empty Heat

1. Navigate to Events → [Event Name] → Heats
2. Find the empty heat (shows "No competitors")
3. Click "Delete Empty Heat" button (only appears on empty heats)
4. Confirm the dialog
5. Remaining heats are renumbered sequentially
6. **Reprint heat sheets if already distributed to judges**

### Regeneration After Scoring

Regeneration of a completed, finalized, or scored event is rejected by both the route and service. Partnered Axe prelim scores remain mutable qualifier history for the first finals-card build; its card locks as soon as finals scoring or completed finals heat history exists.
Completed heat placements and EventResult rows remain immutable. Use scratch for no-shows; before
operations start, use add or move for roster corrections. After a flight starts, manual roster and
running-order edits are also frozen.
