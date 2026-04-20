# Rollback SOP

Use this when a merge or deploy introduces production risk.

This SOP is intentionally conservative. Favor restoring safe service quickly
over clever recovery.

## Classify the Failure First

Choose one:

1. App boot failure
2. Migration failure before traffic flip
3. Runtime regression after successful deploy
4. Data integrity issue discovered after operator actions

## 1. App Boot Failure

Symptoms:

- Railway deploy completes migration step but app never becomes healthy
- `/health` fails or root page fails after deploy

Response:

1. Identify the bad merge commit on `main`
2. Revert it with a dedicated rollback PR
3. Merge the rollback PR
4. Watch Railway redeploy
5. Re-run deploy verification from `docs/RELEASE_CHECKLIST.md`

Notes:

- Prefer `git revert`, not history rewriting
- Keep rollback PR limited to the bad merge

## 2. Migration Failure Before Traffic Flip

Symptoms:

- `flask db upgrade` fails in Railway pre-deploy
- new app version never becomes active

Response:

1. Stop and read the exact failing migration revision
2. Do not push unrelated fixes
3. If failure is code-only in the migration:
   - prepare a narrow migration fix PR
   - validate with `tests/test_pg_migration_safety.py`
   - validate against PostgreSQL locally or CI
4. If failure is caused by unexpected production data:
   - write a repair migration or guarded backfill
   - document the data assumption in the PR

Notes:

- Because Railway runs the migration before traffic flip, production traffic
  should still be on the previous good app version.
- Do not manually alter production rows unless the migration path is fully
  understood and documented.

## 3. Runtime Regression After Successful Deploy

Symptoms:

- deploy succeeded
- judges or operators report broken workflow after deploy

Response:

1. Confirm scope:
   - scoring
   - scheduling
   - reporting/export
   - registration/import
2. Check:
   - `/health`
   - ops dashboard
   - recent audit logs
   - recent background job failures
3. If safe hotfix is obvious:
   - create a narrow hotfix PR
   - run targeted tests only for affected area plus lint
4. If safe hotfix is not obvious:
   - revert the merge
   - restore service first
   - investigate on a branch

## 4. Data Integrity Issue

Symptoms:

- wrong standings
- duplicate/scarred competitors in results
- payout mismatch
- broken exported reports caused by persisted bad state

Response:

1. Preserve evidence:
   - export affected data
   - capture audit log rows
   - capture screenshots if operator-visible
2. Decide whether to:
   - repair in app code and re-run workflow
   - write a one-off data repair script/migration
   - restore from backup if corruption is broad and recent
3. If restoring from backup:
   - verify schema revision matches current app
   - document lost operator actions since backup
   - communicate before restore

## Production Backup / Restore Notes

- SQLite backup download and restore are app-supported only in SQLite environments
- Cloud/local backup jobs should be visible through the app workflow and ops dashboard
- Never restore a DB artifact with unknown schema provenance

## Required Post-Rollback Actions

- open or update an incident issue
- note exact bad commit SHA
- note exact rollback commit SHA
- record whether schema changed
- add or update regression tests before re-attempting the fix

## Minimum Rollback PR Template

```md
## Summary
Rollback [bad change] after production/runtime regression.

## Reason
[short operational reason]

## Validation
- [ ] app boots
- [ ] /health returns 200
- [ ] affected operator workflow loads

## Follow-up
- root cause issue:
- replacement fix PR:
```
