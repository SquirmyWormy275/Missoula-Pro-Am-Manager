---
title: Railway + Postgres Operational Playbook (Missoula Pro-Am Manager)
date: 2026-04-21
category: best-practices
module: deployment/railway
problem_type: best_practice
component: tooling
severity: critical
related_components:
  - database
  - development_workflow
  - testing_framework
applies_when:
  - Deploying a Flask + SQLAlchemy app to Railway with managed Postgres
  - Configuring Railway release or pre-deploy migration hooks
  - Designing CI that must reflect production DB state, not a fresh container
  - Recovering from a Railway volume detach or silent schemaless production
  - Wiring health endpoints and backup strategy before a production event
tags:
  - railway
  - postgres
  - migrations
  - operational-playbook
  - ci-prod-parity
  - backups
  - incident-response
  - health-monitoring
---

# Railway + Postgres Operational Playbook (Missoula Pro-Am Manager)

## Context

This project is a solo-developer Flask + Postgres application deployed on Railway (Hobby tier), approaching its first real production event (Missoula Pro-Am, race day 2026-04-25). Over ~49 sessions of incremental development, the platform accumulated a long tail of Railway-specific operational gotchas: silent `releaseCommand` failures, unexplained Postgres volume detaches, CI that stayed green while production ran schemaless for 14 days, non-existent backups, no monitoring, and a CLI auth surface that partially works but fails opaquely on account-level operations.

Two incidents landed inside the race-day-week preparation window:

1. **Undetected 2-week schemaless production DB** (2026-03-24 → 2026-04-08). `releaseCommand = "flask db upgrade"` was configured in `railway.toml` but never produced any visible log output on Railway. Production ran against an empty Postgres-HB8_ volume with no schema, returning HTTP 500 on `/` and `status: degraded` on `/health`. Nobody noticed.
2. **11-hour volume-detach outage** (2026-04-08 06:35 UTC). Postgres volume `postgres-volume-XLeL` spontaneously detached from the Postgres service. Flask workers entered a gunicorn SIGKILL crash loop every ~30s, looping 5.5 hours before anyone noticed. A prior identical detach had occurred 14 days earlier (2026-03-24) — never root-caused.

The detach happened **before** competitor data was entered. Had it occurred 17 days later on race-day morning with a full roster, it would have been an event-cancelling disaster with no rollback path. This document distills those incidents into operational guidance for the lone dev (and any future agents assisting) so the same mistakes do not repeat.

## Guidance

**1. The release command is not verified at deploy time.** Railway's build logs do not reliably surface `releaseCommand` output — `flask db upgrade` ran (or didn't) with no stdout, stderr, or release log entry for 2 weeks. Use `preDeployCommand` instead, and always hit `/health` after every deploy to verify `status: ok` and `migration_rev` matches the expected HEAD. See [`../configuration-issues/railway-predeploy-command.md`](../configuration-issues/railway-predeploy-command.md).

**2. Every deploy must be verified by `/health`, not by the absence of a 502.** `/health` returns `status`, `db`, and `migration_rev`. A green deploy with `db: false` or `migration_rev: null` is a failed deploy, not a successful one. Bake this into the checklist:
```bash
curl -s https://missoula-pro-am-manager-production.up.railway.app/health | jq '.status, .db, .migration_rev'
```
Expect `"ok"`, `true`, and the current HEAD from the migration chain in MEMORY.md.

**3. Postgres volumes can detach without warning on Railway Hobby tier.** Twice at this project (2026-03-24 and 2026-04-08), both unexplained. The app enters a gunicorn SIGKILL crash loop every ~30s. Reattachment is **dashboard-UI only** — the Railway MCP tools expose `updateServiceTool` (service-side `volumeMounts`) and `updateVolumeTool` (`sizeMB` only); no programmatic attach primitive exists. Know the reattach UI path before you need it; the first time you learn it should not be mid-outage.

**4. Distinguish HTTP 502 from HTTP 000 when diagnosing prod.** Railway's build + release window produces 60-120 seconds of 502s at the edge — normal, not a crash. HTTP 000 (curl timeout, no response body at all) is a true crash loop. Mis-diagnosing 502-as-crash cost ~30 minutes during the 2026-04-08 incident.
```bash
curl -w "%{http_code}\n" -o /dev/null -s \
  https://missoula-pro-am-manager-production.up.railway.app/health
# 502 during deploy window = wait (60-120s)
# 000 = crash loop, investigate now
# 200 + status:ok = deployed
```

