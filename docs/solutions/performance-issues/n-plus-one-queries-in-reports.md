---
module: api
date: 2026-04-15
problem_type: performance_issue
component: database
severity: medium
root_cause: missing_include
resolution_type: code_fix
symptoms:
  - "Public API /api/public/schedule takes multiple seconds"
  - "Flight builder slow on tournaments with many events"
  - "Per-event .filter_by().all() loop in services"
tags:
  - "sqlalchemy"
  - "performance"
  - "n-plus-one"
---

# N+1 queries on event → heats lookups

## Problem
`routes/api.py` `public_schedule` / `public_results` and `services/flight_builder.py` iterated events and issued a heat query per event. Linear latency growth with event count.

## Root Cause
Lazy per-iteration queries:

```python
for event in events:
    heats = Heat.query.filter_by(event_id=event.id, run_number=1).all()
```

## Solution
Batch with `.in_()` + `defaultdict` grouping:

```python
from collections import defaultdict

event_ids = [e.id for e in events]
all_heats = Heat.query.filter(
    Heat.event_id.in_(event_ids),
    Heat.run_number == 1,
).all()
heats_by_event = defaultdict(list)
for h in all_heats:
    heats_by_event[h.event_id].append(h)

for event in events:
    heats = heats_by_event[event.id]
```

## Prevention
- Any `for x in list: Model.query.filter_by(x_id=x.id)` pattern is an N+1. Rewrite as a single `.in_()` + dict/defaultdict grouping.
- SQLAlchemy relationship `lazy='selectin'` is another option for declared relationships.
- Add query count assertions to smoke tests for hot read paths.
