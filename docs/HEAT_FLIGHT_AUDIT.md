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
| `routes/scoring.py` | Heat result entry (reads Heat.competitors for scoring) |

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
4. **Gear-sharing conflict avoidance**: first pass tries to find a heat with capacity AND no gear conflict. If all heats conflict or are full, a fallback pass places despite conflict and records the violation for the route to surface as a warning.

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
| College Stock Saw | Stands 7 and 8 only (max 2 per heat) | `_stand_numbers_for_event` line 604-606 |

**No minimum heat size is enforced.** A heat can have 1 competitor. The last heat in any snake draft will have fewer if competitors don't divide evenly. Empty heats are never created (the competitor list must be non-empty or `ValueError` is raised).

#### C. Competitor removal after generation

The `Heat` model provides `remove_competitor(competitor_id)` (line 87-91) which edits the JSON list. The route `move_competitor_between_heats` (heats.py:185-279) is the only UI path that moves a competitor between heats:

- Removes from source heat JSON + stand_assignments
- Adds to target heat JSON + assigns next open stand
- Calls `sync_assignments()` on both heats
- For dual-run events, mirrors the move across both run_number=1 and run_number=2 heats
- Commits in a single transaction
- Checks for gear-sharing conflicts in the destination (warns but does not block)

**There is no scratch/drop mechanism at the heat level.** If a competitor is removed entirely (not moved), you must either:
1. Manually edit the heat (no UI for "remove without destination")
2. Regenerate heats for the entire event

**EventResult.status can be set to 'scratched' or 'dnf'** but this does NOT automatically remove the competitor from their heat. The heat JSON and the result status are completely decoupled.

#### D. SB/UH alternation logic

**There is no SB/UH alternation logic.** The heat generator treats each event independently. A competitor entered in both Standing Block and Underhand will have independent heats in each event. The only cross-event constraint is **gear-sharing conflict checking**, which prevents gear-sharing partners from being in the same heat. The `GEAR_FAMILIES` config groups underhand, standing_block, and springboard into the `'chopping'` family with `cascade: True`, so a gear conflict in any one cascades to all three.