**5. CI passing is not production passing.** `.github/workflows/ci.yml` has 3 jobs (`lint`, `test` on SQLite in-memory, `postgres-smoke` on PG 15 fresh container). All three stayed green while production was schemaless. Each CI run creates a fresh DB and compares nothing to the deployed state. Until a post-deploy smoke step hits production `/health` and fails the workflow on `status != "ok"`, do it manually after every deploy.

**6. Railway project tokens are scope-limited.** The project token can run `railway variables`, `railway run`, and `pg_dump` via the public proxy. It **cannot** create Railway storage buckets (returns "Bad Access" because bucket creation is account-scoped). Generate both token types if you need bucket-level operations, and revoke any token used for a recovery window once the window closes (the 2026-04-08 recovery token was revoked post-event).

**7. Railway `DATABASE_URL` uses `postgres://`; SQLAlchemy 2.x requires `postgresql://`.** `config.py` has `_normalized_database_url()` that rewrites the scheme. Any new service, script, or worker that reads `DATABASE_URL` directly must apply the same rewrite or fail with an opaque dialect-registration error.

**8. Know your public Postgres proxy URL before you need it.** Read it once and keep a local note:
```bash
railway variables --service Postgres-HB8_ | grep DATABASE_PUBLIC_URL
# postgres://postgres:<password>@turntable.proxy.rlwy.net:48640/railway
```
This is the only way to run `flask db upgrade` or `pg_dump` against production from a local terminal. During the 2026-04-08 recovery, finding this URL was on the critical path.

**9. Memorize (or file) the Railway resource identifiers.** Dashboard navigation is slow; copy-paste is fast. Keep these in MEMORY.md:

| Resource | ID |
|---|---|
| Project (STRATHEX Pro-Am) | `a4f717c5-c813-4959-b719-b3e57e302975` |
| Production env | `5e8a13c5-5384-416c-9f60-a9442c81ff24` |
| Postgres-HB8_ service | `961a200c-a66c-48f1-b264-417143340306` |
| App service | `4d6d4ddd-1341-42ff-84b4-9024d4bd4403` |
| Volume mount | `/var/lib/postgresql/data` |
| Postgres version / size | 18.3 / 5GB (~198MB used post-recovery) |

**10. External uptime monitoring is not optional for race-day-week.** Production ran `status: degraded, db: false, migration_rev: null` for 14 days and nobody noticed. Wire an external pinger (UptimeRobot free tier, BetterUptime, or a GitHub Actions cron calling `/health`) that alerts on `status != "ok"` or non-200. Do this before any future deploy that touches migrations.

