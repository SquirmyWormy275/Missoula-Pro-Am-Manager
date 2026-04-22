---
title: Fingerprint-based staleness tracking for aggregation surfaces
module: services/print_catalog
date: 2026-04-22
problem_type: best_practice
component: service_object
tags:
  - architectural-pattern
  - aggregation
  - staleness
  - decorator
  - cross-cutting-concern
---

# Fingerprint-based staleness tracking for aggregation surfaces

## Context

The Print Hub feature needed a single page showing every printable document in the
app, with a status indicator telling the judge whether each document's data had
changed since the last time it was printed. The obvious approach — compare
`max(updated_at)` of the underlying rows to `last_printed_at` — failed the first
design check: most domain models in this app (`Heat`, `Event`, `ProCompetitor`,
`EventResult`, etc.) do NOT have `updated_at` columns. Only `Tournament` and
`User` do.

Adding `updated_at` to every domain model would be a 10+ migration refactor with
real schema-drift risk (the project already has 58 open drift entries in its
tracker). The payoff for this feature alone would not justify that work.

## Guidance

When an aggregation surface needs per-row staleness detection across many domain
objects that lack `updated_at` columns, use **fingerprint-based tracking** instead
of timestamp comparison:

1. Add ONE cross-cutting table keyed by `(tournament_id, doc_key, entity_id)`.
   Store `last_printed_at`, `last_printed_fingerprint`, and `last_printed_by_user_id`.
2. For each document in the catalog, write a `fingerprint_fn(tournament, entity)`
   that returns a short `sha1` over all data the print route actually renders.
   Include every column that changes the output — counts, statuses, foreign keys,
   JSON fields.
3. Instrument existing print routes with a decorator that runs the fingerprint
   function post-response and upserts the tracker row. The decorator must be
   body-transparent: it reads the response but does not mutate it.
4. On the Hub page, recompute the current fingerprint per row and compare to the
   stored one. Mismatch → STALE.

### Why `sha1`, not `hash()`

Python's built-in `hash()` is NOT stable across processes — `PYTHONHASHSEED`
randomizes it per interpreter start. Stored fingerprints would mis-compare after
every Railway redeploy. `hashlib.sha1(payload.encode()).hexdigest()[:16]` is
deterministic forever and fits comfortably in a 64-char column.

### Why a decorator, not inline writes

Instrumenting 15 existing print routes with inline `PrintTracker.upsert(...)` calls
is a DRY violation waiting to happen — one missed route silently drops the
staleness signal. A decorator collapses the instrumentation to one line per route
and guarantees the write is never skipped on success. Decorator rules:

1. Tracker update runs AFTER the view returns — if the view raises, no tracker
   row is written.
2. Tracker failures are SWALLOWED and logged — an audit bookkeeping error must
   never break the print response.
3. The view function's response body, content-type, and headers are unchanged.

### Fail-safe direction

Fingerprints are coarser than timestamps. A fingerprint that shifts when the
printed output hasn't actually changed will show a false-positive STALE badge and
drive an unnecessary reprint. That's strictly better than the inverse — a judge
trusting a stale printed sheet on race day. Fail on the paper side.

## Why This Matters

This pattern sidesteps schema refactors that would cost weeks of migration work
for a feature that only needs a per-row change signal. It also isolates the
staleness concern to one table + one service module; domain models stay focused
on their actual purpose.

Key property: **adding a new document to the catalog is one entry in a Python
list + one `status_fn` + one `fingerprint_fn` + one decorator line on the
existing print route.** No schema change, no migration, no touching the domain
models.

## When to Apply

Use this pattern when ALL of the following hold:

- You need per-row staleness / change detection across many heterogeneous
  sources.
- The underlying models do NOT have `updated_at` columns AND adding them is
  expensive or risky.
- False-positive staleness is acceptable (user reprints unnecessarily) but
  false-negative freshness is not (user trusts stale data).
- The catalog of tracked things is small (< ~50) and relatively stable — a
  hardcoded Python list is fine; you don't need a DB-driven catalog.

Do NOT use this pattern when:

- Domain models already have reliable `updated_at` columns; just compare
  timestamps.
- The aggregation surface needs true real-time reactivity (staleness must be
  detected within milliseconds — use a proper change-data-capture feed).
- The set of tracked items is huge or dynamic and the per-request fingerprint
  computation cost would matter.

## Examples

