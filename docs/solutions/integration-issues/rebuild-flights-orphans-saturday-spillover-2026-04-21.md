---
title: "Rebuild Flights Only" silently orphans Saturday college spillover heats
date: 2026-04-21
last_updated: 2026-04-22
category: integration-issues
module: flight_builder
problem_type: integration_issue
component: service_object
severity: high
symptoms:
  - "Preflight dashboard shows 'Spillover not integrated into flights' warnings for 5+ events"
  - "Men's Standing Block Speed / Women's SB Speed / Jack & Jill / Men's OP / Women's OP all have heats with flight_id=NULL after rebuilding"
  - "Spillover heats that WERE integrated vanish from flights after clicking 'Rebuild Flights Only'"
root_cause: missing_workflow_step
resolution_type: code_fix
related_components:
  - development_workflow
tags:
  - flight-builder
  - saturday-spillover
  - integration-gap
  - rebuild-flights
  - workflow-trap
---

# "Rebuild Flights Only" silently orphans Saturday college spillover heats

## Problem

`build_pro_flights()` clears every `Heat.flight_id` to NULL as its first step (including heats previously integrated via `integrate_college_spillover_into_flights()`). The `POST /flights/build` route called build only — it never re-integrated spillover. Clicking "Rebuild Flights Only" in the UI therefore silently orphaned every college spillover heat that had been integrated before.

## Symptoms

- Preflight dashboard shows multiple "Spillover not integrated into flights" high-severity warnings for events the judge had marked for Saturday spillover.
- Example from a reported show: `Men's Standing Block Speed: 1 heat(s) not assigned to a Saturday flight`, `Jack & Jill: 4 heats(s) not assigned`, `Men's Obstacle Pole: 9 heat(s) not assigned`, `Women's OP: 4 heat(s) not assigned`.
- The user had gone through `/one-click-generate` once (which DOES chain integrate-spillover), then later hit "Rebuild Flights Only" to tweak flight count — orphaning every spillover heat in the process.
- Manual workaround: click the separate "Integrate Spillover" button after rebuild.

## What Didn't Work

The `/one-click-generate` route at `routes/scheduling/flights.py` correctly chains heat-gen → build-flights → integrate-spillover atomically. The assumption was that it was the only path users took. But the UI exposes a separate "Rebuild Flights Only" button (`POST /flights/build`) intended for tweaking flight count without re-generating heats. That route only called `build_pro_flights()`. Silent divergence — two paths, different semantics, same physical state mutation (flight_id wipe).

## Solution

Mirror what `one_click_generate` already does. After `build_pro_flights()`, chain `integrate_college_spillover_into_flights()` using the saturday_college_event_ids persisted in `tournament.schedule_config`.

In `routes/scheduling/flights.py::build_flights` (POST branch). Note: V2.14.0 grew the chain — Phase 4 added `integrate_proam_relay_into_final_flight` BEFORE spillover so Chokerman Run 2 still closes the show, and Phase 5 added a `get_last_lh_flight_warnings` flash loop right after the build to surface LH springboard contention. The chaining principle below is unchanged; the chain is just longer.

```python
try:
    built = build_pro_flights(tournament, num_flights=num_flights)
    log_action('flights_built', 'tournament', tournament_id, {'count': built})
    db.session.commit()
    flash(text.FLASH['flights_built'].format(num_flights=built), 'success')

    # V2.14.0 Phase 5: surface LH springboard dummy contention warnings.
    from services.flight_builder import get_last_lh_flight_warnings
    for w in get_last_lh_flight_warnings(tournament_id):
        flash(
            f"LH SPRINGBOARD CONTENTION: Flight {w['flight_number']} "
            f"contains {w['lh_count']} left-handed cutters. ...",
            'warning',
        )

    # build_pro_flights wipes every Heat.flight_id (including college
    # spillover that was previously integrated). Chain the relay + spillover
    # integration so "Rebuild Flights Only" doesn't silently orphan them.
    # Order: relay BEFORE spillover so Chokerman Run 2 lands AFTER the relay
    # (FlightLogic.md §4.1 show-climax rule).
    from services.flight_builder import (
        integrate_college_spillover_into_flights,
        integrate_proam_relay_into_final_flight,  # V2.14.0 Phase 4
    )
    relay_result = integrate_proam_relay_into_final_flight(tournament)
    if relay_result.get('placed'):
        flash('Pro-Am Relay placed in the final flight.', 'success')

    db_config = tournament.get_schedule_config() or {}
    saturday_college_event_ids = [
        int(i) for i in db_config.get('saturday_college_event_ids', [])
    ]
    integration = integrate_college_spillover_into_flights(
        tournament, saturday_college_event_ids,
    )
    if integration.get('integrated_heats'):
        db.session.commit()
        flash(
            f"Integrated {integration['integrated_heats']} college spillover "
            f"heat(s) into Saturday flights.",
            'success',
        )

    from services.saw_block_assignment import trigger_saw_block_recompute
    trigger_saw_block_recompute(tournament)
except Exception as e:
    db.session.rollback()
    flash(text.FLASH['flights_error'].format(error=str(e)), 'error')
```

