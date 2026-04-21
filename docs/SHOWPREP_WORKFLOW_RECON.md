# Show-Prep Workflow End-to-End — Recon Report

**Date:** 2026-04-21
**Scope:** Read-only workflow trace. No code changes.
**Goal:** Map every step from "empty tournament" to "printing heat sheets", document every state transition that affects heat run order or stand assignment, so that block-alternation for Single Buck / Double Buck / Jack & Jill can hook in at the right point(s) in the pipeline.

---

## Task 1 — Full show-prep workflow trace

Sequential steps visible from [templates/tournament_detail.html](../templates/tournament_detail.html) and [templates/_sidebar.html](../templates/_sidebar.html), following every link and button to its handler.

### Step 1 — Create tournament
- **UI:** Dashboard → "New Tournament"
- **Route:** `POST /tournament/new` → [routes/main.py](../routes/main.py) `tournament_new()`
- **Template:** [templates/tournament_new.html](../templates/tournament_new.html)
- **Writes:** `Tournament` row (`status='setup'`)
- **Reads:** nothing
- **Idempotent:** creates a new row each time. Destructive only if you create duplicates.
- **Undo:** delete tournament route exists

### Step 2 — Configure events (college + pro)
- **UI:** Tournament detail → "Set Up Events" (Before Show panel) OR sidebar → Setup Events / Tournament Setup
- **Routes:**
  - `GET/POST /tournament/<tid>/setup` → [routes/main.py](../routes/main.py) tournament_setup (consolidated page)
  - `GET/POST /scheduling/<tid>/events/setup` → [routes/scheduling/events.py](../routes/scheduling/events.py) `setup_events()`
- **Templates:** [templates/tournament_setup.html](../templates/tournament_setup.html), [templates/scheduling/setup_events.html](../templates/scheduling/setup_events.html)
- **Writes:** `Event` rows (name, event_type, stand_type, max_stands, scoring_type, is_partnered, is_gendered, is_handicap, is_open, is_finalized=False, payouts JSON)
- **Reads:** `config.COLLEGE_OPEN_EVENTS`, `COLLEGE_CLOSED_EVENTS`, `PRO_EVENTS`, `HANDICAP_ELIGIBLE_STAND_TYPES`
- **Idempotent:** yes — upserts via `_upsert_event()`
- **Undo:** event rows are edited or deleted via same page

### Step 3 — Register college teams/competitors
- **UI:** Tournament detail → "Import College Teams" OR sidebar → College Registration
- **Route:** `GET/POST /registration/<tid>/college/upload` → [routes/registration.py](../routes/registration.py) college upload handler
- **Template:** [templates/college/registration.html](../templates/college/registration.html)
- **Writes:** `Team` rows, `CollegeCompetitor` rows (with events_entered, partners, gear_sharing JSON), `EventResult` skeleton rows via downstream heat gen
- **Reads:** uploaded xlsx, existing `Event` rows for name resolution
- **Idempotent:** partial — reruns may create duplicate teams if not deduped
- **Undo:** per-row edits; bulk rebuild requires manual cleanup

### Step 4 — Register pro competitors
- **UI:** Tournament detail → "Add Pro Competitors" / "Import Entry Forms" / sidebar → Pro Registration
- **Routes:**
  - `GET/POST /registration/<tid>/pro/new` → [routes/registration.py](../routes/registration.py) `new_pro_competitor()` — triggers `strathmark_sync.enroll_pro_competitor()` after commit
  - `GET/POST /import/<tid>/pro-entry/*` → [routes/import_routes.py](../routes/import_routes.py) upload → review → confirm flow
- **Templates:** [templates/pro/new_competitor.html](../templates/pro/new_competitor.html), [templates/pro/import_upload.html](../templates/pro/import_upload.html), [templates/pro/import_review.html](../templates/pro/import_review.html)
- **Writes:** `ProCompetitor` rows (with events_entered, partners, gear_sharing, entry_fees, fees_paid, strathmark_id JSON/columns)
- **Reads:** existing `Event` rows
- **Idempotent:** import pipeline dedupes by timestamp + name
- **Undo:** per-competitor edit/delete

### Step 5 — Gear-sharing manager
- **UI:** Sidebar → Gear Sharing (pro only)
- **Routes:** `GET/POST /registration/<tid>/pro/gear-sharing*` → [routes/registration.py](../routes/registration.py) — gear manager, parse review, confirm, group ops, cleanup scratched
- **Template:** [templates/pro/gear_sharing.html](../templates/pro/gear_sharing.html)
- **Writes:** `ProCompetitor.gear_sharing` JSON dict `{event_id: partner_name | "group:NAME" | "category:KEY"}`
- **Reads:** ProCompetitor roster + events
- **Idempotent:** yes — edits overwrite
- **Undo:** manual edit

