---
module: migrations
date: 2026-04-15
problem_type: database_issue
component: database
severity: medium
root_cause: config_error
resolution_type: migration
symptoms:
  - "KNOWN_NULLABLE_DRIFT contains 40 column entries"
  - "KNOWN_SERVER_DEFAULT_DRIFT contains 16 column entries"
  - "Model declares nullable=False but migration produces nullable=True"
  - "Model has default= but no server_default in DB"
tags:
  - "alembic"
  - "migrations"
  - "tech-debt"
  - "schema"
---

# Migration drift allowlists (Tech Debt #8) — retire incrementally

## Problem
`tests/test_migration_integrity.py` maintains two allowlists to keep CI green while long-standing model/migration drift gets cleaned up:

- `KNOWN_NULLABLE_DRIFT` — 40 columns where the model says `nullable=False` but the migration-produced schema has `nullable=True` (or vice versa).
- `KNOWN_SERVER_DEFAULT_DRIFT` — 16 columns where the model has a `default=` value but no matching `server_default` was written into any migration.

These were introduced in PR #7 (V2.7.0) to unblock CI. Every entry is a latent production bug — raw SQL inserts, migrations, and PG all bypass Python-side defaults.

## Root Cause
Historical migrations were auto-generated without the line-by-line review protocol (see `alembic-migration-protocol.md`). Columns with implicit `nullable=True` were baked into the production schema before anyone noticed. `TestModelColumnDeclarations` in `test_migration_integrity.py` now catches new violations, but existing ones accumulated before the test existed.

## Solution
Each allowlist entry needs a targeted fix-up migration:

1. Pick one or a handful of related entries.
2. Write a hand-crafted Alembic migration that alters the column to match the model (typically `op.alter_column(..., nullable=False, server_default=sa.text('false'))`).
3. Follow the full Migration Review Protocol (no `batch_alter_table` on PG; Boolean `server_default='false'` not `'0'`).
4. Delete the entry from the allowlist in the same commit.
5. Run `pytest tests/test_migration_integrity.py tests/test_pg_migration_safety.py -v`.

**Precedent:** V2.8.0 Phase 1B migration `f0a1b2c3d4e6` retired 3 `KNOWN_NULLABLE_DRIFT` entries — `event_results.points_awarded`, `college_competitors.individual_points`, `teams.total_points` — while converting them from Integer to Numeric. Remaining: 40 nullable + 16 server_default entries.

## Prevention
- Never add to either allowlist. If a new drift is found, fix it at source in the same PR that introduced it.
- The Migration Review Protocol (Step 3.2 and 3.3) and `TestModelColumnDeclarations` together prevent new drift from being committed.
- Prioritize Boolean and status columns — these are the most dangerous because `NULL` on a Boolean typically evaluates falsy and silently corrupts logic.
