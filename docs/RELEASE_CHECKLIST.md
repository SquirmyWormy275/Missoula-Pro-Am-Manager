# Release Checklist

Canonical pre-merge and pre-deploy checklist for `main`.

This file is the source of truth for race-day releases. If another doc says
something different, this one wins.

## Required GitHub Controls

These are GitHub settings, not repo code:

- Protect `main`
- Require PR review before merge
- Require status checks before merge
- Required checks should include:
  - `test`
  - `postgres-smoke`
  - `unit-postgres`
  - `migration-safety`
  - `lint`
  - `pip-audit`
- Disable direct pushes to `main`

## Before Opening a PR

- Branch from current `main`
- Keep the PR scope narrow
- If schema changes are included:
  - add or update Alembic migration(s)
  - run migration integrity checks
  - run PostgreSQL migration safety checks
  - confirm rollback plan exists
- For a STRATHMARK shadow change:
  - confirm the reviewed STRATHMARK contract commit is published and pinned
  - run `tests/test_shadow_release_readiness.py`
  - run `python scripts/verify_strathmark_shadow_contract.py`
  - confirm it reports a migrated disposable Missoula database and exact
    Missoula-built calculate/numeric payloads, not contract-example traffic
  - confirm shadow authority remains recommendation-only
  - confirm official mark fields are unchanged by the rehearsal

## Before Merging to `main`

- CI is green
- PR description includes validation commands
- Any operator-facing change has docs or UI notes updated
- If touching deploy/runtime/config:
  - confirm `railway.toml` changes are intentional
  - confirm env var expectations are documented
- If touching hosted backup/recovery:
  - confirm `RAILWAY_PG_READONLY_DUMP_URL` belongs to the dedicated
    non-superuser, read-only dump role
  - confirm `BACKUP_AGE_RECIPIENT` is the owner-approved public recipient
  - confirm the recovery private identity is held separately from GitHub,
    Railway, the repository, and the application environment
- If touching scoring/scheduling/reporting:
  - run targeted tests for the affected subsystem

## Required Local Validation

Run from repo root:

```powershell
ruff check .
python -m py_compile app.py
python -m pytest tests/test_postgres_runtime_smoke.py -q
python -m pytest tests/test_pg_migration_safety.py -q
python -m pytest tests/test_migration_integrity.py::TestMigrationIntegrity -q
python -m pytest tests/test_migration_integrity.py::test_shadow_schema_repair_upgrades_an_existing_b7_database -q
python -m pytest tests/test_infrastructure.py tests/test_reporting_export.py -q
python -m pytest tests/test_daily_backup_workflow.py -q
$env:PROAM_UNIT_PG = "1"
python -m pytest tests/test_migration_integrity.py::test_shadow_schema_repair_round_trip_on_populated_b7_postgresql tests/test_shadow_settlement_workflow.py::test_postgres_workers_skip_a_locked_delivery_instead_of_double_sending -q
```

The final command is fail-closed: it must connect to the isolated unit-test
PostgreSQL service, assert the PostgreSQL dialect, create and remove a uniquely
named disposable database, exercise populated b7 upgrade/downgrade/re-upgrade,
and execute the concurrency proof. An unreachable server is a failure, not an
accepted skip. Never point the `PROAM_UNIT_PG_*` variables at a production or
shared database.

Plus subsystem-specific tests for the changed area.

For a service-worker/offline-scoring change, also verify on a demo database:

1. `/sw.js` returns JavaScript with `Cache-Control: no-cache`
2. `/static/offline.html` returns the self-contained operator fallback
3. a successful, non-redirected heat-entry page is available by its exact URL after the local server is stopped
4. an uncached heat-entry URL shows the offline fallback rather than a browser network-error page
5. queued POST evidence stays in the same browser profile and is reconciled through Offline Operations after reconnect

## Hosted Backup Restore Proof

The 2027 race-weekend dates are not yet configured. Do not add a date-specific
schedule or claim race-weekend coverage until the owner approves the dates.

Before merging a backup workflow change:

1. Static workflow tests pass and confirm that production credentials occur
   only in the role-verification and dump steps.
2. A hosted run on the exact branch head verifies the dump role, creates the
   dump without row output, restores it only to the runner-local PostgreSQL
   service, suppresses and deletes `pg_restore` diagnostics, and passes
   required-table, Alembic, and aggregate checks.
3. The run uploads one `.age` ciphertext artifact and no plaintext dump. A
   missing recipient, failed encryption, failed runner-local database removal,
   or failed plaintext cleanup must produce no upload. The runner-local restore
   must be dropped before the commit-pinned upload action executes.
4. Evidence records the run URL, exact commit SHA, plaintext and ciphertext
   SHA-256 digests, local restore result, and cleanup result.
5. A designated recovery operator separately decrypts an artifact with the
   approved private identity path and restores it into a disposable PostgreSQL
   database according to `docs/ROLLBACK_SOP.md`.

Do not mark hosted restore proof complete from static tests alone. Do not mark
key custody or decryption proof complete until the separately held identity has
been rehearsed by its owner.

## Deploy Verification

After merge to `main` and Railway deploy start:

1. Confirm Railway runs `preDeployCommand = "flask db upgrade"`
2. Confirm deploy logs show migration output
3. Confirm app boot completes without config/runtime crash
4. Confirm health check:
   - `GET /health` returns `200`
   - `db` is `true`
5. Confirm one authenticated judge page loads
6. Confirm one public spectator/API page loads

For a deliberately enabled STRATHMARK shadow deployment, also confirm:

1. the service reports durable single-writer readiness and current offline evidence
2. a new request performs receipt lookup before calculate
3. restart recovery returns the identical receipt core
4. the whole-field export is checksummed and reports `importable: false`
5. cloud mirror failure is advisory after local persistence
6. numeric settlement/void stays separate from official scoring
7. a bounded delivery command reports `status=complete`, or exits `2` with an
   explicit `remaining_eligible` count before the next supervised batch
8. a blocked delivery principal produces no remote call and does not roll back
   official finalization

## Race-Day Hotfix Rules

- No mixed-scope PRs
- No opportunistic refactors in a hotfix
- No schema change without explicit rollback steps
- Prefer fixing production issues behind the smallest safe diff

## Release Owner Sign-Off

- [ ] CI green
- [ ] PostgreSQL smoke green
- [ ] migration safety green
- [ ] hosted backup restored in runner-local PostgreSQL on the exact head
- [ ] encrypted artifact contains ciphertext only
- [ ] separately held recovery identity rehearsal recorded, or release blocked
- [ ] rollback path documented
- [ ] deploy verified in Railway logs
- [ ] `/health` verified after deploy
