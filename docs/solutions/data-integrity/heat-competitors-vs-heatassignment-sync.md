---
module: heat-generation
date: 2026-04-15
problem_type: logic_error
component: database
severity: high
root_cause: missing_workflow_step
resolution_type: code_fix
symptoms:
  - "Validation service reports missing competitors that are actually in the heat"
  - "Heat.competitors JSON and HeatAssignment rows diverge"
tags:
  - "heats"
  - "data-model"
  - "validation"
---

# Dual heat representations (JSON + rows) drift without explicit sync

## Problem
Heat composition is stored in two places: `Heat.competitors` (JSON list) and `HeatAssignment` (rows). The heat generator reads/writes only the JSON; the validation service reads only the rows. Mutations to one without the other produce false validation failures and stale displays.

## Root Cause
Deliberate design compromise — JSON is ergonomic for bulk heat gen, rows are ergonomic for validation queries. Both coexist but nothing auto-syncs them.

## Solution (SUPERSEDED by D12-C, 2026)

The fix recorded here was: `Heat.competitors` (JSON) is authoritative, and
after any write call `db.session.flush()` then
`heat.sync_assignments(event.event_type)`. Two routes,
`/scheduling/<tid>/event/<eid>/heats/sync-check` and `.../sync-fix`, repaired
drift after the fact, and `run_preflight_autofix` swept every heat in a
tournament doing the same thing.

None of that exists any more, and following it now is a bug. Register decision
D12-C removed the second store rather than continuing to reconcile it:

- Commit A gave `heat_assignments` a NOT NULL `uid` with a foreign key onto
  `competitors.uid`, which is the thing the JSON column could never have.
- Commit E made `Heat.set_roster` the single write target. It writes the rows
  and renders the JSON columns from them.
- Commit F2 deleted every reader of the columns: `sync_assignments`,
  `json_competitors`, `json_stand_assignments`, the preflight
  `heat_sync_mismatch` check, both sync routes and their template form, the
  autofix sweep and its `heats_fixed` / `heats_checked` counters, and the
  reseed script's heats UPDATE block.
- F3 drops `heats.competitors` and `heats.stand_assignments`.

## Current rule

Write with `heat.set_roster(event.event_type, comp_ids, stands)`. Read with
`heat.get_competitors()` and `heat.get_stand_assignments()`. Do not touch
either JSON column. `set_roster` does not need `heat.id`, so it works on a heat
that has not been flushed, and it raises `BadHeatAssignment` before writing
anything if the roster names a competitor that does not exist or names one
twice.

## What this document is still worth

Two representations of the same fact drift, and no amount of discipline about
calling the sync function fixes that: the sync call is a thing a caller can
forget, and every one of the ten mutation sites had to remember it. The repair
routes and the preflight blocker were the cost of the design, not the fix for
it. The lesson generalises past heats, and it is the argument behind open
questions 13 and 20 in the register.
