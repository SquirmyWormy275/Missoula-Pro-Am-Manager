# Hand-Saw Stand Block Alternation — Recon Report

**Date:** 2026-04-21
**Scope:** Read-only recon. No source changes.
**Domain:** Missoula Pro-Am — Single Buck, Double Buck, Jack & Jill Sawing (all pro/college, all genders).

**Goal:** Determine how stand assignment currently works for hand-sawing events and where block-alternation logic (block A = stands 1-4, block B = stands 5-8) must land so that consecutive hand-saw heats swap physical blocks.

---

## TASK 1 — Current stand configuration for hand-saw events

**File:** [config.py:330-335](../config.py#L330-L335)

```python
'saw_hand': {
    'total': 8,
    'groups': [[1, 2, 3, 4], [5, 6, 7, 8]],
    'labels': ['Stand 1', 'Stand 2', 'Stand 3', 'Stand 4',
               'Stand 5', 'Stand 6', 'Stand 7', 'Stand 8']
},
```

Findings:
- `total` (treated as max_stands) = **8**
- **`groups` key already encodes the two physical blocks** as `[[1,2,3,4],[5,6,7,8]]`. However, grep shows `groups` is **never consumed** by any code in the repo — it is informational only.
- `labels` are generic `"Stand 1"` .. `"Stand 8"`. No existing label discriminates block A vs B.
- No `specific_stands` key (saw_hand uses full 1..N fallback in [heat_generator.py:743](../services/heat_generator.py#L743)).

Hand-saw event stand_type assignments — **all six reference the same `saw_hand` key**:

| Event | File / line | stand_type | Partnered | Gender |
|---|---|---|---|---|
| College Single Buck | [config.py:400](../config.py#L400) | `saw_hand` | no | gendered |
| College Double Buck | [config.py:401](../config.py#L401) | `saw_hand` | yes (same gender) | gendered |
| College Jack & Jill | [config.py:402](../config.py#L402) | `saw_hand` | yes (mixed) | — |
| Pro Single Buck | PRO_EVENTS — `saw_hand` | `saw_hand` | no | gendered |
| Pro Double Buck | PRO_EVENTS — `saw_hand` | `saw_hand` | yes (same) | gendered |
| Pro Jack & Jill | PRO_EVENTS — `saw_hand` | `saw_hand` | yes (mixed) | — |

No deviations. One `saw_hand` config covers every hand-saw event.

---

## TASK 2 — Heat size for hand-saw events

**Files:** [services/heat_generator.py:114-122](../services/heat_generator.py#L114-L122), [services/heat_generator.py:140-142](../services/heat_generator.py#L140-L142), [services/heat_generator.py:667-680](../services/heat_generator.py#L667-L680)

max_stands resolution chain:

```python
stand_config = config.STAND_CONFIGS.get(event.stand_type, {})
max_per_heat = event.max_stands if event.max_stands is not None else stand_config.get('total', 4)
```

Order:
1. `event.max_stands` (explicit per-event override, usually None)
2. `STAND_CONFIGS[stand_type]['total']` → returns **8** for saw_hand
3. Hard default `4` if stand_type unknown

For hand-saw events specifically, `_generate_saw_heats()` at [heat_generator.py:667-680](../services/heat_generator.py#L667-L680) **then caps heat size to 4 regardless**:

```python
def _generate_saw_heats(competitors, num_heats, max_per_heat, stand_config, event=None,
                        gear_violations=None):
    actual_max = min(max_per_heat, 4)        # Saw groups are 4 each
    num_heats = math.ceil(len(competitors) / actual_max)
    return _generate_standard_heats(competitors, num_heats, actual_max, event=event,
                                    gear_violations=gear_violations)
```

For N=12 competitors: `ceil(12 / 4) = 3 heats of 4 competitors each`.

**Conclusion for Task 2:** Heats are already sized to a max of 4. **The alternation fix does NOT need to split heats.** It is a stand-number-assignment / labeling concern. However — and this is the trap — `_stand_numbers_for_event()` at [heat_generator.py:734-743](../services/heat_generator.py#L734-L743) currently hands the generator `[1, 2, 3, 4]` for every saw heat because it just does `range(1, max_per_heat + 1)`. Every saw heat is therefore pinned to stands 1-4 today; nobody is ever sitting on stands 5-8.

---

## TASK 3 — Stand assignment structure

**File:** [models/heat.py:51-119](../models/heat.py#L51-L119)

`Heat.stand_assignments` column:

```python
stand_assignments = db.Column(db.Text, nullable=False, default='{}')
# JSON-encoded dict: { competitor_id_as_str: stand_number_int }
```

Shape:
```json
{ "10": 1, "11": 1, "12": 2, "13": 2 }   // partnered — both partners share stand
{ "10": 1, "11": 2, "12": 3, "13": 4 }   // non-partnered — one per stand
```

- Keys are **stringified** competitor IDs (the `set_stand_assignment()` helper at [models/heat.py:110-114](../models/heat.py#L110-L114) forces `str(competitor_id)`).
- Values are integer stand numbers (1..total).
- Partnered pairs share one stand number (both competitor IDs map to the same int) — see TASK 7.

Assignment is **computed at heat generation time and persisted**. The logic lives in [heat_generator.py:161-220](../services/heat_generator.py#L161-L220):

```python
stand_numbers = _stand_numbers_for_event(event, max_per_heat, stand_config)
# ... per heat ...
if is_partnered:
    pair_units = _rebuild_pair_units(heat_competitors, event)
    stand_idx = 0
    for unit in pair_units:
        stand_num = stand_numbers[stand_idx] if stand_idx < len(stand_numbers) else stand_idx + 1
        for comp in unit:
            heat.set_stand_assignment(comp['id'], stand_num)
        stand_idx += 1
else:
    for i, comp in enumerate(heat_competitors):
        stand_num = stand_numbers[i] if i < len(stand_numbers) else i + 1
        heat.set_stand_assignment(comp['id'], stand_num)
```

Key properties for the alternation design:
- Stand numbers come from a single per-event `stand_numbers` list, independent of heat index. There is **no current mechanism for per-heat stand selection**.
- Dual-run events reverse stands for run 2 ([heat_generator.py:202-217](../services/heat_generator.py#L202-L217)), but saw events are **single-run**, so that path is not relevant.
- After each write, `heat.sync_assignments(comp_type)` is called ([heat_generator.py:226-227](../services/heat_generator.py#L226-L227)) to mirror the JSON into `HeatAssignment` rows. If alternation changes stand numbers post-generation, `sync_assignments()` must run again.

---

## TASK 4 — Existing stand label rendering

**Helper function:** [routes/scheduling/heat_sheets.py:52-64](../routes/scheduling/heat_sheets.py#L52-L64)

```python
def _stand_label(stand_type: str | None, stand_number) -> str:
    """Return the physical stand label from STAND_CONFIGS, or fall back to raw number."""
    if stand_number is None:
        return "?"
    cfg = config.STAND_CONFIGS.get(stand_type or "", {})
    labels = cfg.get("labels", [])
    try:
        idx = int(stand_number) - 1
        if 0 <= idx < len(labels):
            return labels[idx]
    except (ValueError, TypeError):
        pass
    return str(stand_number)
```

Used at [routes/scheduling/heat_sheets.py:177](../routes/scheduling/heat_sheets.py#L177):
```python
"stand_label": _stand_label(stand_type, assignments.get(str(comp_id))),
```

Previous fix for Speed Climb / Obstacle Pole / Chokerman — **CONFIRMED in place** (all three swapped labels to the custom strings named in the task):
- Obstacle Pole: [config.py:346-349](../config.py#L346-L349) — `['Pole 1', 'Pole 2']`
- Speed Climb: [config.py:350-353](../config.py#L350-L353) — `['Pole 2', 'Pole 4']`
- Chokerman: [config.py:354-357](../config.py#L354-L357) — `['Course 1', 'Course 2']`

This means the label-rendering infrastructure is already data-driven from `STAND_CONFIGS.labels[stand_number - 1]`.

Templates using stand labels/numbers (from grep):
- [templates/scheduling/heats.html](../templates/scheduling/heats.html)
- [templates/scheduling/day_schedule_print.html](../templates/scheduling/day_schedule_print.html) (renders `comp.stand_label` when defined, falls back to `comp.stand`)
- [templates/portal/school_dashboard.html](../templates/portal/school_dashboard.html)
- [templates/portal/competitor_dashboard.html](../templates/portal/competitor_dashboard.html)
- [templates/portal/event_results.html](../templates/portal/event_results.html)

**Piggyback assessment:** The label pipeline can handle any label text keyed by stand index, so "Block A — Stand 1" style labels work **with no plumbing change**. But the fundamental limit is that `_stand_label` uses only the stand number as an index into a flat `labels` array — it does NOT see heat index or block rotation state. If alternation is implemented via per-heat stand numbers (e.g., heat 1 gets {1,2,3,4}, heat 2 gets {5,6,7,8}), existing labels just work. If alternation is implemented via a rendered "Block A" / "Block B" badge that is separate from the stand number, it needs a new render path.

---

## TASK 5 — Flight / run order state

**File:** [services/flight_builder.py](../services/flight_builder.py)

Heat run order is built in `build_pro_flights()` at [flight_builder.py:59-170+](../services/flight_builder.py#L59). Key steps:

1. All non-axe pro heats batch-loaded at [flight_builder.py:104-109](../services/flight_builder.py#L104-L109):
   ```python
   batched_heats = Heat.query.filter(
       Heat.event_id.in_(non_axe_event_ids),
       Heat.run_number == 1
   ).order_by(Heat.event_id, Heat.heat_number).all()
   ```
   Initial order is per-event, sequential within each event.

2. `_optimize_heat_order()` at [flight_builder.py:145-146](../services/flight_builder.py#L145-L146) runs `N_OPTIMIZATION_PASSES = 5` greedy passes to interleave events for crowd variety and competitor rest. `EVENT_SPACING_TIERS` gives `saw_hand` a `(5, 7)` min/target spacing ([flight_builder.py:33-43](../services/flight_builder.py#L33-L43)).

3. The optimizer returns a single ordered list `ordered_heats`. Each entry is then written into `Flight` rows with `heat.flight_id = flight.id` and `heat.flight_position = 1..heats_per_flight`.

**Global sequential order data structure:**
The persistent answer is `Heat.flight_id` + `Heat.flight_position` + `Flight.flight_number`. Joining those three gives the global sequential order of every pro heat during the show. It is **only available after the flight builder runs** — heat generation happens before flight building, so at heat-generation time there is **no authoritative "global heat order" to query**. This is the single biggest design constraint for the alternation feature.

**Existing alternating / round-robin logic:**
- Snake-draft competitor distribution across heats within a single event (best-to-worst bouncing) exists in `_generate_standard_heats()` and friends — but that is competitor distribution, not stand assignment.
- `integrate_college_spillover_into_flights()` distributes overflow college events round-robin across flights. That is flight-level, not stand-level.
- **No code currently does "alternate the stand-group between consecutive heats" for any event type.** This is greenfield.

---

## TASK 6 — Day boundary handling

**Finding:** There is **no `day_of_week` column on `Heat`**. Day context is derived, not stored.

Derivation points:
- College events (`event.event_type == 'college'`) implicitly run Friday.
- Pro events (`event.event_type == 'pro'`) implicitly run Saturday.
- Dual-run split: Friday shows run 1, Saturday shows run 2 — enforced at render time in [routes/scheduling/heat_sheets.py:108-112](../routes/scheduling/heat_sheets.py#L108-L112):
  ```python
  if event.requires_dual_runs and event.name in DAY_SPLIT_EVENT_NAMES:
      if day == "friday":
          event_heats = [h for h in event_heats if h.run_number == 1]
      elif day == "saturday" or item.get("is_run2"):
          event_heats = [h for h in event_heats if h.run_number == 2]
  ```
- Friday Night Feature / Saturday show blocks are separated at the **schedule** layer, not the Heat layer — see `_hydrate_schedule_for_display()` at [routes/scheduling/heat_sheets.py:67-79](../routes/scheduling/heat_sheets.py#L67-L79): `schedule` is a dict with `friday_day`, `friday_feature`, `saturday_show` keys.

**Implication for alternation:** Since all hand-saw heats for a given tournament are single-run:
- College hand-saw heats all run Friday day.
- Pro hand-saw heats all run Saturday.
- Friday Night Feature could in principle contain hand-saw heats, but today there is no heat-gen/flight path for it (documented gap in CLAUDE.md §5).

Day boundary detection for alternation can rely on `event.event_type` alone today. If Friday Night Feature ever gains hand-saw heats, a stronger mechanism (a `day` column on Heat or Flight, or a schedule-block tag) will be needed.

---

## TASK 7 — Partnered event compatibility

**Partnered stand assignment:** [heat_generator.py:175-182](../services/heat_generator.py#L175-L182) already assigns one stand number per pair; both partners' IDs map to the same stand in `stand_assignments` JSON.

Example (2-pair Double Buck heat, stands 1-4 available):
```json
{ "10": 1, "11": 1, "12": 2, "13": 2 }
```

So a "stand" in `saw_hand` context = one saw = one pair. With max_per_heat = 4 stands, the maximum heat size for partnered saw events is effectively 4 pairs (8 humans).

**Pair reconstruction:** `_rebuild_pair_units()` at [heat_generator.py:448](../services/heat_generator.py#L448) — returns a list of units `[[comp1, comp2], [comp3, comp4], [solo5], ...]`. Each unit occupies exactly one stand slot. Solos (odd partner out) occupy one stand alone.

**Block alternation compatibility:** The block-alternation idea — "heat N runs on block A = stands 1-4, heat N+1 runs on block B = stands 5-8" — treats each pair/solo as one stand-occupant. The existing pair-per-stand data model supports this **directly**. The change is purely: pick the 4 stand numbers for this heat from {1,2,3,4} or {5,6,7,8} based on alternation state.

No code or schema change needed on the partnered side — `Heat.stand_assignments` already stores pair-shared stand numbers.

---

## TASK 8 — Existing tests and fixtures

Test files that exercise heat generation or stand assignments:

| File | Scope |
|---|---|
| [tests/test_heat_generator.py](../tests/test_heat_generator.py) | Primary heat-generator tests. Includes hand-saw cases (gear-sharing conflict detection on Single Buck around lines 250-282; college saw_hand non-partnered around 409-415). |
| [tests/test_heat_gen_integration.py](../tests/test_heat_gen_integration.py) | Integration tests spanning heat gen + downstream. |
| [tests/test_flight_builder_25_pros.py](../tests/test_flight_builder_25_pros.py) | Realistic 25-pro flight build; touches saw_hand spacing. |
| [tests/test_partnered_events_realistic.py](../tests/test_partnered_events_realistic.py) | Partnered events (Double Buck, Jack & Jill) in realistic scenarios. |
| [tests/test_judge_sheet.py](../tests/test_judge_sheet.py) | Heat sheet / stand label rendering for judge sheets. |
| [tests/test_route_smoke.py](../tests/test_route_smoke.py) | Route-level smoke of heat/flight endpoints. |

Shared fixtures:
- [tests/conftest.py](../tests/conftest.py) — in-memory SQLite, app/client/db, seeded tournament.
- [tests/fixtures/synthetic_data.py](../tests/fixtures/synthetic_data.py) — 25-pro roster with realistic SB/DB/J&J partnerships and gear-sharing; usable as alternation test bed.

No existing test asserts the specific stand numbers chosen for saw heats (grep on `"saw_hand"` combined with `stand_assignments` in tests returned no numeric-stand assertions beyond "stand is assigned"). A new alternation test will need to be written from scratch — not extending an existing one.

---

## Risks and gaps for implementing block alternation

1. **`_stand_numbers_for_event()` is per-event, not per-heat** ([heat_generator.py:734-743](../services/heat_generator.py#L734-L743)). Today it returns a single list that every heat in that event reuses. Alternation requires per-heat stand selection. The natural intervention point is the loop at [heat_generator.py:164-186](../services/heat_generator.py#L164-L186), where `heat_num` is available.

2. **`groups` key in `STAND_CONFIGS['saw_hand']` is unused.** It already lists `[[1,2,3,4],[5,6,7,8]]`. Implementation can consume this directly instead of hardcoding blocks in Python. A future non-4+4 saw layout (e.g., a show with only 6 stands arranged 3+3) would just edit config.

3. **No cross-event alternation state today.** If alternation must span consecutive hand-saw heats across events (e.g., last Single Buck heat on Block A → first Double Buck heat on Block B), the state has to thread between separate `generate_event_heats()` calls. Options:
   - Per-tournament counter persisted somewhere (new column on Tournament, or derived from max(heat.id) query at gen time).
   - Re-assign stands post-hoc after flight builder runs — since global order only exists after flight build (Task 5).
   - Keep alternation strictly within-event and accept a reset at each event boundary.

4. **Flight builder re-orders heats after generation.** The sequence `_optimize_heat_order()` produces is not the same as heat_number order. If alternation is computed at heat-gen time, re-running flight builder can interleave hand-saw heats with non-saw heats or place back-to-back SB heats from different events, breaking the "every other saw heat flips blocks" property the domain wants. The correct point to assign block rotation may be **after** flight builder runs, walking the final global run order and re-assigning stand numbers to saw heats by block-alternation.

5. **`sync_assignments()` must re-run** after any stand re-assignment, or `HeatAssignment` rows (used by the validation service) will drift from the JSON ([models/heat.py:121-135](../models/heat.py#L121-L135)).

6. **Stock Saw override exception:** [heat_generator.py:735-737](../services/heat_generator.py#L735-L737) hardcodes college Stock Saw to stands `[7, 8]`. Stock Saw is `stand_type='stock_saw'` — a different stand_type from saw_hand — so it does not conflict directly. But the code lives in the same `_stand_numbers_for_event` helper, so the "saw_hand-specific block-alternation logic" branch needs to coexist cleanly with the existing Stock Saw branch.

7. **Dual-run saw events don't exist today** but the dual-run branch at [heat_generator.py:192-220](../services/heat_generator.py#L192-L220) reverses stand numbers per-heat. If a saw event ever becomes dual-run, block-alternation and run-2-stand-reversal interact and need a defined precedence.

8. **Label display:** `STAND_CONFIGS['saw_hand']['labels']` today is flat `"Stand 1".."Stand 8"`. If the show wants heat sheets to say "Block A — Stand 1", the labels array can be updated, but existing signage, printed heat-sheet stacks, and crowd-facing kiosk pages may need to be re-checked.

---

## Open Questions for Alex

1. **Scope of alternation.** Does alternation apply **within a single event** (SB heat 1 → Block A, heat 2 → Block B, heat 3 → Block A, …) or across **all consecutive hand-saw heats in global run order**, possibly crossing event boundaries (last SB heat Block A → first DB heat Block B)?

2. **Event boundary reset.** If scope is within-event: when SB ends on Block B and DB starts, does DB also start on Block A (reset) or inherit Block A from "the other block than SB ended on"?

3. **Day boundary reset.** If Friday Night Feature ever hosts saw heats, or college hand-saw spills to Saturday overflow, should alternation reset at the day boundary or continue?

4. **Computation timing.** Compute alternation at (a) heat-generation time (simpler, but ignores flight-builder reshuffles), (b) after flight builder runs (complex, but respects true run order), or (c) both — generate a default at heat-gen and recompute after flight?

5. **Labels.** Do we change `STAND_CONFIGS['saw_hand']['labels']` to block-aware strings (e.g. `"Block A — Stand 1"`), keep the numeric labels and instead add a new "Block" column in templates, or leave labels alone and rely on the crowd/judges to read physical stand numbers?

6. **Pro vs college independence.** Do college hand-saw (Friday) and pro hand-saw (Saturday) each get their own alternation sequence that starts fresh, or should we carry state across days?

7. **Partnered-event stand stability across days.** Jack & Jill and Double Buck on the pro side — if a pair runs first on Block A, is there any domain reason they'd want the same block on a hypothetical re-run (none today, but confirm)?

8. **Gear-sharing override.** The saw-heat generator already has gear-sharing constraints that force specific heat placement ([heat_generator.py:233-252](../services/heat_generator.py#L233-L252) gear_violations fallback). If block alternation conflicts with a gear-share constraint (e.g., putting two saws on Block A back-to-back forces a gear-share violation), which wins — gear-share safety or strict block alternation?

9. **Edge case — 1 or 2 heats.** For a 4-competitor event (1 heat) or 8-competitor event (2 heats), alternation is degenerate or trivial. Any special behavior wanted, or just "put the single heat on Block A"?

10. **Display scope.** Is this a scheduling / logistics change (backstage only — judges and crew see it on heat sheets) or a competitor-facing change (competitors see "Block A" on their portal)?
