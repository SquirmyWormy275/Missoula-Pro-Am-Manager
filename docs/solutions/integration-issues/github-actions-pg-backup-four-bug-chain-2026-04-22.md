---
title: Four-bug chain in daily PostgreSQL backup GitHub Actions workflow
date: 2026-04-22
category: integration-issues
module: .github/workflows/daily-backup.yml
problem_type: integration_issue
component: tooling
symptoms:
  - 'pg_dump: error: invalid connection option "DATABASE_PUBLIC_URL" (literal KEY= prefix left in secret)'
  - 'FATAL: database "railway\n" does not exist (trailing whitespace in secret value)'
  - 'pg_dump: error: aborting because of server version mismatch (pg_wrapper dispatched PG 14 client against PG 18.3 server)'
  - 'Sanity-check step fails with SIGPIPE exit code under set -o pipefail when gunzip -c | head -20 closes stdin early'
  - '~15 red workflow runs over the setup window; zero usable backups until all four fixes landed'
root_cause: config_error
resolution_type: workflow_improvement
severity: critical
related_components:
  - database
  - development_workflow
tags:
  - github-actions
  - postgres
  - pg-dump
  - railway
  - backup
  - ci-cd
  - pg-wrapper
  - pipefail
---

# GitHub Actions PostgreSQL Backup Workflow — Four Sequential Footguns

## Problem

Setting up a daily `pg_dump`-based PostgreSQL backup workflow on GitHub Actions (`.github/workflows/daily-backup.yml`) for a Railway-hosted Postgres 18 database fails **four times in a row** before it succeeds, even though each individual fix is a one-liner. The failures chain because each footgun only surfaces after the previous one is cleared:

1. Secret value includes `KEY=` prefix.
2. Secret value has a trailing newline.
3. Wrong `pg_dump` resolved from `$PATH`.
4. `gunzip | head` + `set -o pipefail` raises SIGPIPE.

None of these are bugs in the individual tools. They are composition hazards at the GitHub Actions / apt / libpq / bash boundary. This doc exists so the next person setting up Postgres backup automation can inoculate against **all four at once**, not rediscover them in sequence.

## Symptoms

| # | Exact error | Layer |
|---|---|---|
| 1 | `pg_dump: error: invalid connection option "DATABASE_PUBLIC_URL"` | libpq connection string parser |
| 2 | `FATAL: database "railway\n" does not exist` (literal newline in dbname) | GitHub Secrets input preservation |
| 3 | `pg_dump: error: aborting because of server version mismatch` | Debian `pg_wrapper` vs. versioned bindir |
| 4 | `gzip: stdout: Broken pipe` → `Process completed with exit code 1` | Bash SIGPIPE + `pipefail` |

Each run appears to "just fail at a different place" — classic fix-one-thing-find-another chain.

## What Didn't Work

Three user attempts to fix the secret value by editing the GitHub Actions secret directly, each surfacing a new failure:

- **Attempt 1**: Pasted `DATABASE_PUBLIC_URL=postgresql://...` (full line from `railway variables --kv | Select-String DATABASE_PUBLIC_URL`) into the secret. → Bug 1.
- **Attempt 2**: Stripped the `DATABASE_PUBLIC_URL=` prefix, pasted URL only — but the copy source (web code block) ended with `\n`, and GitHub Secrets preserves it byte-for-byte. → Bug 2.
- **Attempt 3**: Pasted a clean URL, no newline. Now `pg_dump` runs, but the version mismatch (Bug 3) shows up. After Bug 3 is fixed, the dump itself succeeds — and then Bug 4 emerges in the "sanity check" step.

The lesson from this sequence: **don't trust the secret value**. Defend in the workflow, not in the human paste.

## Solution

All four fixes live in `.github/workflows/daily-backup.yml`. Apply them together.

### Fix 1 + 2 — Sanitize the secret value

