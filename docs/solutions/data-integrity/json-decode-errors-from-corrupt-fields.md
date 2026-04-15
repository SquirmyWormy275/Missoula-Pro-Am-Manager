---
type: bug
problem_type: data-integrity
severity: medium
symptoms:
  - "500 error: json.decoder.JSONDecodeError on /dashboard or /portal/*"
  - "Single corrupt JSON cell breaks the whole page"
tags:
  - "json"
  - "models"
  - "resilience"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Corrupt JSON in a single row cascades to 500 across every list view

## Problem
Models store lists/dicts as JSON TEXT columns (`events_entered`, `partners`, `gear_sharing`, `stand_assignments`, `payouts`, etc.). A single row with malformed JSON propagated a `JSONDecodeError` up through any route that iterated the collection, breaking dashboards and portals entirely.

## Root Cause
Raw `json.loads()` calls with no exception handling. Manual DB edits, partial writes, or format drift produced the corrupt values.

## Solution
Every `.get_*()` method on models must guard against `JSONDecodeError` and return an empty list/dict:

```python
def get_events_entered(self):
    if not self.events_entered:
        return []
    try:
        return json.loads(self.events_entered)
    except (json.JSONDecodeError, TypeError):
        return []
```

Already applied to: `CollegeCompetitor.get_events_entered/get_partners/get_gear_sharing`, `ProCompetitor` equivalents, `Event.get_payouts()`, `Heat.get_competitors/get_stand_assignments()`.

## Prevention
- Any new JSON TEXT column gets a getter with a JSONDecodeError guard. Never call `json.loads` on the raw attribute in route code.
- Consider logging (not raising) when the guard fires, so corrupt rows can be found and repaired.