### Step 6 — Friday Night Feature config
- **UI:** Sidebar → Fri Feature / Sat Overflow
- **Route:** `GET/POST /scheduling/<tid>/friday-feature` → [routes/scheduling/friday_feature.py:77](../routes/scheduling/friday_feature.py#L77)
- **Template:** [templates/scheduling/friday_feature.html](../templates/scheduling/friday_feature.html)
- **Writes:** `Tournament.schedule_config['friday_pro_event_ids']`, `friday_feature_notes`, `saturday_college_event_ids`
- **Reads:** Events, current schedule_config
- **Idempotent:** yes
- **Undo:** clear selection + save

### Step 7 — Ability rankings (pro)
- **UI:** Sidebar → Ability Rankings
- **Route:** `GET/POST /scheduling/<tid>/ability-rankings` → [routes/scheduling/ability_rankings.py](../routes/scheduling/ability_rankings.py)
- **Template:** [templates/scheduling/ability_rankings.html](../templates/scheduling/ability_rankings.html) — SortableJS drag-drop ranking
- **Writes:** `ProEventRank` rows per (tournament, competitor, event_category)
- **Reads:** `ProCompetitor` list filtered by events_entered
- **Idempotent:** yes — upsert
- **Undo:** drag back / delete row

### Step 8 — Preflight check (optional)
- **UI:** Sidebar / Events page → "Preflight Check"
- **Route:** `GET /scheduling/<tid>/preflight` + JSON variant → [routes/scheduling/preflight.py](../routes/scheduling/preflight.py)
- **Template:** [templates/scheduling/preflight.html](../templates/scheduling/preflight.html)
- **Writes:** nothing (read-only)
- **Reads:** heats, assignments, partner pools, Saturday overflow
- **Idempotent:** trivially yes
- **Undo:** n/a

### Step 9 — Generate college heats
- **UI:** Unified Events & Schedule page → "Generate All College Heats" OR per-event "Generate Heats"
- **Routes:**
  - `POST /scheduling/<tid>/events` (action=`generate_all_college`) → [routes/scheduling/events.py](../routes/scheduling/events.py) `event_list()`
  - `POST /scheduling/<tid>/event/<eid>/generate-heats` → [routes/scheduling/heats.py](../routes/scheduling/heats.py) `generate_heats()`
- **Template:** [templates/scheduling/events.html](../templates/scheduling/events.html) (tournament page) / [templates/scheduling/heats.html](../templates/scheduling/heats.html) (per-event)
- **Writes:** `Heat` rows (competitors JSON, stand_assignments JSON), `HeatAssignment` rows via `heat.sync_assignments('college')`
- **Reads:** `CollegeCompetitor` roster, ability rankings (if any), gear-sharing conflict map
- **Idempotent:** effectively — deletes existing heats and regenerates. **Destructive to scoring if results exist.**
- **Undo:** no; guarded by `event.is_finalized` hard block + `has_scored` soft warning

### Step 10 — Generate pro heats
- Same handler as step 9, but for pro events. Calls `strathmark_sync` hooks on finalization (later). Uses `ProEventRank` for ability grouping when present.

### Step 11 — Build pro flights
- **UI:** Events & Schedule page → "Build Pro Flights" OR Pro Flights page → "Build Flights"
- **Routes:**
  - `POST /scheduling/<tid>/events` (action=`rebuild_flights`) → `event_list()`
  - `POST /scheduling/<tid>/flights/build` → [routes/scheduling/flights.py:57](../routes/scheduling/flights.py#L57) `build_flights()`
  - Async variant via `submit_job()`
- **Templates:** [templates/pro/flights.html](../templates/pro/flights.html), [templates/pro/build_flights.html](../templates/pro/build_flights.html)
- **Writes:** Deletes all existing `Flight` rows, nulls all `Heat.flight_id`/`flight_position`, creates new flights, sets `Heat.flight_id` + `Heat.flight_position`; rebuilds Partnered Axe show heats via `_prepare_partnered_axe_show_heats()` (these call `heat.sync_assignments('pro')`)
- **Reads:** pro heats (run_number==1), ProEventRank, gear-sharing conflict pairs
- **Idempotent:** yes — same input produces same output (see Task 4; optimizer is deterministic, not randomized). Re-running clobbers then rebuilds.
- **Undo:** no explicit undo; rerun with different parameters or re-order manually

### Step 12 — Integrate Saturday college spillover
- **UI:** Events & Schedule page → "Integrate Saturday Spillover" (after flights built)
- **Route:** `POST /scheduling/<tid>/events` (action=`integrate_spillover`) → `event_list()` → `flight_builder.integrate_college_spillover_into_flights()`
- **Writes:** `Heat.flight_id` for selected college events (esp. Chokerman Run 2 at end of last flight)
- **Reads:** `schedule_config['saturday_college_event_ids']`, existing flights
- **Idempotent:** re-runnable
- **Undo:** re-build flights

### Step 13 — Manual event order (Friday / Saturday fallback)
- **UI:** Events & Schedule page → drag-drop event cards (SortableJS — [templates/scheduling/events.html:1108](../templates/scheduling/events.html#L1108) loads `sortablejs@1.15.2`)
- **Routes:**
  - `POST /scheduling/<tid>/events/reorder-friday` → [routes/scheduling/events.py:473](../routes/scheduling/events.py#L473) `reorder_friday_events()` — writes `schedule_config['friday_event_order']`
  - `POST /scheduling/<tid>/events/reorder-saturday` → [routes/scheduling/events.py:492](../routes/scheduling/events.py#L492) `reorder_saturday_events()` — writes `schedule_config['saturday_event_order']`
  - `POST /scheduling/<tid>/events/reset-order` → [routes/scheduling/events.py:511](../routes/scheduling/events.py#L511) `reset_event_order()` — pops keys
- **Writes:** `Tournament.schedule_config` keys only
- **Reads:** existing config
- **Idempotent:** yes
- **Undo:** reset_event_order

### Step 14 — Manual flight-heat reorder (Saturday show order)
- **UI:** Heat Sheets page → drag-drop heat cards within a flight (SortableJS in [templates/scheduling/heat_sheets_print.html](../templates/scheduling/heat_sheets_print.html))
- **Route:** `POST /scheduling/<tid>/flights/<fid>/reorder` → [routes/scheduling/flights.py:130](../routes/scheduling/flights.py#L130) `reorder_flight_heats()`
- **Writes:** `Heat.flight_position` for each heat in specified order
- **Reads:** flight's current heats
- **Idempotent:** yes — validates heat_id set matches before applying
- **Undo:** manual re-drag

### Step 15 — Assign handicap marks (STRATHMARK, handicap events only)
- **UI:** Events list → per-event "Assign Marks" (appears only for handicap-eligible events)
- **Route:** `GET/POST /scheduling/<tid>/events/<eid>/assign-marks` → [routes/scheduling/assign_marks.py](../routes/scheduling/assign_marks.py)
- **Writes:** `EventResult.handicap_factor`, `EventResult.predicted_time`
- **Reads:** competitor history from STRATHMARK
- **Idempotent:** yes — can re-run
- **Undo:** re-run or edit EventResult

### Step 16 — Print heat sheets (Saturday flights)
- **UI:** Sidebar → Heat Sheets
- **Route:** `GET /scheduling/<tid>/heat-sheets` → [routes/scheduling/heat_sheets.py](../routes/scheduling/heat_sheets.py) `heat_sheets()`
- **Template:** [templates/scheduling/heat_sheets_print.html](../templates/scheduling/heat_sheets_print.html)
- **Writes:** none (read-only render)
- **Reads:** `Flight.get_heats_ordered()` ([models/heat.py](../models/heat.py) Flight class), `Heat.competitors`, `Heat.stand_assignments`, `EventResult.status`
- **Idempotent:** trivially yes

### Step 17 — Print day schedule
- **UI:** Sidebar → Day Schedule (Print)
- **Route:** `GET /scheduling/<tid>/day-schedule-print` → [routes/scheduling/heat_sheets.py](../routes/scheduling/heat_sheets.py) `day_schedule_print()` → calls `schedule_builder.build_day_schedule()`
- **Template:** [templates/scheduling/day_schedule_print.html](../templates/scheduling/day_schedule_print.html)
- **Writes:** none
- **Reads:** schedule_config, events, flights (for saturday_show block when flights exist)
- **Idempotent:** yes

### Step 18 — Print judge sheets / heat sheet PDF
- **UI:** Score entry page → "Print Judge Sheets" OR per-heat PDF
- **Routes:**
  - `GET /scoring/<tid>/heat/<hid>/pdf` → heat sheet PDF (WeasyPrint or print-HTML fallback)
  - Per-event judge sheet (inside scoring blueprint)
- **Writes:** none
- **Reads:** Heat + Event + EventResult

---

## Task 2 — Manual reordering feature status

**Status: IMPLEMENTED.**

**SortableJS is present.** CDN loaded at [templates/scheduling/events.html:1108](../templates/scheduling/events.html#L1108) (`sortablejs@1.15.2`). Also referenced in:
- [templates/scheduling/heat_sheets_print.html](../templates/scheduling/heat_sheets_print.html) (flight-heat drag-drop)
- [templates/scheduling/ability_rankings.html](../templates/scheduling/ability_rankings.html) (ability ranks)
- [templates/proam_relay/manual_teams.html](../templates/proam_relay/manual_teams.html) (relay teams)
- [templates/portal/user_guide.html](../templates/portal/user_guide.html) (reference)

### Routes and storage

| Scope | Route | Handler | Writes | Reader |
|---|---|---|---|---|
| Friday event order | `POST /scheduling/<tid>/events/reorder-friday` | [events.py:473](../routes/scheduling/events.py#L473) `reorder_friday_events()` | `schedule_config['friday_event_order']` (JSON list[int]) | `schedule_builder._build_friday_day_block(custom_order=...)` [schedule_builder.py:93](../services/schedule_builder.py#L93) |
| Saturday event order (fallback) | `POST /scheduling/<tid>/events/reorder-saturday` | [events.py:492](../routes/scheduling/events.py#L492) `reorder_saturday_events()` | `schedule_config['saturday_event_order']` (JSON list[int]) | `schedule_builder._build_saturday_from_event_order(custom_order=...)` [schedule_builder.py:213](../services/schedule_builder.py#L213) — used **only when no flights exist** |
| Flight heat order | `POST /scheduling/<tid>/flights/<fid>/reorder` | [flights.py:130](../routes/scheduling/flights.py#L130) `reorder_flight_heats()` | `Heat.flight_position` per heat | `Flight.get_heats_ordered()` [models/heat.py:198](../models/heat.py#L198) |
| Reset order | `POST /scheduling/<tid>/events/reset-order` | [events.py:511](../routes/scheduling/events.py#L511) `reset_event_order()` | removes `friday_event_order` / `saturday_event_order` keys | n/a |
| Ability ranks | `POST /scheduling/<tid>/ability-rankings/save` | ability_rankings.py | `ProEventRank` rows | heat_generator snake-draft |
| Manual relay teams | `POST /proam-relay/...` | proam_relay | ProAmRelay state | proam_relay service |

**Critical subtlety:** `schedule_config['saturday_event_order']` is **advisory only when flights are NOT built**. Once `build_pro_flights()` has created flights, `_build_saturday_from_flights()` at [schedule_builder.py:163](../services/schedule_builder.py#L163) is used instead — it iterates `Flight.query.order_by(flight_number).all()` and calls `flight.get_heats_ordered()`. The `saturday_event_order` key is silently ignored in that path. This is the key structural fact for block alternation: **once flights exist, `Flight.flight_number` + `Heat.flight_position` are authoritative for the pro show.**

---

## Task 3 — College heat/flight pipeline

**College does NOT use the flight builder.** There is no `build_college_flights()`.

College heat generation:
- Same `generate_event_heats()` [services/heat_generator.py:85](../services/heat_generator.py#L85) as pro, dispatched by `event.event_type=='college'`.
- Output: `Heat` rows with `heat_number` ascending, `run_number=1` (plus `run_number=2` for dual-run events Chokerman / Speed Climb).
- No `Flight` rows are created for college. `Heat.flight_id` stays NULL for college heats unless integrated via Saturday spillover.

Friday college day run order:
- Built by `schedule_builder._build_friday_day_block(events, custom_order=...)` at [schedule_builder.py:93](../services/schedule_builder.py#L93).
- If `schedule_config['friday_event_order']` present: `_apply_custom_order(events, custom_order)` applies it.
- Else: `sorted(events, key=_college_friday_sort_key)` — config-default order (OPEN events first, CLOSED in config order, Chokerman Run 1 near end, Birling last).
- Within each event: heats render in `Heat.heat_number` ascending order (hydration at [heat_sheets.py:104-106](../routes/scheduling/heat_sheets.py#L104-L106): `event.heats.order_by(Heat.heat_number, Heat.run_number)`).

Answer to (a) / (b) / (c):
- **Between events:** (c) — explicit `friday_event_order` list when present, otherwise (a) config-default sort. No algorithmic optimization. No tier interleaving.
- **Within an event:** (a) — `heat_number` ascending.

Authoritative source for "first heat on Friday":
- **Between events:** `schedule_config['friday_event_order'][0]` if set, else first event from `_college_friday_sort_key`.
- **Within that event:** `min(Heat.heat_number)` with `run_number=1`.

Saturday college overflow: Friday spillover events listed in `schedule_config['saturday_college_event_ids']` get their heats integrated into pro flights via `integrate_college_spillover_into_flights()`. Once integrated, their position is determined by `Heat.flight_id` + `Heat.flight_position` exactly like pro heats.

---

## Task 4 — Pro heat/flight pipeline

`build_pro_flights(tournament, num_flights=None)` — [services/flight_builder.py:59](../services/flight_builder.py#L59)

### When it runs
- Manual: `POST /scheduling/<tid>/flights/build` → [flights.py:57](../routes/scheduling/flights.py#L57) `build_flights()`
- Auto-triggered from `event_list()` POST with `action='rebuild_flights'` or `action='generate_all'` bulk path
- Async wrapper via `submit_job()` in preflight/async routes

### Idempotency
**Not idempotent in the "side-effect-free" sense — it is destructive and rebuilt-from-scratch**, but **deterministic** so the same input yields the same output:

1. Lines [79-87](../services/flight_builder.py#L79-L87): delete all existing `Flight` rows for the tournament, null out `Heat.flight_id` and `Heat.flight_position` on all affected heats.
2. Re-load all non-axe pro heats (`run_number==1` only), ordered by `(event_id, heat_number)`.
3. Pre-compute gear-sharing conflict pairs.
4. Call `_optimize_heat_order(all_heats, heats_per_flight, N_OPTIMIZATION_PASSES=5, gear_conflict_pairs)` at [line 145](../services/flight_builder.py#L145).
5. Create new `Flight` rows, assign heats with `flight_position=1,2,3,...`.
6. Inject Partnered Axe show heats (rebuilt via `_prepare_partnered_axe_show_heats()`, these call `heat.sync_assignments('pro')` around [line 268](../services/flight_builder.py#L268)).
7. Commit.

Re-running with unchanged heats produces the same `(flight_number, flight_position)` tuple for every heat.

### `_optimize_heat_order()` — [flight_builder.py:328](../services/flight_builder.py#L328)

**Inputs:**
- `all_heats`: list of `{'heat': Heat, 'event': Event, 'competitors': set[int]}`
- `heats_per_flight`: int, default 8 (or derived from `num_flights`)
- `n_passes`: `N_OPTIMIZATION_PASSES = 5`
- `gear_conflict_pairs`: `dict[int, set[int]]`

**Deterministic — NOT randomized.** [flight_builder.py:369-381](../services/flight_builder.py#L369-L381):
```python
actual_passes = min(n_passes, max(1, len(event_ids)))
for pass_num in range(actual_passes):
    rotated = event_ids[pass_num:] + event_ids[:pass_num]   # <— rotation, not shuffle
    candidate = _single_pass_optimize(event_queues, rotated, heats_per_flight,
                                      gear_conflict_pairs=gear_conflict_pairs)
    score = _score_ordering(candidate, heats_per_flight,
                            gear_conflict_pairs=gear_conflict_pairs)
    if score > best_score:
        best_score = score
        best_ordered = candidate
```
No `random.*` call in the file. Same input → same output every time. Tie-breaking uses "prefer event with most remaining heats" — deterministic.

### Authoritative source for "first heat on Saturday"
When flights exist:
- `Flight.query.filter_by(tournament_id=tid).order_by(Flight.flight_number).first()` → earliest flight
- Within that flight: `flight.get_heats_ordered()` [models/heat.py:198](../models/heat.py#L198), which returns heats sorted by `(flight_position IS NULL, flight_position, id)`. First heat of first flight = first heat of the show.

This is the pro show's only truth.

---

## Task 5 — State lock points

| Event | Immutable after this point? | Guard | File:line |
|---|---|---|---|
| Registration close | No hard lock. Tournament status transitions `setup → college_active → pro_active → completed` are by convention. | `Tournament.status` enum; routes don't block re-registration after status change | `models/tournament.py` |
| Heat generation complete | No — heats can be regenerated. | Hard guard: `event.is_finalized==True` blocks regen. Soft warning when scored results exist. | [routes/scheduling/heats.py](../routes/scheduling/heats.py) `generate_heats()` |
| Flight build complete | No — `build_pro_flights()` rebuilds destructively every call. | No lock. | [services/flight_builder.py:59](../services/flight_builder.py#L59) |
| "Lock schedule" / "finalize show-prep" | **Does not exist.** There is no tournament-wide schedule-lock action. Finalization is per-event (`Event.is_finalized`). | n/a | n/a |
| First heat scored | Per-heat edit lock via `Heat.acquire_lock(user_id)` + `locked_at` column; TTL = `HEAT_LOCK_TTL_SECONDS=300`. Prevents concurrent judges editing same heat. **Does not prevent heat regeneration**. | Soft lock, expiring | [models/heat.py:152](../models/heat.py#L152) |
| Event finalized (`Event.is_finalized=True`) | Blocks heat regeneration. Payout config saves trigger a recalculation of positions. | `Event.is_finalized` Boolean; checked in `routes/scheduling/heats.py` generate_heats | [models/event.py](../models/event.py) |
| Flight started | Soft indicator only. `Flight.status='in_progress'` set by `start_flight()` route; triggers SMS notifications; **does NOT block reorder or rebuild**. | Convention | [routes/scheduling/flights.py:155](../routes/scheduling/flights.py#L155) |

**Key implication for block alternation:** There is no hard "schedule-is-locked" gate. Any stand-assignment write that happens between heat-gen and show-time is legitimate, as long as it respects `event.is_finalized` and the transient `Heat.acquire_lock()` during score entry.

---

## Task 6 — Post-generation mutation hooks

| Operation | Route / Handler | Mutates | Calls `sync_assignments()`? |
|---|---|---|---|
| Regenerate heats (one event) | `POST /scheduling/<tid>/event/<eid>/generate-heats` → `generate_heats()` → `generate_event_heats()` | Deletes existing `Heat` rows for event, creates new ones with fresh `stand_assignments` JSON. | Yes — [heat_generator.py:226-227](../services/heat_generator.py#L226-L227) per created heat |
| Regenerate flights only (heats unchanged) | `POST /scheduling/<tid>/events` action=`rebuild_flights` and `POST /scheduling/<tid>/flights/build` | Clobbers `Flight` rows and `Heat.flight_id`/`Heat.flight_position`. Does **not** rewrite `Heat.stand_assignments` for existing heats. Does rewrite stand_assignments on rebuilt Partnered Axe heats. | Yes, for Partnered Axe rebuild (line ~268); non-axe heats: no, because their stand_assignments are unchanged |
| Move competitor between heats | `POST /scheduling/<tid>/event/<eid>/heats/swap` (or move) → `move_competitor_between_heats()` | Updates both heats' `competitors` JSON + `stand_assignments` | Yes — both heats synced |
| Scratch competitor | `POST /scheduling/<tid>/event/<eid>/heats/scratch` → `scratch_competitor()` | Removes competitor from `Heat.competitors`; deletes `HeatAssignment` row; sets `EventResult.status='scratched'` | Not explicit — scratch path modifies HeatAssignment directly |
| Manual stand reassignment within heat | Heat edit UI → calls `heat.set_stand_assignment(comp_id, stand)` directly in handler | `Heat.stand_assignments` JSON | Caller responsible; `sync_assignments` not universally called post-write |
| Sync check / sync fix | `GET /scheduling/<tid>/heats/sync-check` (JSON), `POST /scheduling/<tid>/heats/sync-fix` | Re-derives `HeatAssignment` rows from the JSON (authoritative) | Yes — that IS the sync operation |
| Integrate college spillover | `POST /scheduling/<tid>/events` action=`integrate_spillover` → `integrate_college_spillover_into_flights()` | Sets `Heat.flight_id` + `Heat.flight_position` on selected college heats | No — doesn't touch stand_assignments |
| Reorder flight heats | `POST /scheduling/<tid>/flights/<fid>/reorder` | `Heat.flight_position` only | No |
| Partner reassignment during score entry (`partner_resolver`) | scoring blueprint | May update partner linkage; does not mutate `stand_assignments` directly | n/a |

---

## Task 7 — Print export timing

| Export | Route | Reads | Before heats generated | Before flights built | Depends on Flight order? |
|---|---|---|---|---|---|
| Heat sheets | `GET /scheduling/<tid>/heat-sheets` → [heat_sheets.py](../routes/scheduling/heat_sheets.py) `heat_sheets()` | `Flight` rows, `flight.get_heats_ordered()`, `Heat.competitors`, `Heat.stand_assignments`, `_stand_label()` | Renders with empty flight list; no heat cards | Renders — heats show grouped by event, not flight, since flight_id is NULL | Yes for pro (flight-grouped view). College heat section renders heats in `heat_number` order regardless. |
| Judge sheets all | `GET /scoring/<tid>/judge-sheets` → scoring blueprint | Event.heats.order_by(heat_number), `Heat.competitors`, `Heat.stand_assignments`, `EventResult.status` | Flashes "No events with heats available"; redirects | Works fine — judge sheets use event-level heat_number order, not flight order | **No** — depends only on Heat data |
| Day schedule print | `GET /scheduling/<tid>/day-schedule-print` → `day_schedule_print()` → `build_day_schedule()` [schedule_builder.py:37](../services/schedule_builder.py#L37) | `schedule_config` (friday_pro_event_ids, saturday_college_event_ids, friday_event_order, saturday_event_order), Event rows, `Flight.get_heats_ordered()` when flights exist | Renders event names + slot numbers; heat detail empty | Saturday show block falls back to `_build_saturday_from_event_order()` (uses `saturday_event_order` custom_order or pro_sort_key); Friday day block unaffected | Yes for Saturday once flights exist (flights-first at [schedule_builder.py:155-157](../services/schedule_builder.py#L155-L157)); No for Friday |
| Video judge workbook | `POST /reporting/<tid>/export-video-judge` (sync) or `POST /reporting/<tid>/export-video-judge-async` (async) → `services/video_judge_export.py` | Event + Heat + EventResult; bracket events from `BirlingBracket` | Likely empty workbook or error | Works — reads heat data directly | **No** — heat data is enough |
| Birling blank bracket | Under birling routes (e.g. `GET /scheduling/<tid>/birling/print`) | `Event.payouts` bracket JSON | Renders blank bracket (bracket is pre-seeded from registration, not heats) | Works | **No** — bracket is its own structure |

Summary: only **Heat Sheets** and **Day Schedule (Saturday block)** depend on Flight ordering. Judge sheets, video judge workbook, and birling bracket are Flight-independent.

---

## Task 8 — Regenerate / rebuild / redo / reset semantics

| Name | Route / Handler | Rebuilds | Guards | Downstream staleness |
|---|---|---|---|---|
| `generate_heats` | `POST /scheduling/<tid>/event/<eid>/generate-heats` → `generate_heats()` | Full heat set for one event | Hard: `event.is_finalized==True` blocks. Soft: `has_scored` confirmation warning. | Orphans `EventResult` rows that still point to old competitor IDs; flight assignments lost for that event |
| `generate_all_college` | `POST /scheduling/<tid>/events` action=`generate_all_college` | All college event heats | Same as above per event | Same |
| `rebuild_flights` | `POST /scheduling/<tid>/events` action=`rebuild_flights` AND `POST /scheduling/<tid>/flights/build` | All `Flight` rows + all `Heat.flight_id` assignments | None | Saturday day schedule re-derives; any manual `reorder_flight_heats` changes lost |
| `integrate_spillover` | `POST /scheduling/<tid>/events` action=`integrate_spillover` | Sets `Heat.flight_id` for selected college events | None | Re-running overwrites previous integration |
| `reset_event_order` | `POST /scheduling/<tid>/events/reset-order` | Removes `friday_event_order` / `saturday_event_order` keys | None — idempotent | Day schedule reverts to default sort |
| `birling_reset` | Birling routes — resets bracket state | Clears bracket from `Event.payouts` JSON | Usually requires event not in progress | All bracket results lost |
| `sync-fix` | `POST /scheduling/<tid>/heats/sync-fix` | Rebuilds `HeatAssignment` rows from `Heat.competitors` + `Heat.stand_assignments` JSON (JSON is authoritative) | None — idempotent | None |
| Pro-Am Relay redraw / manual rebuild | `POST /proam-relay/...` | Relay state in `Event.payouts` JSON | Relay state-machine guards | Relay teams reshuffled |

---

## Task 9 — `Tournament.schedule_config` JSON schema

Model helpers: `Tournament.get_schedule_config()` / `Tournament.set_schedule_config(dict)` on [models/tournament.py](../models/tournament.py).

Observed keys (grep + schedule_builder + friday_feature confirm):

| Key | Type | Writer | Reader | Role |
|---|---|---|---|---|
| `friday_pro_event_ids` | `list[int]` (Event IDs) | [friday_feature.py:77](../routes/scheduling/friday_feature.py#L77) `friday_feature()` POST | `friday_feature()` GET; `build_day_schedule()` [schedule_builder.py:52](../services/schedule_builder.py#L52) | **Authoritative** — which pro events run in Friday Night Feature |
| `saturday_college_event_ids` | `list[int]` | `friday_feature()`, `event_list()` | `build_day_schedule()` [schedule_builder.py:60](../services/schedule_builder.py#L60); `integrate_college_spillover_into_flights()` | **Authoritative** — which college events spill to Saturday |
| `friday_event_order` | `list[int]` (Event IDs in order) | [events.py:473](../routes/scheduling/events.py#L473) `reorder_friday_events()` | `_build_friday_day_block(custom_order=...)` [schedule_builder.py:93](../services/schedule_builder.py#L93) | **Authoritative** when present; advisory when absent (default sort kicks in) |
| `saturday_event_order` | `list[int]` | [events.py:492](../routes/scheduling/events.py#L492) `reorder_saturday_events()` | `_build_saturday_from_event_order(custom_order=...)` [schedule_builder.py:213](../services/schedule_builder.py#L213) | Authoritative **only as fallback when no flights exist**. Ignored entirely when `Flight` rows are present (flights-first path at [schedule_builder.py:155-157](../services/schedule_builder.py#L155-L157)). |
| `friday_feature_notes` | `str` | `friday_feature()` POST | `friday_feature()` GET | Advisory — display metadata |

No other keys observed in code. `get_schedule_config()` returns `{}` default; setters write minimal diffs.

---

## Task 10 — Every stand_assignments mutation site

Grep for `set_stand_assignment`, `stand_assignments`, direct writes.

| File : line | Function | What it does |
|---|---|---|
| [models/heat.py:52-53](../models/heat.py#L52-L53) | `Heat.stand_assignments` column declaration | `db.Text, nullable=False, default='{}'` — JSON-encoded dict `{str(comp_id): int(stand_number)}` |
| [models/heat.py:103-108](../models/heat.py#L103-L108) | `Heat.get_stand_assignments()` | Read-only loader; returns `{}` on JSONDecodeError |
| [models/heat.py:110-114](../models/heat.py#L110-L114) | `Heat.set_stand_assignment(comp_id, stand_number)` | **Primary mutation API.** Loads JSON, updates `dict[str(comp_id)]=stand_number`, serializes back. |
| [models/heat.py:121-135](../models/heat.py#L121-L135) | `Heat.sync_assignments(competitor_type)` | Deletes all `HeatAssignment` rows for this heat, recreates from `competitors` JSON + `stand_assignments` JSON. Must be called after `db.session.flush()` so `heat.id` exists. |
| [services/heat_generator.py:181](../services/heat_generator.py#L181) | `generate_event_heats()` partnered branch | `heat.set_stand_assignment(comp['id'], stand_num)` for each competitor in each pair-unit |
| [services/heat_generator.py:186](../services/heat_generator.py#L186) | `generate_event_heats()` non-partnered branch | `heat.set_stand_assignment(comp['id'], stand_num)` per competitor |
| [services/heat_generator.py:210](../services/heat_generator.py#L210) | `generate_event_heats()` partnered dual-run branch | Assigns reversed stands for run 2 |
| [services/heat_generator.py:217](../services/heat_generator.py#L217) | `generate_event_heats()` non-partnered dual-run branch | Assigns reversed stands for run 2 |
| [services/heat_generator.py:227](../services/heat_generator.py#L227) | `generate_event_heats()` | Calls `heat.sync_assignments(comp_type)` for each created heat |
| [services/flight_builder.py](../services/flight_builder.py) around line 262 (per Explore agent finding) | `_prepare_partnered_axe_show_heats()` | Assigns stand=1 for all competitors in Partnered Axe finals heats; calls `heat.sync_assignments('pro')` at ~line 268 |
| [routes/scheduling/heats.py](../routes/scheduling/heats.py) `move_competitor_between_heats()` | Move handler | Updates `from_heat.competitors`, `to_heat.competitors`, both `stand_assignments`; calls `sync_assignments` on both heats |
| [routes/scheduling/heats.py](../routes/scheduling/heats.py) `scratch_competitor()` | Scratch handler | Removes competitor from `Heat.competitors`; deletes `HeatAssignment` row directly; does not call `sync_assignments` explicitly |
| Sync fix route (`/heats/sync-fix` POST) | Recovery | Calls `heat.sync_assignments(comp_type)` for each heat to rebuild `HeatAssignment` table |

**Surface area for block-alternation hook:** the two calls at [heat_generator.py:181](../services/heat_generator.py#L181) and [heat_generator.py:186](../services/heat_generator.py#L186) (initial heat gen) plus `_prepare_partnered_axe_show_heats()` (Partnered Axe only, irrelevant for saw events) plus the manual move path (`move_competitor_between_heats`). A post-flight rewrite step would need its own call site — none exists today.

---

## Sequential workflow — chronological summary (Task 1 condensed)

1. Create tournament (`status='setup'`).
2. Configure events (college + pro) on `/tournament/<tid>/setup`.
3. Import / register college teams + competitors.
4. Register pro competitors (manual + import pipeline).
5. Resolve gear-sharing via Gear Sharing Manager.
6. Select Friday Night Feature + Saturday college spillover events (writes `schedule_config` keys).
7. Rank pro competitors per event category (`ProEventRank`, SortableJS UI).
8. (Optional) Run Preflight checks.
9. Generate college heats (per-event or bulk). `Heat` + `HeatAssignment` rows written; `stand_assignments` JSON populated.
10. Generate pro heats (same).
11. Build pro flights — deterministic 5-pass greedy optimization. Creates `Flight` rows, sets `Heat.flight_id` + `Heat.flight_position`.
12. Integrate Saturday college spillover into flights.
13. (Optional) Drag-drop reorder of Friday events → `schedule_config['friday_event_order']`.
14. (Optional) Drag-drop reorder of flight heats → `Heat.flight_position`.
15. (Optional) Assign handicap marks per handicap-eligible event (STRATHMARK).
16. Print heat sheets (reads `Flight.get_heats_ordered()` + `stand_assignments`).
17. Print day schedule (reads `schedule_config` + flights).
18. Print judge sheets / per-heat PDFs (reads heat data only).
19. Run the show.

---

## Authoritative statements

> **The authoritative source for global heat run order on Friday is** `schedule_config['friday_event_order']` when present (else the `_college_friday_sort_key` default order over configured events), combined with `Heat.heat_number` (ascending, `run_number=1`) within each event. Dual-run college events show only run 1 on Friday; Chokerman Run 2 + Speed Climb Run 2 are forced to Saturday by [schedule_builder._add_mandatory_day_split_run2](../services/schedule_builder.py).

> **The authoritative source for global heat run order on Saturday is** `Flight.flight_number` ascending, then `Heat.flight_position` ascending within each flight (via `Flight.get_heats_ordered()` at [models/heat.py:198](../models/heat.py#L198)), when flights exist. When no flights exist (pre-build or rebuild-failed state), the fallback is `_build_saturday_from_event_order()` with `schedule_config['saturday_event_order']` custom order or `_pro_sort_key` default. The fallback is the only case where `saturday_event_order` is read.

---

## Mutation windows for stand assignments

Every point in the workflow where `Heat.stand_assignments` can legitimately change:

1. **Heat generation** — [heat_generator.py:181](../services/heat_generator.py#L181) and [heat_generator.py:186](../services/heat_generator.py#L186). Run 1 assignment.
2. **Heat generation, dual-run events** — [heat_generator.py:210](../services/heat_generator.py#L210) and [heat_generator.py:217](../services/heat_generator.py#L217). Run 2 gets reversed stands (stands reversed relative to heat composition, not re-assigned per heat index). Saw events are single-run, so this path is inert for hand-saw.
3. **Heat regeneration (same route as #1)** — same mutation, clobbers previous.
4. **Move competitor between heats** — `move_competitor_between_heats()` updates stand_assignments for both source and destination heat.
5. **Scratch competitor** — removes entry from stand_assignments JSON (via HeatAssignment delete; JSON may need explicit cleanup — confirm on implementation).
6. **Partnered Axe show heat rebuild** — during `build_pro_flights()` → `_prepare_partnered_axe_show_heats()`. Assigns all finalists to stand 1 of their show heat.
7. **Sync fix route** — does NOT change `stand_assignments` JSON; it rebuilds the `HeatAssignment` cache from the JSON. But the JSON is authoritative and unchanged.
8. **Manual stand reassignment via heat editor** — direct `heat.set_stand_assignment()` call in edit handler (if such a route is surfaced; exists in handler layer but no dedicated UI found in this recon).

**Windows where no stand_assignment mutation currently happens but could be added:**
- After `build_pro_flights()` completes (global run-order is now known).
- After `reorder_flight_heats()` (manual reorder has set final run order).
- Inside `_optimize_heat_order()` as a post-step once heat placement is chosen.
- At heat-sheet render time (compute block labels on the fly without persisting).

For a hand-saw block-alternation design, windows 2–4 above are the candidate hook points, depending on whether alternation is computed eagerly (heat-gen time, risks being broken by later flight reshuffle) or lazily (after `reorder_flight_heats` or at render time, more accurate but touches more of the pipeline).

---

## Corrections vs. the earlier recon drafts

During this recon the following initial findings from the sub-agent were verified and partially corrected:

- **SortableJS is present in multiple templates**, not absent. Confirmed at [templates/scheduling/events.html:1108](../templates/scheduling/events.html#L1108).
- **`_optimize_heat_order()` is deterministic**, not randomized. Uses rotation of event-id order across 5 passes; best score wins. No `random.*` call in the file.
- **`saturday_event_order` is only consulted when no flights exist.** Once `Flight` rows are present, `_build_saturday_from_flights()` takes precedence and the custom order is silently ignored.
- **There is no tournament-wide "lock schedule" action.** Finalization is per-event (`Event.is_finalized`). The only other lock is the 300-second `Heat.acquire_lock()` during concurrent score entry.
