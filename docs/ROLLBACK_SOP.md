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

### STRATHMARK shadow-specific containment

If the STRATHMARK shadow integration is unhealthy, disable new shadow
preparation/calculation without rewriting issued receipts or official results.
Keep receipt lookup, status, export, and durable settlement recovery available
when their local evidence remains valid. Do not delete append-only outcome,
context, receipt, or outbox rows as a rollback technique.

- A mirror outage alone is not a reason to revoke an issued local receipt.
- Missing/stale/corrupt offline evidence blocks new calculations, not exact old
  receipt replay.
- If a numeric settlement was wrong, append a correction or void; never edit the
  earlier evidence row.
- Reverting Missoula code does not roll back the separately versioned
  STRATHMARK ledger or its PostgreSQL mirror migrations.
- Do not fall back silently to the legacy official mark writer. Any authority
  change is an explicit show-director decision outside the shadow workflow.

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
   - use the provider recovery path below; the application and backup workflow
     are not production restore tools

## Recovery Boundaries

Keep these three operations separate:

1. **Application rollback** reverts application code with a PR and redeploy. It
   does not reverse committed database rows or automatically downgrade schema.
2. **Provider recovery** restores a Railway-managed backup, snapshot, or
   point-in-time recovery target through an owner-approved incident procedure.
   This is the only production database recovery path documented here.
3. **Disposable verification** decrypts and restores an exported artifact into
   a new local or CI PostgreSQL database for proof. It never targets Railway,
   an externally resolved host, or a database named for production.

The application must not perform an in-place PostgreSQL production restore.
The daily backup workflow must not receive production restore credentials. It
may read a dump through the dedicated read-only role, but it restores only into
the runner-local PostgreSQL service before encrypting the artifact.

SQLite backup and staged restore operations apply only to explicitly supported
SQLite environments. They are not a substitute for PostgreSQL recovery proof.

### SQLite Offline Restore Contract

The web route may validate and stage a SQLite restore package, but it must not
replace the live database. Applying a staged package is an offline maintenance
operation: stop every app process, drain queued/running jobs, and acquire the
exclusive database activity fence before proceeding.

Restore package version 2 fails closed unless the upload has all of the
following:

- a clean SQLite integrity and foreign-key check
- the current Alembic revision
- the exact current application table set, excluding only SQLite's internal
  tables, with the same canonical columns, types, nullability, defaults,
  primary keys, foreign keys, and indexes for every table
- the same deterministic identity fingerprint for the complete sorted set of
  tournament `(id, name, year)` rows, not only the selected tournament
- the staged manifest checksum

The selected tournament and complete tournament-set fingerprint establish
identity continuity with the current database; the restore still replaces the
whole SQLite database. A mismatch in any other tournament blocks the restore.
These checks do **not** prove backup provenance or authenticity. Package v2
does not consume a backup-issued signed manifest or an owner-held signature.
Until that external evidence exists, an operator must independently verify the
source backup and its recorded digest before staging it; do not describe the
package as cryptographically provenance-verified.

Before replacing the main database file, the maintenance process creates and
verifies a SQLite online safety snapshot. While the exclusive fence is held, it
moves any target `-wal` and `-shm` files into the package's
`sidecar-quarantine` directory before replacing either the live main file or a
rollback main file. Every sidecar move intent, destination name, and source
hash is fsynced to the external restore journal and manifest before the move.
A later invocation reconciles a move that completed before its final manifest
update, then reconciles the interrupted `applying` package before opening the
target database. It either validates a fully replaced target or restores the
safety snapshot. Quarantined sidecars must never be moved back beside a
restored main file.

The staged database, safety snapshot, external journal, manifest, and
quarantined sidecars can contain production-equivalent personal data. Protect
the entire `.restore-staging` tree as backup material. The maintenance tooling
sets staging and quarantine directories to `0700` and database, journal, and
manifest files to `0600` on filesystems that support those modes. Platform
access controls remain mandatory where POSIX modes are unavailable. Remove the
tree only under the approved evidence-retention procedure.

## Hosted Backup Contract

The scheduled workflow fails closed unless all three are configured:

- `RAILWAY_PG_READONLY_DUMP_URL`, a dedicated dump role URL
- `BACKUP_AGE_RECIPIENT`, the approved `age` public recipient repository variable
- `BACKUP_AGE_RECIPIENT_SHA256`, the reviewed SHA-256 of the exact recipient
  string, stored as a separate repository variable

Before `pg_dump`, automation verifies that the connected role is not a
superuser, cannot create roles or databases, defaults to read-only transactions,
and has no detected table or sequence write privileges. The production URL is
available only to that check and the dump step.

The dump is restored into `127.0.0.1:5432/proam_restore_verify`. Required
tables, Alembic metadata, and aggregate relationship checks must pass. The
runner-local database password is scoped only to the trusted restore,
verification, and database-removal shell steps. `pg_restore` diagnostics are
captured without being printed and securely deleted on both success and
failure. The workflow then encrypts the verified dump to the approved
recipient only after its configured value matches the pinned fingerprint,
drops the runner-local plaintext database, removes the plaintext
dump unconditionally, and only then invokes the commit-pinned official upload
action with the `.age` file. Missing configuration, failed restore, failed
verification, failed encryption, failed database removal, or failed plaintext
cleanup produces no artifact upload.

Workflow logs and summaries must not print database rows. Evidence may include
file sizes, SHA-256 digests, generic pass/fail results, the workflow run URL,
and the exact Git commit.

## Disposable PostgreSQL Restore Rehearsal

The recovery private key is held separately from GitHub Actions. Do not add it
to repository secrets or the application environment. A designated recovery
operator performs the decryption rehearsal on an approved workstation:

1. Record the workflow run URL, commit SHA, ciphertext SHA-256, and operator.
2. Download the `.dump.age` artifact and verify its ciphertext SHA-256 against
   the workflow summary.
3. Set `PROAM_BACKUP_AGE_IDENTITY` to the approved private identity file path.
   Stop if the path is missing, unapproved, or repository-owned.
4. Decrypt with `age --decrypt --identity "$PROAM_BACKUP_AGE_IDENTITY"` to a
   temporary local file. Confirm its SHA-256 matches the plaintext digest in
   the workflow summary.
5. Guard the restore target before any destructive command: host must be
   `127.0.0.1`, database must be a newly created disposable rehearsal database,
   and neither the host nor database may reference Railway or production.
6. Restore with `pg_restore --exit-on-error --single-transaction --no-owner
   --no-acl` and run the same required-table, Alembic, and relationship checks.
7. Record pass/fail without capturing row output. Delete the decrypted dump and
   drop the disposable database after evidence is recorded.

Successful hosted encryption and a matching recipient fingerprint are not
decryption proof. Release evidence must say whether the separately held
identity rehearsal was actually executed against the retained artifact.

## Provider Production Recovery

Production recovery requires an incident-specific plan reviewed by the release
owner and database owner. Before any provider restore:

1. Freeze application writes and preserve audit and deployment evidence.
2. Identify the provider recovery source and a new recovery target. Do not
   overwrite the active database as the first operation.
3. Record current and target schema revisions and the operator actions that may
   be lost. Do not invent an RPO or RTO.
4. Restore through provider tooling into the reviewed target, validate it, and
   switch application connectivity only after explicit approval.
5. Keep the old target available until post-recovery checks and reconciliation
   complete.

Never restore a database artifact with unknown provenance, an unverified
checksum, or a schema revision that has not been reviewed.

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