## Why This Works

**The root invariant:** `build_pro_flights` wipes all `Heat.flight_id` values including non-pro heats. This is by design — it's a clean slate rebuild. The workflow obligation is on the caller: every code path that calls `build_pro_flights` is responsible for re-integrating spillover afterward. `one_click_generate` already did this; `build_flights` didn't. The fix enforces the invariant at every call site that rebuilds flights.

**Why read from schedule_config and not session:** The event IDs marked for Saturday spillover are persisted to the Tournament's `schedule_config` JSON (via `get_schedule_config()`). Session-scoped storage would break when the user rebuilds from a different browser session or after a timeout. Persistent storage is the source of truth.

**Rollback on failure:** Added `db.session.rollback()` in the exception handler so a partial rebuild (flights created but integrate failed) doesn't leave the DB in an inconsistent state — the try/except scope includes both operations.

## Prevention

- Regression test (future): a dedicated integration test that marks events for Saturday spillover, calls `/flights/build`, and asserts all spillover heats have `flight_id != NULL`. Not currently written; the existing smoke tests cover the happy-path one-click flow.
- **Architectural rule:** `build_pro_flights` should only be called by orchestrating routes that also handle downstream integration. If a future caller is added that skips spillover, it must document why and flag the risk of orphaned heats. Consider making `build_pro_flights` always call `integrate_college_spillover_into_flights` internally — but only if a design review concludes the coupling is preferable to the current orchestrator pattern.
- **UI signal:** The "Rebuild Flights Only" button's label implies it won't touch spillover, which was true before V2.11.0 (when spillover integration didn't exist) but became misleading after. Button copy is now accurate thanks to this fix — "Rebuild Flights Only" means "rebuild pro flights AND re-integrate spillover." If the behavior is ever split again (e.g., power-user mode that skips integration), the label must be updated in lockstep.
- **Preflight check** in `services/preflight.py` (`spillover_not_in_flights` finding code in `build_preflight_report`) continues to catch orphaned spillover heats. It's the last line of defense — even if the fix ever regresses, the preflight dashboard will surface the issue before the show goes live. Reference the finding code, not a line range — preflight has accreted checks since this doc was written and line numbers drift.
- **Pattern-to-not-copy:** The snippet above uses `except Exception as e: flash(... .format(error=str(e)), 'error')` because that's exactly what the shipped code does (it predates this fix and wasn't in scope to change). Per CLAUDE.md Section 5 error-handling rules and the fix already applied in `routes/registration.py`, leaking raw exception text to a user-facing flash is an anti-pattern. New callers of `build_pro_flights` should log the exception server-side and show a generic admin-contact message to the user. Cleanup of this specific flash is tracked separately from this fix.
- **Config coercion:** `[int(i) for i in db_config.get('saturday_college_event_ids', [])]` assumes the config writer side (the events page POST handler) validates integers before persisting. Per CLAUDE.md Section 6 input-conversion rules, if a future caller reads this config from a less-trusted source, wrap the coercion in `try/except (TypeError, ValueError)` and skip malformed entries.

## Related Issues

- PR #55 (merged as `e4e45a0`): this fix bundled with cross-event adjacency + drag-drop.
- [flight-builder-cross-event-same-stand-adjacency-2026-04-21.md](../logic-errors/flight-builder-cross-event-same-stand-adjacency-2026-04-21.md): sibling fix in same PR.
- `services/preflight.py` `spillover_not_in_flights` check: preflight warning users will see if this regresses.