```bash
# Strip optional KEY= prefix AND all whitespace (newlines, spaces, CR).
RAW_URL=$(printf '%s' "$RAILWAY_PG_PUBLIC_URL" | tr -d '[:space:]')
if [[ "$RAW_URL" =~ ^[A-Z0-9_]+= ]]; then
  RAW_URL="${RAW_URL#*=}"
fi
# Fail fast with a clear message if the secret is obviously wrong-shaped.
if [[ "$RAW_URL" != postgresql://* && "$RAW_URL" != postgres://* ]]; then
  echo "::error::RAILWAY_PG_PUBLIC_URL does not look like a postgres:// URL after sanitization"
  exit 1
fi
```

- `tr -d '[:space:]'` removes newlines, CR, spaces, tabs. URLs can't legally contain whitespace, so this is always safe.
- `${VAR#*=}` trims through the first `=`, but only after the regex confirms the value starts with `KEY=` (prevents eating a legitimate `=` that appears later in the URL's query string).
- The shape check turns "gibberish deep in libpq" into a clear workflow error on line 1.

### Fix 3 — Pin `pg_dump` to the versioned bindir

```bash
sudo apt-get update
sudo apt-get install -y postgresql-client-18

# Debian ships /usr/bin/pg_dump as a pg_wrapper symlink that dispatches
# to whatever version it considers the default — NOT necessarily the
# newest installed. Prepend the PG 18 bindir so later steps pick it up.
echo "/usr/lib/postgresql/18/bin" >> "$GITHUB_PATH"

# Verify. If this prints PG 14/15/16, Fix 3 didn't take.
/usr/lib/postgresql/18/bin/pg_dump --version
```

The explicit absolute-path version print is the canary. If a future runner image changes how `pg_wrapper` defaults, this line makes the regression obvious.

### Fix 4 — Isolate the `gunzip | head` sanity check

```yaml
- name: Dump + compress  # canonical pipeline — pipefail ON
  run: |
    set -euo pipefail
    pg_dump "$RAW_URL" | gzip > "$BACKUP_FILE"

- name: Sanity-check dump content  # exploratory read — pipefail OFF
  run: |
    # Deliberately NOT using `set -o pipefail`. `gunzip | head` closes
    # the pipe after 20 lines (by design); gunzip receives SIGPIPE;
    # pipefail would convert that into a fatal step failure.
    set -eu
    echo "--- First 20 lines of dump ---"
    gunzip -c "$BACKUP_FILE" | head -20 || true
```

Two steps, two different `set` policies. The canonical pipeline keeps `pipefail` (real failures must fail the build). The exploratory "show me the first N lines" step drops it.

## Why This Works

**Bug 1** — libpq accepts its connection string as either a URL (`postgresql://...`) or a space-separated list of `key=value` pairs (`host=... dbname=... user=...`). When the arg starts with an uppercase identifier followed by `=`, libpq takes the `key=value` path and balks at unknown keys like `DATABASE_PUBLIC_URL`. Stripping the prefix converts the input into the URL form libpq expects.

**Bug 2** — GitHub Actions Secrets are stored as raw bytes. The UI does not trim. A trailing newline becomes part of `$RAILWAY_PG_PUBLIC_URL`, which libpq reads as part of the dbname path segment. `tr -d '[:space:]'` normalizes regardless of paste source.

**Bug 3** — On Debian/Ubuntu, `/usr/bin/pg_dump` is `pg_wrapper`, a dispatcher that reads `/etc/postgresql-common/user_clusters` and the `PG_CLUSTER_CONF_ROOT` env to decide which installed version to invoke. `apt-get install postgresql-client-18` installs the binaries into `/usr/lib/postgresql/18/bin/` but does **not** change `pg_wrapper`'s default. PG's compatibility rule is hard: the client major version must be >= the server major version, or `pg_dump` aborts. Prepending the versioned bindir to `$GITHUB_PATH` makes bash resolve `pg_dump` directly, bypassing the wrapper.

**Bug 4** — `head -20` is working correctly: it reads 20 lines, closes stdin, exits 0. The OS then sends `SIGPIPE` to `gunzip` because it tries to write to a closed pipe. `gunzip` dies with non-zero exit. Under `set -o pipefail`, the pipeline's exit status is the rightmost non-zero exit, so the step fails. `|| true` catches the status; dropping `pipefail` for this specific step is the cleaner fix because SIGPIPE here is **expected behavior**, not an error.

## Prevention

Four concrete rules for any future GitHub Actions workflow that touches Postgres:

1. **Secrets are untrusted input.** For every secret holding a URL or connection string, run it through:
   ```bash
   VAL=$(printf '%s' "$SECRET" | tr -d '[:space:]')
   [[ "$VAL" =~ ^[A-Z0-9_]+= ]] && VAL="${VAL#*=}"
   ```
   Two lines, zero cost, defends against the two most common paste mistakes forever.

2. **Fail fast with a shape check.** Right after sanitization, assert the value matches the expected pattern (`postgresql://`, `https://`, numeric ID, etc.). Turn "mystery error 200 lines into the log" into "line 3: secret is malformed".

3. **When installing a versioned Postgres client, pin the PATH.** Always:
   ```bash
   sudo apt-get install -y postgresql-client-N
   echo "/usr/lib/postgresql/N/bin" >> "$GITHUB_PATH"
   /usr/lib/postgresql/N/bin/pg_dump --version   # log the real version
   ```
   Never trust `/usr/bin/pg_dump` to be the version you just installed.

4. **Separate canonical pipelines from exploratory reads.** Any `… | head -N`, `… | tail -N`, or early-termination read goes in its own step with `pipefail` **off** (or use `|| true`). Keep `pipefail` on for pipelines where every stage must succeed.

## Related

**Parent operational context**:
- [`../best-practices/railway-postgres-operational-playbook-2026-04-21.md`](../best-practices/railway-postgres-operational-playbook-2026-04-21.md) — lesson 11 flagged the GHA backup as dead code awaiting implementation; this doc is that lesson's tactical completion. Lessons 7 (URL normalization) and 8 (public proxy URL) are the adjacent operational rules.

**Adjacent "silent fallback" patterns** (different bug, same shape):
- [`../configuration-issues/railway-predeploy-command.md`](../configuration-issues/railway-predeploy-command.md) — Railway's builder silently ignoring `releaseCommand` and falling back to no release phase. Same meta-pattern as Bug 3: a dispatch layer picking the wrong default.
- [`../integration-issues/postgres-migration-compatibility.md`](postgres-migration-compatibility.md) — SQLite dev vs PG prod dialect divergence. Same meta-pattern as Bug 3: tooling version mismatch across environments.

**Procedural runbooks**:
- [`../../POSTGRES_MIGRATION.md`](../../POSTGRES_MIGRATION.md) — canonical doc for the `DATABASE_PUBLIC_URL` source and public proxy usage.
- [`../../ROLLBACK_SOP.md`](../../ROLLBACK_SOP.md) — failure-classification SOP. A broken backup job means no rollback path; this doc exists so the backup stays live.

**Incident / memory**:
- Auto-memory `incident_2026-04-08_postgres_recovery.md` — the 11-hour outage that proved "no backups until recovery itself" is not a survivable state. This workflow is the structural fix.

**Code paths referenced**:
- `.github/workflows/daily-backup.yml` — the file fixed
- `services/backup.py` — the in-app S3 uploader that remains dead code; the GHA workflow is the out-of-app path that now runs daily + hourly during race weekend
- `instance/backup_now.sh` — the manual-recovery script used during the 2026-04-08 incident
- `config.py::_normalized_database_url()` — the scheme rewrite that applies in the Flask app; the workflow does not need it because `pg_dump` accepts both `postgres://` and `postgresql://` schemes