### Catalog entry (`services/print_catalog.py`)

```python
PrintDoc(
    key='heat_sheets',
    label='Heat Sheets',
    section=SECTION_RUN_SHOW,
    route_endpoint='scheduling.heat_sheets',
    status_fn=_status_heat_sheets,
    fingerprint_fn=_fp_heat_sheets,
    description='Master heat sheet print page (per-flight tabs).',
),
```

### Fingerprint function — must include EVERY rendered field

```python
def _fp_heat_sheets(tournament, entity=None):
    heats = Heat.query.join(Event, Heat.event_id == Event.id) \
        .filter(Event.tournament_id == tournament.id) \
        .order_by(Heat.id).all()
    scratched = ProCompetitor.query.filter_by(
        tournament_id=tournament.id, status='scratched'
    ).count()
    parts = [
        tournament.updated_at.isoformat() if tournament.updated_at else '',
        f'scratched={scratched}',
    ]
    for h in heats:
        parts.append(
            f'{h.id}:{h.status}:{h.flight_id}:'
            f'{h.competitors or ""}:{h.stand_assignments or ""}'
        )
    return _sha1(parts)
```

Design rule: the fingerprint must reflect everything the print template reads.
Miss `Heat.flight_id` and the Hub will lie about freshness after a flight rebuild.
A parametrized test per doc verifies that a change to each rendered field shifts
the fingerprint.

### Transparent decorator

```python
def record_print(doc_key: str, entity_id_kwarg: Optional[str] = None):
    def wrap(view):
        @functools.wraps(view)
        def inner(*args, **kwargs):
            response = view(*args, **kwargs)   # runs first
            try:
                _write_tracker_from_request(doc_key, entity_id_kwarg, kwargs)
            except Exception:
                logger.exception('PrintTracker upsert failed (non-fatal)')
            return response                    # response is untouched
        return inner
    return wrap
```

### Use on an existing route (one-line addition)

```python
@scheduling_bp.route('/<int:tournament_id>/heat-sheets')
@record_print('heat_sheets')
def heat_sheets(tournament_id):
    ...  # unchanged
```

### Dynamic (per-entity) rows

For "one row per event" rows like per-event results, pass `entity_id_kwarg`:

```python
@reporting_bp.route('/<int:tournament_id>/event/<int:event_id>/results/print')
@record_print('event_results', entity_id_kwarg='event_id')
def event_results_print(tournament_id, event_id):
    ...
```

Storage: `PrintTracker.entity_id` is nullable. Fixed docs store NULL; dynamic
docs store the entity PK. UNIQUE `(tournament_id, doc_key, entity_id)` lets fixed
and dynamic rows coexist in the same table.

### Table shape

```sql
CREATE TABLE print_trackers (
  id INTEGER PRIMARY KEY,
  tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  doc_key VARCHAR(64) NOT NULL,
  entity_id INTEGER NULL,                           -- NULL for fixed docs
  last_printed_at DATETIME NOT NULL,
  last_printed_fingerprint VARCHAR(64) NOT NULL,
  last_printed_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
  UNIQUE (tournament_id, doc_key, entity_id)
);
```

`entity_id` is deliberately NOT a foreign key because dynamic docs may reference
different entity tables (today events; potentially heats, competitors later). A
real FK would force a discriminator column. Orphaned rows after entity deletion
are acceptable historical bookkeeping — they get cleaned up when the tournament
is deleted (CASCADE).

## Related docs

- [explicit-nullable-and-server-default.md](explicit-nullable-and-server-default.md) —
  the `server_default` parity rule caught a genuine drift bug in the
  `print_email_logs` model while building this feature (migration had
  `server_default="[]"` and `server_default="queued"`; model had only
  `default=`, not `server_default=`).
- [non-blocking-external-integrations.md](../architecture-decisions/non-blocking-external-integrations.md) —
  the same non-blocking principle applied to the SMTP delivery path attached to
  this feature: queueing + background_jobs + log-and-continue for failures so a
  mail server outage never freezes the Hub UI.

## When this breaks

If you add a new column to a domain model that's rendered by an existing print
template but forget to include it in that doc's `fingerprint_fn`, the Hub will
falsely report FRESH after a data change. Mitigation: one parametrized test per
doc that mutates each field the template reads and asserts the fingerprint
shifts. Without this test, drift is invisible until a judge notices the stale
badge never triggers.
