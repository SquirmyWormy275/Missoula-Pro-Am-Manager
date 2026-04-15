---
module: scoring
date: 2026-04-15
problem_type: logic_error
component: service_object
severity: medium
root_cause: wrong_api
resolution_type: code_fix
symptoms:
  - "500 error when two judges edit same heat simultaneously"
  - "User sees raw IntegrityError traceback"
  - "Stale form submission overwrites newer data silently"
tags:
  - "sqlalchemy"
  - "concurrency"
  - "version-id"
  - "scoring"
---

# Split `StaleDataError` vs `IntegrityError` handling in scoring

## Problem
Concurrent score entry by multiple judges produced either silent overwrites or opaque 500s. A generic `except Exception` caught both SQLAlchemy `StaleDataError` (optimistic-lock version mismatch) and `IntegrityError` (constraint violation) together, masking the distinction.

## Root Cause
`EventResult.version_id` and `Heat.version_id` exist for optimistic locking (`version_id_col=...`). When a stale form submits, SQLAlchemy raises `StaleDataError` — a recoverable concurrency signal. DB-level constraint violations raise `IntegrityError` — a different class of bug.

## Solution
Split the exception handlers in `routes/scoring.py`:

```python
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.exc import IntegrityError

try:
    db.session.commit()
except StaleDataError:
    db.session.rollback()
    flash('Another judge just edited this heat — please reload and retry.', 'warning')
    return redirect(...)
except IntegrityError:
    db.session.rollback()
    flash('Database constraint violation — contact admin.', 'danger')
    return redirect(...)
```

Plus: `Heat.locked_by_user_id` / `locked_at` + `acquire_lock()` / `release_lock()` give judges explicit exclusive access before entry. The Bootstrap 5 conflict modal in `enter_heat.html` shows the StaleDataError message with a reload link.

## Prevention
- Any route writing to a `version_id`-protected model must catch `StaleDataError` separately.
- Never `except Exception: flash(str(e))` — leaks internals AND conflates concurrency/bug/env errors.
