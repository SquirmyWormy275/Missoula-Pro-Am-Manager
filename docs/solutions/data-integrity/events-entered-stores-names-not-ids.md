---
module: woodboss
date: 2026-04-15
problem_type: logic_error
component: service_object
severity: critical
root_cause: wrong_api
resolution_type: code_fix
symptoms:
  - "Wood Count Report shows zero college blocks the day before the show"
  - "closed_event_count returns 0 for every athlete"
  - "6-CLOSED-events-per-athlete enforcement silently not running"
  - "By-species grouping filters out all college rows"
tags:
  - "woodboss"
  - "college"
  - "events-entered"
  - "data-model"
---

# `events_entered` JSON stores event NAMES, not IDs

## Problem
Multiple subsystems (Woodboss `_count_competitors` / `_list_competitors`, `CollegeCompetitor.closed_event_count`) silently returned zero because they looked up `events_entered` entries against an ID-keyed event dict. Entries never matched, code hit the `if not event: continue` guard, and the failure was invisible until the day before race day.

## Root Cause
`CollegeCompetitor.events_entered` and `ProCompetitor.events_entered` both store event **names** as strings (e.g. `"Underhand Hard Hit"`), not event IDs. This is the format Excel imports, Google Forms round-trip, and registration UIs all produce. Any service layer that builds an ID-only event lookup will silently fail every resolution.

## Solution
Resolvers must try ID first, then fall back to name:

```python
event_id_map = {e.id: e for e in events}
event_name_map = {e.name: e for e in events}

for entry in competitor.events_entered:
    event = event_id_map.get(entry) or event_name_map.get(entry)
    if not event:
        continue
    ...
```

Pro path already did this via `_get_pro_event_map`. College paths must match.

## Prevention
- Any new code reading `events_entered` on either competitor model MUST build both id_map and name_map.
- Log a warning when a resolution misses both — silent skip hides the class of bug that caused the V2.8.2 Woodboss incident.
- CLAUDE.md Section 4 documents this explicitly; treat it as a cross-cutting invariant.
