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

## Before Merging to `main`

- CI is green
- PR description includes validation commands
- Any operator-facing change has docs or UI notes updated
- If touching deploy/runtime/config:
  - confirm `railway.toml` changes are intentional
  - confirm env var expectations are documented
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
```

Plus subsystem-specific tests for the changed area.

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

## Race-Day Hotfix Rules

- No mixed-scope PRs
- No opportunistic refactors in a hotfix
- No schema change without explicit rollback steps
- Prefer fixing production issues behind the smallest safe diff

## Release Owner Sign-Off

- [ ] CI green
- [ ] PostgreSQL smoke green
- [ ] migration safety green
- [ ] rollback path documented
- [ ] deploy verified in Railway logs
- [ ] `/health` verified after deploy