**11. Backups must be wired to a scheduled caller, not just present as code.** `services/backup.py` exists with S3 upload logic, but the env vars (`BACKUP_S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) are unset and no cron/admin route calls the function. That is not a backup — it is dead code. Railway automatic snapshots require Pro tier; on Hobby, run a manual `pg_dump` before every risky deploy and schedule a daily one via GitHub Actions cron against the public proxy. The first real backup this project ever had was `instance/backups/proam_recovery_20260408_115838.sql.gz`, taken during recovery, not before it.

**12. Offline migration dry-runs are cheap diagnostics.** `flask db upgrade --sql` dumps the migration chain as raw SQL with no DB connection — use it to inspect what WOULD run before touching production, and to diff against the migration chain in MEMORY.md.

**13. SQLite (dev) and Postgres (prod) diverge in migration patterns.** Seventeen migration files in this repo historically used SQLite-specific idioms (`batch_alter_table`, `server_default='0'` on booleans, `PRAGMA`) that silently failed or produced wrong schema on PG. `tests/test_pg_migration_safety.py` guards against regression. See [`../integration-issues/postgres-migration-compatibility.md`](../integration-issues/postgres-migration-compatibility.md) — do not re-document, but cross-reference before writing any new migration.

**14. Optional integrations must fail gracefully, never block the release phase.** STRATHMARK, Twilio SMS, and S3 backup all need to degrade silently when their env vars are missing or their upstreams are unreachable. Hard-failing `validate_runtime()` on missing optional env vars once blocked a release and required a hotfix. Gate environment-specific hard-fails on `ENV_NAME == 'production'` and reserve them for core infra (DB, SECRET_KEY). See [`../configuration-issues/validate-runtime-hard-fail-blocks-deploy.md`](../configuration-issues/validate-runtime-hard-fail-blocks-deploy.md) and [`../architecture-decisions/non-blocking-external-integrations.md`](../architecture-decisions/non-blocking-external-integrations.md).

**15. Local dev port 5000 collides with the UM contract portal; use 5001.** Codify this in `README.md` and provide a flag-free default in `run.py`. Do not lose a session per new contributor rediscovering this.

**16. The user noticed the 2-week outage before monitoring did.** Assume all silent failure modes will be found by the user, not by tooling. Every change that touches DB, migrations, or deploy config must include an observable signal (log line, `/health` field, alert) so the failure surfaces to tooling before it surfaces to the user. (session history)

## Why This Matters

The costs are concrete:

- **14 days of schemaless production** (2026-03-24 to 2026-04-08) — undetected by CI, undetected by Railway's own logs, found only when the user manually checked `/health` during unrelated work.
- **11-hour outage on 2026-04-08** with gunicorn looping SIGKILL for 5.5 hours before anyone noticed.
- **Zero backups until the recovery itself** — the only reason data loss was zero is that no competitor data had been entered yet.
- **Near-miss with race-day**: the detach occurred 17 days before event day. Had it happened on 2026-04-25 morning with a full competitor roster, this would be an event-cancelling disaster with no rollback path.

This is a solo-dev, solo-platform context. No on-call rotation, no SRE team, no dashboard-watcher. A single undetected failure mode stays undetected until it compounds into an outage, and the blast radius is the entire event. The lessons above are the minimum defense-in-depth for a stakes-mismatched deployment (Hobby-tier infrastructure carrying race-day-critical data).

## When to Apply

Consult this doc:

- Before triggering any Railway deploy that includes a new migration.
- When diagnosing a production 502, crash loop, or `/health` degraded response.
- Before setting up backups, monitoring, or any new env-var-dependent service.
- When a Postgres volume appears empty, detached, or returns connection-refused.
- When migrating schema patterns between SQLite (dev) and Postgres (prod), or when adding a new Alembic migration.
- Before race day, or before any production event where downtime translates to real-world consequences.
- When onboarding a new contributor or handing context to a fresh Claude Code session.

## Examples

### Example 1 — Running a schema migration on production Postgres from local terminal

```bash
# 1. Read the public proxy URL (needs project token via `railway login`)
railway variables --service Postgres-HB8_ | grep DATABASE_PUBLIC_URL

# 2. Export with the postgresql:// scheme fix
export DATABASE_URL="postgresql://postgres:<pass>@turntable.proxy.rlwy.net:48640/railway"

# 3. Dry-run first
flask db upgrade --sql > /tmp/pending.sql
less /tmp/pending.sql

# 4. Apply
flask db upgrade

# 5. Verify HEAD matches MEMORY.md
flask db current
# Expect: current HEAD from the Migration Chain entry in MEMORY.md
```

### Example 2 — Taking a manual backup before a risky deploy

```bash
# Requires pg_dump installed locally and DATABASE_URL exported as in Example 1
TS=$(date -u +%Y%m%d_%H%M%S)
pg_dump "$DATABASE_URL" | gzip > "instance/backups/proam_pre_deploy_${TS}.sql.gz"

# Verify non-empty
ls -lh "instance/backups/proam_pre_deploy_${TS}.sql.gz"
# Expect > 100KB once there's real data; 4-5KB for empty DB

# Restore path — DO NOT RUN unless recovering
# gunzip -c instance/backups/proam_pre_deploy_<ts>.sql.gz | psql "$DATABASE_URL"
```

### Example 3 — Verifying a deploy actually applied migrations

```bash
# 1. Immediately after Railway reports "deployed"
curl -s https://missoula-pro-am-manager-production.up.railway.app/health | jq
# {
#   "status": "ok",           <-- must be "ok", not "degraded"
#   "db": true,                <-- must be true
#   "migration_rev": "b1c2d3e4f5a6",  <-- must match HEAD in MEMORY.md
#   "version": "2.11.2"
# }

# 2. Cross-check via railway run
railway run --service App flask db current
# Expect same revision as migration_rev above

# 3. If they don't match: deploy did NOT run migrations. Apply manually per Example 1.
```

### Example 4 — Diagnosing a production crash loop

```bash
# Distinguish deploy-window 502 from real crash
for i in 1 2 3 4 5; do
  curl -w "%{http_code}\n" -o /dev/null -s \
    https://missoula-pro-am-manager-production.up.railway.app/health
  sleep 10
done

# 502 x 5-12 times = deploy in progress, wait
# 000 x any = crash loop, check Railway logs:
railway logs --service App --tail 200

# Common signature: volume detach -> SIGKILL loop
# psycopg2.OperationalError: could not connect to server
# [gunicorn] Worker (pid:NNN) was sent SIGKILL!
```

## Related

**Per-pattern canonical writeups** (read before taking action on that specific pattern):
- [`../configuration-issues/railway-predeploy-command.md`](../configuration-issues/railway-predeploy-command.md) — lesson 1: `releaseCommand` silent failure; `preDeployCommand` fix
- [`../integration-issues/postgres-migration-compatibility.md`](../integration-issues/postgres-migration-compatibility.md) — lesson 13: banned SQLite-specific migration patterns + `tests/test_pg_migration_safety.py`
- [`../configuration-issues/validate-runtime-hard-fail-blocks-deploy.md`](../configuration-issues/validate-runtime-hard-fail-blocks-deploy.md) — lesson 14: `ENV_NAME=='production'` gating for optional env vars
- [`../architecture-decisions/non-blocking-external-integrations.md`](../architecture-decisions/non-blocking-external-integrations.md) — lesson 14 architecture anchor: graceful no-op for STRATHMARK, Twilio, S3 backup

**Strategic and procedural docs**:
- [`../../ROOT_CAUSES_AND_CLEANUP_ORDER.md`](../../ROOT_CAUSES_AND_CLEANUP_ORDER.md) — strategic root-cause framing; this playbook is its tactical companion
- [`../../POSTGRES_MIGRATION.md`](../../POSTGRES_MIGRATION.md) — one-time SQLite → PG migration runbook (used for initial production bring-up)
- [`../../ROLLBACK_SOP.md`](../../ROLLBACK_SOP.md) — failure-classification SOP for deploy breakage
- [`../../RELEASE_CHECKLIST.md`](../../RELEASE_CHECKLIST.md) — pre-merge/pre-deploy gates and required CI checks
- [`../../GITHUB_REQUIRED_SETTINGS.md`](../../GITHUB_REQUIRED_SETTINGS.md) — branch protection + required status checks

**Sibling solution docs** — silent DB-layer failure family:
- [`../test-failures/test-data-polluting-production-sqlite-db-2026-04-21.md`](../test-failures/test-data-polluting-production-sqlite-db-2026-04-21.md) — the test-isolation defense. Both this playbook and that doc are about "DB-layer failures that stay silent for a long time because nothing is watching."

**Incident and feedback memory** (auto memory, Claude Code):
- `incident_2026-04-08_postgres_recovery.md` — primary incident narrative (schemaless DB + volume detach + recovery via public proxy)
- `feedback_migration_protocol.md` — "Never skip review of auto-generated Alembic migrations"
- `feedback_pg_migrations.md` — "Never use SQLite-specific patterns in migrations (PG production)"

**Code paths referenced**:
- `railway.toml` — current `preDeployCommand` config
- `config.py` — `_normalized_database_url()` scheme rewriter; `validate_runtime()`
- `services/backup.py` — S3 backup (currently dead code awaiting env vars + caller)
- `instance/backup_now.sh` — manual backup script used during recovery (requires `RAILWAY_TOKEN`)
- `.github/workflows/ci.yml` — 3-job CI (`lint`, `test`, `postgres-smoke`)
- `routes/main.py` `/health` endpoint — `status`, `db`, `migration_rev`
