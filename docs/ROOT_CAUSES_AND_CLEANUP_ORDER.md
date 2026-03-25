# Root Causes And Cleanup Order

This document explains why the project has continued to hit Flask, Railway,
and database failures even after extensive debugging and test expansion.

The short version:

- The failures are not coming from one bug class.
- The repo has had multiple boundary problems at once.
- Many tests are useful, but they mostly defend slices of behavior rather than
  the exact production lifecycle end to end.

## Top 5 Root Causes

### 1. Environment mismatch: local SQLite, test SQLite, Railway PostgreSQL

The app runs against materially different database backends depending on where
it is executed:

- local dev defaults to SQLite in [`config.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/config.py#L10)
- tests commonly build temporary SQLite databases in [`tests/db_test_utils.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/db_test_utils.py#L21)
- Railway deploys against PostgreSQL per [`docs/POSTGRES_MIGRATION.md`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/docs/POSTGRES_MIGRATION.md#L22)

That split matters because SQLite is more permissive than PostgreSQL. This repo
already has explicit regression tests proving that PostgreSQL failures happened
from SQLite-specific migration patterns:

- [`tests/test_pg_migration_safety.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/test_pg_migration_safety.py#L1)

Impact:

- code and migrations can pass locally and still fail on Railway
- schema operations valid on SQLite can break on PostgreSQL
- operational assumptions drift because dev and prod are not exercising the same path

### 2. Startup lifecycle was coupled to schema mutation

The app historically allowed schema upgrade concerns to leak into application
startup. The current comment in [`database.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/database.py#L25)
documents the concrete failure mode:

- Railway `releaseCommand` ran `flask db upgrade`
- the app boot path also tried to manage migrations
- both competed around migration timing/locking

Even though that startup behavior has now been removed, this class of issue is
important because it explains why failures looked random during deploy:

- same code, different boot timing
- sometimes schema state was ready
- sometimes the app started against a partially updated database

Impact:

- deployments fail intermittently instead of deterministically
- app health looks like a Flask problem when the real issue is boot order
- debugging is noisy because the failure sits between deploy orchestration and app runtime

### 3. Configuration resolution has been too sensitive to process context

This repo has repeatedly depended on environment variables and working
directory state being exactly right at the right time.

Evidence:

- config defaults are resolved from module-level config in [`config.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/config.py#L38)
- test setup has to set `DATABASE_URL` before `create_app()` specifically to
  prevent the app from touching the wrong database in
  [`tests/db_test_utils.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/db_test_utils.py#L28)
- the helper contains a fatal guard if the app resolves back to the production-style
  SQLite path in
  [`tests/db_test_utils.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/db_test_utils.py#L58)

That is not normal "just config." It means app behavior has been influenced by:

- current working directory
- env var timing
- import timing
- whether config resolved before or after a test override

Impact:

- the same command behaves differently depending on where it is run from
- tests need protective scaffolding to avoid hitting the wrong database
- small execution-context changes create failures that look unrelated to the feature being worked on

### 4. Migration-chain complexity has become a source of its own risk

The repo now carries a meaningful amount of migration-defense infrastructure.
That is useful, but it is also evidence that migration drift has already become
a recurring operational problem.

Evidence:

- schema parity tests compare `flask db upgrade` output against `db.create_all()` in
  [`tests/test_migration_integrity.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/test_migration_integrity.py#L1)
- PostgreSQL migration safety tests statically scan migration files for known bad patterns in
  [`tests/test_pg_migration_safety.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/test_pg_migration_safety.py#L1)
- developer docs explicitly call out prior migration incidents and strict migration protocol in
  [`DEVELOPMENT.md`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/DEVELOPMENT.md)

This means the migration layer is not a passive detail anymore. It is one of
the highest-risk parts of the system.

Impact:

- fixes in models can still leave deploys broken if migrations drift
- merge history and auto-generated revisions can create latent failures
- adding tests helps detect regressions, but does not simplify the underlying migration surface area

### 5. Railway-specific operational constraints are still partially outside the main design

Railway is not just "production hosting." It changes several assumptions:

- production database is PostgreSQL, not SQLite
- release phase and app boot are separate lifecycle stages
- filesystem under `instance/` is ephemeral per
  [`docs/POSTGRES_MIGRATION.md`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/docs/POSTGRES_MIGRATION.md#L225)

The docs already acknowledge that `instance/` files can be lost on deploy in
Railway, including JSON-backed settings/config caches.

Impact:

- behavior that is stable locally becomes transient in Railway
- non-database state can disappear even when the database is healthy
- fixes aimed only at Flask routes or models do not address deployment-state loss

## Why So Many Tests Did Not Eliminate The Problem

The tests are not pointless. They are catching real regressions. The issue is
that the problem set spans different layers:

- config resolution
- migration generation
- migration execution
- app startup
- deploy lifecycle
- backend differences between SQLite and PostgreSQL

Most test files validate one layer well. They do not fully recreate the entire
Railway release-and-boot path against PostgreSQL.

In practice, the test suite has been acting as a defensive perimeter around a
system with several unstable boundaries. That helps, but it does not make those
boundaries disappear.

## Cleanup Order

### 1. Freeze startup responsibilities

Goal:

- app startup only creates the app and opens connections
- startup never performs schema mutation

Primary files:

- [`database.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/database.py)
- [`railway.toml`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/railway.toml)

Reason this is first:

- it removes deploy-time nondeterminism
- without this, every other fix still sits on unstable boot behavior

### 2. Make all local file defaults explicitly repo-rooted or env-only

Goal:

- no runtime behavior depends on current working directory
- every local path default is either absolute-from-project or explicitly provided

Primary files:

- [`config.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/config.py)
- any service writing under `instance/` or `uploads/`

Reason this is second:

- path instability creates false failures during local debugging and test setup
- it also obscures whether a bug is real or just context-sensitive

### 3. Reduce migration risk before adding more features

Goal:

- treat each new migration as a production change, not a generated artifact
- keep the migration chain valid on both SQLite and PostgreSQL

Primary files:

- [`migrations/versions/*`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/migrations/versions)
- [`tests/test_migration_integrity.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/test_migration_integrity.py)
- [`tests/test_pg_migration_safety.py`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/test_pg_migration_safety.py)

Reason this is third:

- migrations are currently one of the highest-value failure reducers
- deploy reliability will remain fragile if schema evolution remains fragile

### 4. Add one true production-like validation path

Goal:

- run at least one validation workflow against PostgreSQL, not only SQLite plus static checks

Examples:

- CI job that boots the app against a temporary PostgreSQL service
- smoke test that runs `flask db upgrade` and basic app startup against PostgreSQL

Reason this is fourth:

- this closes the biggest realism gap in the current test strategy
- static migration scans are useful, but they are not the same as executing on PostgreSQL

### 5. Move deploy-critical state out of ephemeral filesystem assumptions

Goal:

- Railway deploys should not lose meaningful runtime state

Primary areas:

- JSON config/cache files under `instance/`
- any operator data that must survive deploys

Source reference:

- [`docs/POSTGRES_MIGRATION.md`](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/docs/POSTGRES_MIGRATION.md#L225)

Reason this is fifth:

- it is less urgent than startup/migration correctness
- but it will keep causing "production weirdness" until state ownership is clarified

## Bottom Line

The recurring failures are happening because the repo has been crossing too
many boundaries at once:

- schema mutation vs app startup
- SQLite assumptions vs PostgreSQL reality
- local filesystem persistence vs Railway ephemerality
- test harness overrides vs runtime defaults

The test suite is large because the system has needed a lot of defensive
coverage. That does not mean the failures are surprising. It means the repo has
been paying interest on unresolved environment and lifecycle complexity.