The **flight builder** handles cross-event spacing (minimum 4 heats between a competitor's appearances), but within heat generation, each event is self-contained.

#### E. Marks/handicaps on add/remove

**Marks and handicaps are NOT automatically adjusted when a competitor is added to or removed from a heat.**

- `EventResult.handicap_factor` is populated by `services/mark_assignment.py` → `assign_handicap_marks()`, which is a separate manual action triggered via the `/scheduling/<tid>/events/<eid>/assign-marks` POST route.
- Moving a competitor between heats does not touch `handicap_factor` or `predicted_time`.
- Regenerating heats does not clear or recalculate marks. The `_delete_event_heats()` function deletes Heat and HeatAssignment rows but does NOT touch EventResult rows. Marks survive regeneration.
- If a new competitor is added to an event after marks were assigned, they will have `handicap_factor=0.0` (scratch) unless marks are reassigned.

#### F. How flights are composed from heats

See the Flight Builder section below.

#### G. Regeneration path

**Yes, there is a clean regeneration path.** `generate_event_heats(event)` (line 71):

1. Calls `_get_event_competitors(event)` which re-scans ALL active competitors (not just existing EventResult rows) — catches new registrations.
2. Creates missing `EventResult` rows for new entrants.
3. Calls `_delete_event_heats(event.id)` which deletes all HeatAssignment rows then all Heat rows for the event.
4. Generates new heats from scratch.
5. Calls `flush()` — does NOT commit. The calling route owns the transaction.

**Important:** Regeneration preserves EventResult data (scores, marks, positions) but destroys all heat assignments. After regeneration, scored heats no longer map to any Heat row. This is safe pre-scoring but dangerous post-scoring.

The route (`heats.py:71-135`) wraps this in try/except with `db.session.rollback()` on failure and `db.session.commit()` on success.

#### H. Transaction safety

| Operation | Safety |
|-----------|--------|
| `generate_event_heats()` | `flush()` only — caller commits. Good. |
| `_generate_all_heats()` in `__init__.py` | Uses `db.session.begin_nested()` per event — savepoint isolation. Excellent. |
| `generate_heats` route | Single `db.session.commit()` after success, `rollback()` on exception. Good. |
| `generate_college_heats` route | Single `commit()` after ALL events. If event 15/20 fails, events 1-14 are committed but 15-20 are lost. **Gap:** no per-event savepoint here (unlike `_generate_all_heats`). |
| `move_competitor_between_heats` | Single `commit()` after all moves. Good. |

#### I. Heat.competitors JSON vs HeatAssignment rows

**Heat.competitors (JSON) is the authoritative source.** All heat generation code writes to `Heat.competitors` via `set_competitors()`. After generation, `heat.sync_assignments(event.event_type)` is called to rebuild HeatAssignment rows from the JSON.

`sync_assignments()` (heat.py:111-125):
1. Deletes all existing HeatAssignment rows for the heat
2. Creates new rows from `get_competitors()` + `get_stand_assignments()`

**Known divergence risk:** Any code that modifies HeatAssignment directly (without updating Heat.competitors) will cause drift. The sync-check/sync-fix routes (heats.py:305-357) exist to detect and repair this. The validation service also reads HeatAssignment rows, so drift can cause phantom validation failures.

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

Partnered Axe Throw heats are inserted AFTER flight creation (one per flight, not counted in the 8-heat cap).

#### C. College spillover integration

`integrate_college_spillover_into_flights()` (lines 898-994):
- Chokerman's Race Run 2: all heats placed at end of last flight (show climax).
- Other spillover events: distributed round-robin across flights with MIN_HEAT_SPACING respect for cross-division competitors.
- Fallback: if no flight has adequate spacing, place anyway.
- Preserves existing placements (skips heats with non-null `flight_id`).

#### D. Transaction safety

`build_pro_flights()`: deletes all existing flights and heat flight assignments, rebuilds, then calls `db.session.commit()`. This is an all-or-nothing rebuild. **Good.**

The route layer (`flights.py:91-98`) wraps in try/except but the commit is inside `build_pro_flights` itself — a partial failure after commit would leave inconsistent state. However, since the function deletes first and builds second, a failure mid-build would leave zero flights (not partial), which is recoverable by re-running.

---

### `routes/scheduling/heats.py` — Route-Level Operations

#### move_competitor_between_heats (lines 185-279)

The only supported day-of competitor movement operation:
- POST with `competitor_id`, `from_heat_id`, `to_heat_id`
- Validates competitor is in source heat
- For dual-run events, mirrors move across both runs
- Assigns next available stand in destination
- Syncs HeatAssignment rows
- Checks gear-sharing conflicts (warn only, doesn't block)
- Single commit

#### heat_sync_check / heat_sync_fix (lines 305-357)

- `sync-check` (GET): returns JSON comparing Heat.competitors JSON against HeatAssignment rows
- `sync-fix` (POST): rebuilds HeatAssignment from authoritative JSON

---

### `models/heat.py` — Data Model

**Heat model** stores:
- `competitors` (JSON TEXT): ordered list of competitor IDs — **authoritative**
- `stand_assignments` (JSON TEXT): dict mapping competitor_id → stand_number
- `flight_id` / `flight_position`: flight membership
- `locked_by_user_id` / `locked_at`: scoring lock
- `version_id`: optimistic locking for concurrent edits

**HeatAssignment model** is a normalized mirror:
- `heat_id`, `competitor_id`, `competitor_type`, `stand_number`
- Used by validation service for relational queries
- Must be synced manually via `sync_assignments()`

**Flight model** is lightweight:
- `tournament_id`, `flight_number`, `name`, `status`, `notes`
- Heats reference flights via `Heat.flight_id`

---

## Step 4: Gap Analysis

### Day-of Operations Currently Supported

| Operation | Route/Method | Notes |
|-----------|-------------|-------|
| Move competitor between heats | `POST /scheduling/<tid>/event/<eid>/move-competitor` | Mirrors dual-run heats; warns on gear conflicts |
| Regenerate heats for one event | `POST /scheduling/<tid>/event/<eid>/generate-heats` | Destroys and rebuilds all heats; preserves EventResult data |
| Bulk regenerate college heats | `POST /scheduling/<tid>/generate-college-heats` | All non-completed college events |
| Rebuild all flights | `POST /scheduling/<tid>/flights/build` | Destroys and rebuilds all flights |
| Reorder heats within a flight | `POST /scheduling/<tid>/flights/<fid>/reorder` | Drag-and-drop via JSON |
| Sync HeatAssignment drift | `POST /scheduling/<tid>/event/<eid>/heats/sync-fix` | Repairs JSON ↔ table divergence |
| Mark flight started/completed | `POST /scheduling/<tid>/flights/<fid>/start` | Sends SMS to competitors in upcoming flights |

### Day-of Operations NOT Supported (Will Be Needed)

| Operation | Impact | Workaround |
|-----------|--------|------------|
| **Scratch competitor from a heat** | No way to remove a competitor from a heat without moving them to another heat. EventResult can be marked `scratched` but the competitor remains in the heat JSON and on printed heat sheets. | Regenerate the entire event's heats (destroys all heat assignments). |
| **Add late entry to a specific heat** | `Heat.add_competitor()` exists in the model but no route exposes it. A late-registered competitor only appears after full regeneration. | Regenerate event heats. |
| **Swap two competitors between heats** | The move route only supports one-directional moves. A true swap (A→Heat2, B→Heat1 atomically) requires two sequential moves. | Two separate move operations. |
| **Drop a competitor from the tournament mid-show** | Setting `competitor.status='scratched'` does not remove them from heats or flights. Their heat slot remains occupied. | Mark result as `scratched`, regenerate heats if needed. |
| **Re-assign marks after heat change** | Moving a competitor or regenerating heats does not trigger mark reassignment. A moved competitor keeps their old mark; a new competitor gets `handicap_factor=0.0`. | Manually re-run mark assignment for the event. |
| **Remove an empty heat** | If a heat becomes empty after moves, there is no route to delete it. It remains in the schedule as a 0-competitor heat. | Regenerate event heats. |
| **Insert a heat into an existing flight** | No route adds a single heat to a flight. Flight rebuild is all-or-nothing. | Rebuild all flights. |
| **Partially regenerate (one heat only)** | Regeneration is all-or-nothing per event. No way to rebuild just Heat 3 of 5. | Regenerate all heats for the event. |
| **Lock an event's heats from regeneration** | No guard prevents regenerating heats for an event that has already been scored or finalized. `Event.is_finalized` is checked in scoring but not in heat generation. | Discipline only. |

### Enforcement Gaps (Intent vs Implementation)

| Rule | Intent | Actual Enforcement |
|------|--------|--------------------|
| **Heat size maximum** | Competitors per heat ≤ max_stands | Enforced during generation. NOT enforced on manual moves — `move_competitor_between_heats` does not check if the destination heat is full. |
| **Gear conflict in destination** | Moving a competitor should warn about gear conflicts | Implemented as warning only (heats.py:246-276). Move is never blocked. |
| **Cookie Stack / Standing Block mutual exclusion** | Never schedule both in the same flight window | Enforced in flight builder scoring (-1 within 8 heats). NOT enforced during heat generation. |
| **Event.is_finalized guard on regeneration** | Finalized events should not have heats regenerated | NOT checked. `generate_heats` route does not check `event.is_finalized`. A judge could accidentally regenerate heats for a scored event. |
| **Heat.competitors ↔ HeatAssignment sync** | Always consistent | Sync is called after generation and moves, but any direct DB edit or crash between JSON write and sync_assignments() can cause drift. Manual sync-fix route exists. |
| **Competitor spacing in college overflow** | College overflow should respect spacing | Implemented in `integrate_college_spillover_into_flights` with MIN_HEAT_SPACING check and fallback. |
| **Sequential heat order** | Heat 1 before Heat 2 within each event | Enforced in flight builder's per-event queue. Validated by `build_flight_audit_report()`. |
| **Ability ranking in heat generation** | Better competitors distributed evenly | Enforced for pro events with ProEventRank rows. College events and unranked pro events use registration order. |
| **Left-hand springboard grouping** | All left-handed cutters in one heat | Enforced. Overflow to adjacent heats if needed. |
| **Slow-heat springboard grouping** | Slow cutters in dedicated heat | Enforced in generation AND in `optimize_flight_for_ability()` (post-flight regroup). |
| **Partner unit placement** | Partners stay together | Enforced via `_build_partner_units()`. Bidirectional name matching. Unpaired competitors treated as solo. |
| **List-only events skip heat generation** | OPEN college events tracked as signups only | Enforced. Returns 0 heats, deletes any existing heats. |

### Critical Day-of Risks

1. **Scratch without removal**: A scratched competitor appears on heat sheets as if they're competing. The judge must mentally skip them. No visual indicator on the heat sheet that a competitor is scratched.

2. **Regeneration destroys heat assignments but not results**: If heats are regenerated after scoring has begun, scored results become orphaned — they reference competitor IDs that no longer have heat assignments. The scoring UI may still work (it reads EventResult directly), but heat sheets will show different competitor orderings than what was scored.

3. **No regeneration guard on finalized events**: `generate_heats` route does not check `event.is_finalized`. Regenerating a finalized event's heats would destroy the heat-to-result mapping that was used for the official results.

4. **Move doesn't check destination capacity**: `move_competitor_between_heats` does not verify that the target heat has room. You can move a 5th competitor into a 4-stand heat.

5. **Dual storage creates drift risk**: The Heat.competitors JSON and HeatAssignment table can diverge. While sync mechanisms exist, a crash or partial transaction between JSON update and sync_assignments() call will leave inconsistent data.
