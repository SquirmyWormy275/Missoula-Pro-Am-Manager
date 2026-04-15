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

## Solution
`Heat.competitors` (JSON) is the authoritative source. After any write, call:

```python
db.session.flush()  # ensure heat.id is assigned
heat.sync_assignments(event.event_type)
```

This is already invoked after heat generation, flight rebuild, and competitor moves. New code writing to `Heat.competitors` must follow suit.

The routes `/scheduling/<tid>/heats/sync` (GET JSON check, POST reconcile) exist to repair drift post-hoc.

## Prevention
- Never write to `HeatAssignment` directly. Always write `Heat.competitors` + call `sync_assignments()`.
- New heat-mutating code: grep for `sync_assignments` calls in similar routes and mirror the pattern.
- Run the sync-check endpoint as part of any pre-show preflight.
