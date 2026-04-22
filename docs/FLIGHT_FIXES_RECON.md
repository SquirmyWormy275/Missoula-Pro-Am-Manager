# Flight Fixes Recon

Reconnaissance for five flight-generation / heat-composition issues. No fixes proposed — this document maps the relevant code paths so a follow-up implementation prompt can be written.

- **Recon date:** 2026-04-22
- **Repo:** Missoula-Pro-Am-Manager
- **Source-of-truth spec:** [FlightLogic.md](../FlightLogic.md)
- **Live DB inspected:** `instance/proam.db`
  - Tournament 1 — "Missoula Pro Am (WOOD TEST) 2026": 8 flights × 8–9 heats
  - Tournament 2 — "Missoula Pro Am (flights test) 2026": 3 flights × 17–18 heats (matches user's Initial_Flights.pdf report)

---

## Inventory

Project uses a flat layout (`routes/`, `services/`, `models/`) — not `app/`. The PowerShell inventory commands in the task description target `app/**`, which does not exist in this repo. Substituted results below.

### Key file sizes (lines)

```
services/flight_builder.py        1258
services/heat_generator.py         909
services/schedule_builder.py       487
services/scoring_engine.py         929  (approx, from byte-based ranking)
services/gear_sharing.py         ~1500
services/woodboss.py              ~900
routes/scoring.py                ~1000
routes/registration.py            ~900
routes/scheduling/heats.py         928
routes/scheduling/events.py        720
routes/scheduling/flights.py       566
routes/scheduling/friday_feature.py 305
config.py                          562
models/event.py                    319
models/competitor.py               396
models/tournament.py               268
models/heat.py                     220
```

### Largest Python files across routes/ + services/ + models/ (top 15 by bytes)

```
83,483 services/gear_sharing.py
78,112 routes/scoring.py
70,778 routes/registration.py
65,089 services/woodboss.py
54,111 services/flight_builder.py
50,600 routes/portal.py
47,309 services/excel_io.py
41,472 routes/scheduling/heats.py
41,253 services/scoring_engine.py
40,310 services/registration_import.py
39,956 routes/main.py
36,880 services/heat_generator.py
35,156 services/birling_bracket.py
35,098 routes/reporting.py
34,240 routes/scheduling/events.py
```

### Scheduling sub-package

`routes/scheduling/` is a package of sub-modules sharing one `scheduling_bp` blueprint:

```
__init__.py (284 lines — shared helpers, blueprint, late imports)
ability_rankings.py  assign_marks.py  birling.py  events.py  flights.py
friday_feature.py   heat_sheets.py   heats.py    partners.py  preflight.py
print_hub.py        pro_checkout_roster.py       show_day.py
```

---

## Issue 1: College Speed Climb Run 2 routing

**User report:** College Speed Climb Men and Women Run 2 heats are not appearing in any Day-2 (Saturday) flight.

**Constants:**
- [config.py:235](../config.py#L235): `DAY_SPLIT_EVENT_NAMES = {"Chokerman's Race", "Speed Climb"}` — the two events whose Run 2 must be on Saturday.
- [config.py:404](../config.py#L404): Speed Climb is a college event (`requires_dual_runs=True`, `stand_type='speed_climb'`, `is_gendered=True`).
- [config.py:406](../config.py#L406): Chokerman's Race is a college event (`requires_dual_runs=True`, `stand_type='chokerman'`, `is_gendered=True`).

### Working precedent — Chokerman's Race Run 2

File: [services/flight_builder.py:1122-1177](../services/flight_builder.py#L1122-L1177) (`integrate_college_spillover_into_flights`):

```python
def integrate_college_spillover_into_flights(tournament, college_event_ids=None):
    selected_ids = set(int(v) for v in (college_event_ids or []))
    mandatory = tournament.events.filter_by(event_type='college',
                                            name="Chokerman's Race").first()
    if mandatory:
        selected_ids.add(mandatory.id)               # Chokerman is ALWAYS in the set
    ...
    last_flight = flights[-1]
    ...
    for event in sorted(events, key=lambda e: (e.name, e.gender or '')):
        if event.name == "Chokerman's Race":
            # Run 2 only on Saturday. All heats group together at the end of
            # the last flight in the same heat-number order as Run 1.
            heats = event.heats.filter_by(run_number=2).order_by(Heat.heat_number).all()
        else:
            heats = event.heats.order_by(Heat.run_number, Heat.heat_number).all()
        ...
        for heat in heats:
            if heat.flight_id is not None:
                continue
            if event.name == "Chokerman's Race":
                heat.flight_id = last_flight.id
                heat.flight_position = _next_flight_position(last_flight.id)
```

**Summary of Chokerman routing:**
1. Auto-added to `selected_ids` (no UI selection required).
2. Only `run_number == 2` heats are queried.
3. All Run 2 heats are placed at the end of the last flight, in `heat_number` order.

### Speed Climb current handling

Two places treat Speed Climb as a "mandatory Saturday" event but **neither actually places its heats into a Flight**:

**(a) Schedule display (`services/schedule_builder.py:355-372`):**

```python
def _add_mandatory_day_split_run2(schedule_entries, college_events):
    existing_event_ids = {entry.get('event_id') for entry in schedule_entries}
    updated = list(schedule_entries)
    for event in college_events:
        if event.name not in DAY_SPLIT_EVENT_NAMES or event.event_type != 'college':
            continue
        if event.id in existing_event_ids:
            continue
        updated.append({
            'slot': len(updated) + 1,
            'event_id': event.id,
            'label': f"{event.display_name} (Run 2)",
            ...
            'is_run2': True,
        })
    return updated
```

**(b) Saturday ordered-heats read (`services/schedule_builder.py:469-486`):**

```python
# Include dual-run Run 2 heats for day-split college events
# (Chokerman's Race, Speed Climb) — their Run 2 is always Saturday.
college_events = tournament.events.filter_by(event_type='college').all()
for event in college_events:
    if event.name not in DAY_SPLIT_EVENT_NAMES:
        continue
    run2_heats = Heat.query.filter_by(event_id=event.id, run_number=2).order_by(Heat.heat_number).all()
    for h in run2_heats:
        if h.id not in seen_ids:
            heats.append(h)
            seen_ids.add(h.id)
```

**Critical gap:** `integrate_college_spillover_into_flights` (the function that actually assigns `Heat.flight_id`) only auto-adds Chokerman to the mandatory set. If Speed Climb is not selected explicitly via `saturday_college_event_ids`, its Run 2 heats stay with `flight_id=NULL` and never appear in any flight sheet. If it *is* selected, line 1165 pulls **both Run 1 and Run 2** heats (no `filter_by(run_number=2)` in the non-Chokerman branch), which would double-place Run 1 onto Saturday.

**Event identity:** College Speed Climb is a **separate Event row** from Pro "Pole Climb" — both use `stand_type='speed_climb'` but:
- College: [config.py:404](../config.py#L404) `name='Speed Climb'`, `event_type='college'`.
- Pro: [config.py:427](../config.py#L427) `name='Pole Climb'`, `event_type='pro'`.

The alias set in [routes/scheduling/__init__.py:113](../routes/scheduling/__init__.py#L113) maps `poleclimb` ↔ `speedclimb` only for **pro** events_entered parsing, so there is no name collision at the event-lookup layer.

**Live DB evidence (tournament 2):**

```
Speed Climb heat breakdown  (run_number, flight_id, count)
(1, None, 4)     # Run 1: 4 heats, all orphaned from flights
(2, None, 4)     # Run 2: 4 heats, all orphaned from flights

Chokerman heat breakdown
(1, None, 10)    # Run 1: orphaned (expected — Friday only)
(2, None, 10)    # Run 2: orphaned (UNEXPECTED — should be in last flight)
```

This means Tournament 2's flights were likely built via the **async job path** (`routes/scheduling/flights.py:181-198`), which calls `build_pro_flights(...)` in the background but **does not chain** `integrate_college_spillover_into_flights`. See Issue 2.

### Implementation hook point

The single authoritative place to mirror Chokerman's Run-2-only routing for Speed Climb is [services/flight_builder.py:1159-1177](../services/flight_builder.py#L1159-L1177). A generalisation would:
1. Use `DAY_SPLIT_EVENT_NAMES` (config.py:235) as the mandatory set instead of hard-coding `"Chokerman's Race"`.
2. Apply `filter_by(run_number=2)` to every name in `DAY_SPLIT_EVENT_NAMES`, not just Chokerman.
3. Decide where Speed Climb Run 2 lands (end of last flight? round-robin across flights? judge-selectable position?).

FlightLogic.md §4.1 documents Chokerman Run 2 placement at end of last flight; Speed Climb Run 2 placement is **not yet specified** in the source-of-truth doc.

---

## Issue 2: Saturday spillover bug

**User report:** Events selected for Saturday spillover in the UI do not appear in Saturday flight structure even when explicitly selected.

### Full end-to-end path

**Step 1 — UI checkbox (friday_feature page):**

File: [templates/scheduling/friday_feature.html:113-118](../templates/scheduling/friday_feature.html#L113-L118)

```html
<input class="form-check-input" type="checkbox"
       name="saturday_college_event_ids" value="{{ event.id }}"
       {% if event.id in selected_saturday_ids %}checked{% endif %}>
```

Also exposed on [templates/scheduling/day_schedule.html:165-167](../templates/scheduling/day_schedule.html#L165-L167) (same field name).

**Step 2 — POST handler:**

File: [routes/scheduling/friday_feature.py:102-125](../routes/scheduling/friday_feature.py#L102-L125) (`friday_feature` view):

```python
if request.method == 'POST':
    ...
    try:
        saturday_college_event_ids = [
            int(eid) for eid in request.form.getlist('saturday_college_event_ids')
            if str(eid).strip()
        ]
    except (TypeError, ValueError):
        saturday_college_event_ids = []

    # Merge into DB config (preserves friday_event_order / saturday_event_order)
    db_cfg = tournament.get_schedule_config()
    db_cfg['saturday_college_event_ids'] = saturday_college_event_ids
    saved_opts = dict(saved_opts)
    saved_opts['saturday_college_event_ids'] = saturday_college_event_ids
    session[session_key] = saved_opts
    session.modified = True
    tournament.set_schedule_config(db_cfg)
    db.session.commit()
```

**Step 3 — Persistence:**

File: [models/tournament.py:35,48-59](../models/tournament.py#L35-L59)

```python
schedule_config = db.Column(db.Text, nullable=True)

def get_schedule_config(self):
    return _json.loads(self.schedule_config or '{}')

def set_schedule_config(self, data):
    self.schedule_config = _json.dumps(data)
```

Stored as a JSON string in a `TEXT` column. Access helpers on the `Tournament` model.

**Step 4 — Read paths in flight generator:**

Four separate entry points chain `integrate_college_spillover_into_flights` after building flights:

| Caller | File | Lines |
|---|---|---|
| `event_list` POST (`generate_all`, `rebuild_flights`, `integrate_spillover`) | [routes/scheduling/events.py:41-108](../routes/scheduling/events.py#L41-L108) | reads saved `saturday_college_event_ids` at line 128 and dispatches |
| `build_flights` synchronous POST | [routes/scheduling/flights.py:210-224](../routes/scheduling/flights.py#L210-L224) | chained after `build_pro_flights` |
| `one_click_generate` | [routes/scheduling/flights.py:34-44](../routes/scheduling/flights.py#L34-L44) | single transaction |
| `preflight` apply-fixes | [routes/scheduling/preflight.py:25-65](../routes/scheduling/preflight.py#L25-L65) | via `apply_preflight_fixes` |

**Step 5 — The spillover function:**

File: [services/flight_builder.py:1122-1218](../services/flight_builder.py#L1122-L1218) (`integrate_college_spillover_into_flights`), shown in Issue 1 above.

Key behavioural detail at line 1165:

```python
else:
    heats = event.heats.order_by(Heat.run_number, Heat.heat_number).all()
```

For dual-run events other than Chokerman, this pulls **all run_number values**, so a user selecting Speed Climb would drag Run 1 heats onto Saturday too.

### Identified break points

**Break point (A) — Async flight build does NOT chain spillover integration.**

[routes/scheduling/flights.py:181-198](../routes/scheduling/flights.py#L181-L198):

```python
if request.form.get('run_async') == '1':
    def _build_flights_async(target_tournament_id, requested_num_flights):
        target = Tournament.query.get(target_tournament_id)
        if not target:
            raise RuntimeError(f'Tournament {target_tournament_id} not found.')
        return build_pro_flights(target, num_flights=requested_num_flights)

    job_id = submit_job(
        'build_pro_flights',
        _build_flights_async,
        tournament_id,
        num_flights,
        metadata={'tournament_id': tournament_id, 'kind': 'build_pro_flights'},
    )
    ...
    flash('Flight build started in the background.', 'success')
    return redirect(url_for('reporting.export_results_job_status', ...))
```

The sync path (lines 200-224) chains `integrate_college_spillover_into_flights`; the async path does not. Because `build_pro_flights` *wipes every `Heat.flight_id`* (flight_builder.py:104-108), any previously-integrated spillover is orphaned and never re-integrated.

**Live DB evidence** matches this: Tournament 2's `schedule_config` has `saturday_college_event_ids: [58, 59, 64, 69, 70]` (Standing Block Speed M/F, Jack & Jill, Obstacle Pole M/F) but **all five events' heats, plus Chokerman Run 2, have `flight_id=NULL`**. The 3 built flights only contain pro heats.

**Break point (B) — Non-Chokerman branch pulls all runs for dual-run spillover events.**

[services/flight_builder.py:1165](../services/flight_builder.py#L1165). If a user ever selected Speed Climb as spillover, Run 1 heats (which are supposed to stay on Friday) would be pulled into Saturday flights alongside Run 2.

**Break point (C) — Silent success when `selected_ids` is empty.**

[services/flight_builder.py:1139-1141](../services/flight_builder.py#L1139-L1141):

```python
events = tournament.events.filter(Event.id.in_(selected_ids)).all() if selected_ids else []
if not events:
    return {'integrated_heats': 0, 'events': 0, 'message': 'No selected spillover events.'}
```

When the UI checkboxes are unchecked but the user still expects Chokerman Run 2 to land, `selected_ids = {chokerman_id}` (from the mandatory branch) and the path works. But if the `Chokerman's Race` row does not exist in the tournament (edge case — event not configured), `selected_ids` stays empty and the function exits with `integrated_heats=0`. The caller only flashes "Integrated 0..." when count > 0, so nothing surfaces.

### Summary of break points (ranked by live-data impact)

1. **(A) async build path** — most likely cause of the current Tournament-2 state (Chokerman Run 2 + all 5 selected spillover events orphaned).
2. **(B) dual-run spillover event** — latent; would bite the moment Speed Climb is selected as spillover (see Issue 1 for why this may become necessary).
3. **(C) silent no-op** — minor, edge case.

---

## Issue 3: Pro-Am Relay missing from flight sheet

**User report:** Pro-Am Relay does not appear in any flight. Required behavior: always in the final flight of the show.

### Does Pro-Am Relay exist as an Event?

**In `config.PRO_EVENTS`:** No. [config.py:412-429](../config.py#L412-L429) lists 14 pro events; Pro-Am Relay is not among them. An inline comment on [config.py:213](../config.py#L213) notes "Excludes the Pro-Am Relay, which uses a single team time entry."

**Created on demand:** [services/proam_relay.py:593-612](../services/proam_relay.py#L593-L612):

```python
def create_proam_relay_event(tournament: Tournament) -> Event:
    """Create the Pro-Am Relay event for a tournament."""
    relay_event = Event.query.filter_by(
        tournament_id=tournament.id, name='Pro-Am Relay'
    ).first()

    if not relay_event:
        relay_event = Event(
            tournament_id=tournament.id,
            name='Pro-Am Relay',
            event_type='pro',
            scoring_type='time',
            is_partnered=True,
            status='pending'
        )
        db.session.add(relay_event)
        db.session.commit()
    return relay_event
```

Also created opportunistically inside `ProAmRelay._save_relay_data()` at [services/proam_relay.py:72-86](../services/proam_relay.py#L72-L86). There is **no `stand_type`** set on this event.

Live DB confirms: Tournament 2 has an event row `(id=87, name='Pro-Am Relay', event_type='pro')`.

### Why it is excluded from flights

**Zero Heat rows.** [services/heat_generator.py:85-116](../services/heat_generator.py#L85-L116) (`generate_event_heats`) would:
1. Call `_get_event_competitors(event)` (heat_generator.py:305). That function filters `ProCompetitor` by `_competitor_entered_event(event, comp.get_events_entered())` — `events_entered` stores event **names** as strings (e.g. `"Springboard"`, `"Stock Saw"`), and no ProCompetitor lists `"Pro-Am Relay"` in `events_entered` because the relay is managed by the lottery (`event_state` JSON), not the entry-form flow.
2. So `competitors == []`, and the function raises `ValueError("No competitors entered for ...")` — caught by the per-event savepoint in `_generate_all_heats` ([routes/scheduling/__init__.py:215-243](../routes/scheduling/__init__.py#L215-L243)) and counted as "skipped".

No Heat rows → not pulled by `build_pro_flights`'s batch query ([services/flight_builder.py:141-146](../services/flight_builder.py#L141-L146)):

```python
batched_heats = (
    Heat.query
    .filter(Heat.event_id.in_(non_axe_event_ids), Heat.run_number == 1)
    .order_by(Heat.event_id, Heat.heat_number)
    .all()
) if non_axe_event_ids else []
```

Live DB (Tournament 2): `SELECT COUNT(*) FROM heats h JOIN events e ON e.id=h.event_id WHERE e.name='Pro-Am Relay' AND e.tournament_id=2;` → **0**.

### State lives in JSON, not Heat rows

[services/proam_relay.py:32-61](../services/proam_relay.py#L32-L61) — `ProAmRelay` loads relay state from `Event.event_state` (fallback `Event.payouts`):

```python
return {
    'status': 'not_drawn',
    'teams': [],
    'eligible_college': [],
    'eligible_pro': [],
    'drawn_college': [],
    'drawn_pro': []
}
```

[services/proam_relay.py:21-26](../services/proam_relay.py#L21-L26) — the relay's four sub-events (`partnered_sawing`, `standing_butcher_block`, `underhand_butcher_block`, `team_axe_throw`) are a Python tuple, not Event rows. No physical stand_type mapping.

### Flight ordering / "final flight" reference

[services/flight_builder.py:1135,1143](../services/flight_builder.py#L1135-L1143) — `Flight` rows are ordered by `flight_number`, and "last flight" is accessed by `flights[-1]`:

```python
flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).all()
...
last_flight = flights[-1]
```

This is the established pattern to deterministically target the final flight.

### FlightLogic.md coverage

Searched the spec: **no mention of Pro-Am Relay** in [FlightLogic.md](../FlightLogic.md). FlightLogic §3.5 covers Partnered Axe Throw's "one per flight" rule but Pro-Am Relay has no placement rule documented.

### Hook point for "final flight" placement

[services/flight_builder.py:1159-1219](../services/flight_builder.py#L1159-L1219) — the `integrate_college_spillover_into_flights` function is the closest analog of "append at the end of the last flight" logic. A new routine (or extension) would need to:
- Ensure the Pro-Am Relay event exists (call `create_proam_relay_event(tournament)` if not).
- Decide what a "relay heat" even is (no Heat row exists today — either synthesise one Heat row placeholder, or teach the flight/heat-sheet render layer to treat Pro-Am Relay as a pseudo-heat read from `event_state`).
- Append a reference (heat row or pseudo-slot) to `flights[-1]` with `flight_position = _next_flight_position(last_flight.id)`.

Open design questions (to resolve before implementation):
- Does Pro-Am Relay get ONE pseudo-heat (the whole relay as a block) or FOUR pseudo-heats (one per sub-event in `RELAY_EVENTS`)?
- If synthesising Heat rows, what goes in `Heat.competitors` — the two team captains? Nothing (empty)?
- Scoring flow: Pro-Am Relay doesn't go through `EventResult` the usual way — where does the heat sheet render the team composition?

---

## Issue 4: Left-handed Springboard grouping

**User report:** All LH Springboard cutters must be placed on the same tree (same heat or sequential heats on the same physical setup).

### Handedness field

File: [models/competitor.py:241](../models/competitor.py#L241)

```python
is_left_handed_springboard = db.Column(db.Boolean, nullable=False, default=False)
```

Field only exists on `ProCompetitor` — no equivalent on `CollegeCompetitor`. Collected via pro entry form / Excel import (`services/pro_entry_importer.py`, `scripts/repair_springboard_handedness.py`).

### Springboard heat composer

File: [services/heat_generator.py:609-680](../services/heat_generator.py#L609-L680) (`_generate_springboard_heats`)

Current rule (direct quote from the docstring):

```python
def _generate_springboard_heats(competitors, num_heats, max_per_heat, stand_config, event=None,
                                gear_violations=None, lh_warnings=None):
    """
    Generate springboard heats with left-handed cutter spreading.

    Only one physical left-handed springboard dummy exists on site, so at most
    ONE left-handed cutter can be in a single heat at a time.  Spread LH cutters
    one per heat across heats 0..N-1.  If more LH cutters than heats exist,
    overflow into the FINAL heat (per user rule, 2026-04-20) and log a warning
    via lh_warnings so the admin knows there is dummy contention.
    """
    heats = [[] for _ in range(num_heats)]
    left_handed = [c for c in competitors if c.get('is_left_handed', False)]
    slow_heat  = [c for c in competitors if c.get('is_slow_springboard', False)]
    ...
    # --- LH spread ---
    # One LH cutter per heat, heats 0..N-1.  Overflow spills into the final
    # heat (heats[num_heats-1]), mixed with RH cutters there.
    if left_handed and num_heats > 0:
        spread = left_handed[:num_heats]
        overflow = left_handed[num_heats:]
        for i, lh in enumerate(spread):
            if len(heats[i]) < max_per_heat:
                heats[i].append(lh)
                assigned_ids.add(lh['id'])
        ...
```

**Current behavior — direct conflict with user's new requirement:**
- The current rule **spreads** LH cutters one per heat (to time-multiplex the single LH dummy across heats).
- The flight builder further amplifies the spread by penalising flights with >1 LH-containing heat ([services/flight_builder.py:154-172, 260-270](../services/flight_builder.py#L154-L270)):

  ```python
  lh_comp_ids: set[int] = set()
  for heat in batched_heats:
      event = event_by_id.get(heat.event_id)
      if event and getattr(event, 'stand_type', None) == 'springboard':
          lh_comp_ids.update(heat.get_competitors())
  ...
  is_springboard = getattr(event, 'stand_type', None) == 'springboard'
  contains_lh = is_springboard and any(lh_flags.get(cid, False) for cid in comps)
  ```

### Tree / dummy data model

Springboard stand config, [config.py:310-315](../config.py#L310-L315):

```python
'springboard': {
    'total': 4,
    'uses_per_event': 3,
    'supports_handedness': True,
    'labels': ['Dummy 1', 'Dummy 2', 'Dummy 3', 'Dummy 4']
},
```

- Physical model: **4 dummies** per springboard event.
- `uses_per_event: 3` is the cut count per dummy.
- `supports_handedness: True` is a flag — there is **no explicit mapping from a specific dummy number to "the LH dummy"** anywhere in `services/heat_generator.py`, `services/flight_builder.py`, or `config.py`. The "LH dummy" is a domain concept (one of the four is set up left-handed on site) but the code does not label which stand number is the LH one.

Stand assignment within a springboard heat is by list index order ([services/heat_generator.py:181-206](../services/heat_generator.py#L181-L206)):

```python
stand_numbers = _stand_numbers_for_event(event, max_per_heat, stand_config)
...
for i, comp in enumerate(heat_competitors):
    stand_num = stand_numbers[i] if i < len(stand_numbers) else i + 1
    heat.set_stand_assignment(comp['id'], stand_num)
```

`_stand_numbers_for_event` ([services/heat_generator.py:825-834](../services/heat_generator.py#L825-L834)) returns `list(range(1, max_per_heat + 1))` by default (no LH-specific slot), unless the event has `specific_stands` in `STAND_CONFIGS` (springboard does not).

### Current grouping logic

| Layer | Behaviour | Reference |
|---|---|---|
| Heat gen | LH cutters spread one per heat, overflow to final heat | [services/heat_generator.py:641-678](../services/heat_generator.py#L641-L678) |
| Heat gen warning | `lh_warnings` + `gear_violations` surfaced via `get_last_lh_overflow_warnings(event.id)` | [services/heat_generator.py:36-45](../services/heat_generator.py#L36-L45) |
| Flight builder | Tracks `contains_lh` per heat, penalises flights with >1 LH heat, logs "LH DUMMY CONTENTION" warning | [services/flight_builder.py:260-270](../services/flight_builder.py#L260-L270) |
| FlightLogic.md §5.3 | Documents old rule: "All left-handed cutters are grouped into a dedicated heat whenever capacity allows." Note: the spec describes *grouping*, the code does *spreading* — the spec is stale. | [FlightLogic.md:294-301](../FlightLogic.md#L294-L301) |

**Important contradiction:** FlightLogic.md §5.3 (the source-of-truth) says "grouped into a dedicated heat" — matches the user's new requirement. The code does the opposite (spreads). This is either a recent code regression from the spec, or the spec was never updated when the rule flipped. The 2026-04-20 docstring and flight_builder's "LH DUMMY CONTENTION" warning both strongly imply the current intent *is* to spread, reasoning that one physical LH dummy means one LH cutter per heat time-slot.

### Implementation hook point

Two candidate hook points depending on "same tree" interpretation:

- **All LH in one heat (same heat, same dummy):** rewrite the `--- LH spread ---` block in [services/heat_generator.py:636-678](../services/heat_generator.py#L636-L678) to group up to `max_per_heat=4` LH cutters in a single dedicated heat.
- **Consecutive heats on LH dummy (same physical setup across N heats):** two sub-changes —
  1. Heat composer: place LH cutters into a contiguous run of heats (e.g. heats `[0]..[k-1]`) with one LH per heat.
  2. Flight builder: invert the LH penalty in [services/flight_builder.py:260-270](../services/flight_builder.py#L260-L270) so LH-containing heats *cluster* in one flight instead of spreading across flights.

Data-model gap: no field identifies "which of the 4 dummies is the LH one" — either add `specific_stands_lh` to `STAND_CONFIGS` or hard-code a convention (e.g. Dummy 4 = LH).

---

## Issue 5: Flight duration / heats per flight

**User report:** Initial_Flights.pdf shows 17–18 heats/flight, ≈ 90+ min each. Target is ≈5.5 min/heat average, ~1 h/flight, configurable flight count.

### Current heats-per-flight algorithm

File: [services/flight_builder.py:194-214](../services/flight_builder.py#L194-L214)

```python
total_non_axe = len(all_heats)
MIN_HEATS_PER_FLIGHT = 2
if num_flights and num_flights > 0 and total_non_axe > 0:
    target_flights = int(num_flights)
    heats_per_flight = math.ceil(total_non_axe / target_flights)
    if heats_per_flight < MIN_HEATS_PER_FLIGHT and total_non_axe >= MIN_HEATS_PER_FLIGHT:
        heats_per_flight = MIN_HEATS_PER_FLIGHT
        clamped = math.ceil(total_non_axe / heats_per_flight)
        ...
        target_flights = clamped
else:
    heats_per_flight = 8
    target_flights = math.ceil(total_non_axe / heats_per_flight) if total_non_axe else 0
```

- When `num_flights` is provided (caller explicitly): `heats_per_flight = ceil(total / num_flights)`, clamped to a minimum of 2.
- When omitted: default is **8 heats/flight**, fixed.

Post-build heat assignment loop at [services/flight_builder.py:239-258](../services/flight_builder.py#L239-L258) creates `target_flights` rows and rounds up fill.

Spec reference: [FlightLogic.md §3.1](../FlightLogic.md#L77-L83) documents "Default 8 heats per flight; `ceil(total_pro_heats / heats_per_flight)`."

### Per-event heat duration field

**None.** Searched the `Event` model, `config.py`, and `services/*.py`:

```
Grep:  minutes_per_heat | heat_duration | target_flight | flight_minutes |
       FLIGHT_DURATION | avg_heat | average.?heat        (case-insensitive)
Hits:  services/schedule_status.py   (incidental "avg_heats_per_flight" stat)
       services/flight_builder.py    (comments only)
       docs/solutions/logic-errors/flight-builder-per-event-stacking-2026-04-21.md
       DEVELOPMENT.md, SESSION_HANDOFF_2026-04-21.md
       routes/scheduling/flights.py  (string "heats per flight" in flash)
       FlightLogic.md
```

No duration-related field exists on the `Event` model ([models/event.py](../models/event.py)). No constant in config.py. No per-stand-type duration mapping.

### schedule_config structure (live data)

**Tournament 1** (`Missoula Pro Am (WOOD TEST) 2026`):

```json
{
  "saturday_college_event_ids": [13, 11],
  "friday_event_order": [21, 22, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
                         23, 24, 25, 26, 27, 28, 1, 2, 3, 4, 5, 6, 29, 30],
  "friday_pro_event_ids": [],
  "friday_feature_notes": "QA test note"
}
```

**Tournament 2** (`Missoula Pro Am (flights test) 2026`):

```json
{
  "friday_pro_event_ids": [76],
  "friday_feature_notes": "",
  "saturday_college_event_ids": [58, 59, 69, 70, 64]
}
```

**Keys observed across live tournaments** (no other keys exist in the data):
- `friday_pro_event_ids` — Friday Night Feature pro event selection (excludes from Saturday flights)
- `friday_feature_notes` — free-text annotation for FNF
- `saturday_college_event_ids` — college events that spill to Saturday
- `friday_event_order` — optional custom ordering of Friday events (`int` list)
- `saturday_event_order` — optional custom ordering of Saturday events (referenced in [services/schedule_builder.py:453](../services/schedule_builder.py#L453) but not present in live data)

**No `num_flights`, no `heats_per_flight`, no duration key is persisted.** The flight count is a form parameter, captured only at build time.

### Current Missoula 2026 flight counts (live DB)

```
Tournament 1 (WOOD TEST)   — 8 flights,  8–9 heats each
Tournament 2 (flights test)— 3 flights, 17–18 heats each  ← user's Initial_Flights.pdf case
```

**How the count was derived (T=2 forensics):**
- `build_flights` form default is `num_flights=4` ([templates/pro/build_flights.html:62-64](../templates/pro/build_flights.html#L62-L64)), range 2-10.
- User chose `num_flights=3` on the T=2 build (produces 53 pro heats ÷ 3 ≈ 18 per flight).
- The clamp at [routes/scheduling/flights.py:168-179](../routes/scheduling/flights.py#L168-L179) only kicks in when `heats_per_flight < 2`, so large heat counts are not clamped upward.

No runtime configuration ties minutes/heat to flight count — the operator picks a number and the algorithm fills flights to match.

### Implementation hook point

Any "minutes-per-heat × target-minutes-per-flight → num_flights" calculation must add:
1. A duration field (per-event, per-stand-type, or single global average). No column exists today.
2. A derivation step before `build_pro_flights` that reads total expected minutes, divides by target flight length, rounds up to `num_flights`, and hands it to `build_pro_flights(tournament, num_flights=...)`.
3. A persistence key in `schedule_config` if the operator's choice is to survive across build attempts (today it's form-only, not remembered).

UI today: [templates/pro/build_flights.html:60-73](../templates/pro/build_flights.html#L60-L73) shows a `<select>` of 2-10 flights with a JS-computed "Estimated heats per flight" label — no minutes estimate.

---

## Cross-cutting concerns

### Shared functions touched by multiple issues

| Function / file | Touches |
|---|---|
| `services/flight_builder.build_pro_flights` | Issues 1 (indirectly via FNF event exclusion), 3 (Relay excluded), 4 (LH flight spread penalty), 5 (heats_per_flight) |
| `services/flight_builder.integrate_college_spillover_into_flights` | Issues 1 (Chokerman vs Speed Climb routing), 2 (read path, async gap) |
| `services/heat_generator._generate_springboard_heats` | Issue 4 (LH spreading) |
| `services/schedule_builder._add_mandatory_day_split_run2` / `get_saturday_ordered_heats` | Issue 1 (Speed Climb Run 2 display only, not flight placement) |
| `Tournament.schedule_config` JSON | Issues 2 (spillover selection), 5 (would be the home for num_flights / duration if persisted) |
| `routes/scheduling/flights.build_flights` (async branch) | Issue 2 (break point A) |

### Required model schema changes (candidate — not prescriptive)

- Issue 3: none required (pseudo-heat approach) OR a new `HeatKind` discriminator on `Heat` if synthesising a rows.
- Issue 4 (consecutive-heat variant): a `specific_stands_lh` in `STAND_CONFIGS` or a new `lh_dummy_number` event field.
- Issue 5: either a new `Event.minutes_per_heat` column or a per-stand-type dict in `config.py`.

### Required migrations

Depends on schema decisions above. If Issue 5 adds `Event.minutes_per_heat`, follow CLAUDE.md migration rules: PG-safe direct `op.add_column`, nullable with server_default, SQLite/PG dialect portability, retire from `KNOWN_NULLABLE_DRIFT` in `tests/test_migration_integrity.py` if applicable.

### Data backfill needed

- Issue 3: none — Pro-Am Relay event row is created on demand by `create_proam_relay_event`.
- Issue 4: none for handedness itself; the flag already exists on `ProCompetitor`.
- Issue 5: if a duration field is added, either accept `NULL` (compute from stand-type defaults) or backfill with stand-type averages at migration time.

---

## Recommended implementation order

Dependency-driven ordering. Each line is a one-shot rationale:

1. **Issue 2 (Break point A) — async flight build chains spillover.** Smallest diff, unlocks a real working spillover path that Issues 1 and 3 will build on; without this, Issues 1 & 3 remain invisible because no spillover integration ever runs.
2. **Issue 1 — generalise mandatory Run-2 routing to all `DAY_SPLIT_EVENT_NAMES`.** Directly leans on the fixed integration path from step 1; also tightens the non-Chokerman `run_number` filter (Break point B).
3. **Issue 5 — add persistence + optional minutes/heat input to flight build.** Independent of 1-3; pick it up next because it changes the user-facing "Build Flights" form, which Issues 1 & 2 rendered trustworthy in steps 1-2.
4. **Issue 3 — Pro-Am Relay placement in final flight.** Needs a design call on pseudo-heat vs synthesised Heat row; depends on a reliable final-flight pointer (step 1) and predictable flight count (step 5).
5. **Issue 4 — LH Springboard grouping to same tree.** Do last because it (a) contradicts current heat-gen behaviour *and* FlightLogic.md §5.3 simultaneously, so requires the most explicit user sign-off on the new rule, and (b) has the widest blast radius into `_generate_springboard_heats`, flight builder LH penalties, and existing tests (`tests/test_flight_builder_lh_constraint.py`, `tests/test_pro_entry_importer_handedness.py`).
