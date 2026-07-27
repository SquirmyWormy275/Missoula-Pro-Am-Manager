---
module: migrations
date: 2026-04-15
problem_type: database_issue
component: database
severity: critical
root_cause: wrong_api
resolution_type: migration
symptoms:
  - "flask db upgrade fails on Railway PostgreSQL"
  - "500 errors on every route after deploy"
  - "PG error: column type 0 does not exist (from boolean server_default='0')"
  - "PRAGMA table_info syntax error on PostgreSQL"
  - "batch_alter_table fails on PG tables with FKs or indexes"
tags:
  - "postgres"
  - "sqlite"
  - "alembic"
  - "flask-migrate"
  - "railway"
---

# SQLite-specific migration patterns break PostgreSQL deploys

## Problem
Migrations authored against SQLite (dev) repeatedly broke on Railway PostgreSQL (prod). Failures included: `batch_alter_table` crashing on tables with FKs/indexes, boolean `server_default='0'` rejected, `PRAGMA table_info()` throwing syntax errors, `SET col = 0` failing for boolean columns. Each failure caused 500 errors on all routes querying the affected table.

## Root Cause
SQLite is permissive and monolingual; PostgreSQL is strict. Alembic auto-generation tends to emit SQLite-flavored SQL (especially `batch_alter_table` under `render_as_batch=True`). Developers ran migrations locally against SQLite and they "passed," hiding PG incompatibilities until deploy.

## Solution
Banned patterns (enforced by `tests/test_pg_migration_safety.py`):

| Banned | Use Instead |
|---|---|
| `batch_alter_table` | `op.add_column()`, `op.create_index()`, `op.drop_index()` directly |
| `server_default='0'` on Boolean | `server_default='false'` or `sa.text('false')` |
| `server_default=sa.text('0')` on Boolean | `server_default=sa.text('false')` |
| `PRAGMA table_info(...)` | `information_schema.columns` query (dialect-portable; see `e9f0a1b2c3d4`) |
| `SET col = 0` in `op.execute()` on Boolean | `SET col = false` |
| `ALTER TABLE ... RENAME` via batch | `op.alter_column()` directly |

## Prevention
- Run `pytest tests/test_pg_migration_safety.py -v` before committing any migration. The test scans migration files for banned patterns and fails in seconds.
- CI has a `postgres-smoke` job that runs `flask db upgrade` against PG 15 — watch for failures here.
- Per `docs/POSTGRES_MIGRATION.md`: production runs on PostgreSQL. SQLite is dev-only. Every migration must work on both.
- When hand-writing dual-dialect SQL, use `context.get_bind().dialect.name` to branch.
