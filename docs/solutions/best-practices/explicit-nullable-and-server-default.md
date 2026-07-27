---
module: models
date: 2026-04-15
problem_type: best_practice
component: database
severity: high
tags:
  - "sqlalchemy"
  - "models"
  - "alembic"
  - "schema"
---

# Explicit `nullable` + `server_default` on every column

## Context
Omitting `nullable` defaults to `nullable=True`. Alembic then bakes `nullable=True` into auto-generated migrations — even for columns that should clearly be NOT NULL (Booleans with `default=False` being the worst offender). Similarly, `default=` is Python-side only — raw SQL inserts, migrations, and PostgreSQL bypass it entirely; only `server_default=` is visible to the database. This ambiguity is the upstream source of most migration drift in this codebase.

## Pattern
Every `db.Column()` call in `models/*.py` MUST declare `nullable` explicitly, and any column with a non-NULL default MUST have `server_default`:

```python
# CORRECT
is_active = db.Column(db.Boolean, nullable=False, default=False, server_default=sa.text('false'))
status    = db.Column(db.String(20), nullable=False, default='pending', server_default='pending')
notes     = db.Column(db.Text, nullable=True)  # intentionally nullable

# WRONG
is_active = db.Column(db.Boolean, default=False)        # implicit nullable=True
status    = db.Column(db.String(20), default='pending') # server can't see default
```

Rules:
1. Always declare `nullable=True` or `nullable=False` — never rely on the implicit default.
2. Add `server_default` alongside `default` for any column with a non-NULL default.
3. FK columns may be `nullable=True` — but declare it explicitly.
4. `TestModelColumnDeclarations` in `test_migration_integrity.py` flags violations.

## Rationale
- `nullable` ambiguity produces drift — the model and DB disagree, and Alembic can't distinguish intent from accident.
- `server_default` is what Alembic sees and what the DB enforces. Without it, migrations apply with `NULL` for the new column on existing rows.
- PostgreSQL Boolean `server_default` must be `sa.text('false')` (not `'0'` or `sa.text('0')`).

## Examples
See `models/event.py`, `models/competitor.py` for the correct pattern across dozens of columns. The project tracks 40+ remaining drift entries in `KNOWN_NULLABLE_DRIFT` and `KNOWN_SERVER_DEFAULT_DRIFT` allowlists (Open Tech Debt #8) that need retro-migrations to retire — don't add to the list; fix at source.
