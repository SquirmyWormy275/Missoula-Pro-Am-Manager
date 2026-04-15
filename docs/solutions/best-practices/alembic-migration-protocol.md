---
module: migrations
date: 2026-04-15
problem_type: best_practice
component: development_workflow
severity: critical
tags:
  - "alembic"
  - "flask-migrate"
  - "migrations"
  - "schema"
---

# Mandatory Migration Review Protocol

## Context
Auto-generated Alembic migrations have repeatedly introduced silent schema drift: unintended `nullable` flips, dropped indexes, altered defaults on columns unrelated to the intended change. These bugs took hours to diagnose and caused a multi-week production schemaless incident (2026-04-08).

## Pattern
Every migration touching models follows this sequence:

1. **Before generating:** Only modify columns you intend to change. Run `flask db check` — if Alembic detects changes you didn't make, investigate drift first; do NOT bundle it into the new migration.
2. **Generate:** `flask db migrate -m "descriptive_name"`.
3. **Review line-by-line (never skip):** Open the generated file in `migrations/versions/` and verify every line of `upgrade()` and `downgrade()`:
   - Only intended columns appear — delete unexpected `alter_column` lines.
   - `nullable` matches the model.
   - `server_default` matches the model (Booleans need `sa.text('false')`, not `'0'`).
   - No `drop_index` unless intentional.
   - No type changes unless intentional.
   - `down_revision` points to the current HEAD.
4. **Apply and verify:**
   - `flask db upgrade`
   - `pytest tests/test_migration_integrity.py -v`
   - `pytest tests/test_pg_migration_safety.py -v`
5. **Update MEMORY.md Migration Chain** with the new head.

## Rationale
Auto-generated ≠ correct. Alembic's autogenerate uses model introspection that misses `server_default`, misreads `nullable` for batch ops, and sometimes emits spurious `alter_column` lines. Review is the only defense.

## Examples
**Never do:**
- Commit a migration you haven't read line-by-line.
- Bundle drift fixes from a prior session into a new feature migration.
- Use `_add_column_if_missing()` or idempotent hacks — if a column is missing, the original migration was wrong; fix at source.
- Alter `nullable` or `server_default` on an existing column unless that IS the migration's purpose.

**Always do:**
- Separate migrations for separate concerns.
- Run both integrity test suites before pushing.
- Dual-dialect test (SQLite + PG) via CI's `postgres-smoke` job.
