---
title: Test suite polluted production SQLite DB with fixture users and tournaments
date: 2026-04-21
category: test-failures
module: tests/conftest.py
problem_type: test_failure
component: testing_framework
symptoms:
  - Production instance/proam.db contained 17 test users (rbac_admin, smoke_admin, tmpl_admin, h_admin, comp_admin, post_admin) and 80+ test tournaments ("Flight Test 2026", "Test Tournament 2026", "Points Test 2026", "Relay Test 2026")
  - Real admin account missing from production DB, blocking login
  - "Create a judge account" flow errored "one already exists" because a test fixture user already occupied the judge role
  - Issue recurred across multiple dev sessions over weeks despite prior triage
  - DATABASE_URL env var set by tests was ignored because config was resolved once at class-definition time
root_cause: test_isolation
resolution_type: test_fix
severity: critical
related_components:
  - authentication
  - database
  - development_workflow
tags:
  - test-isolation
  - production-db-pollution
  - pytest-conftest
  - flask-app-factory
  - sqlite
  - database-url
  - fixture-leakage
  - runtime-guard
---

# Test suite polluted production SQLite DB with fixture users and tournaments

## Problem

The dev test suite for the Missoula-Pro-Am-Manager Flask app was silently writing fixture users and tournaments into the real `instance/proam.db` production SQLite file instead of isolated temp DBs. The user's real admin account was never created because test-generated admins occupied the unique `username` slots, and the `/auth/bootstrap` "create judge" flow failed with "one already exists." This recurred across multiple dev sessions for weeks before a permanent fix landed.

## Symptoms

- Production `instance/proam.db` contained 17 test users with obvious prefixes: `rbac_admin`, `rbac_judge`, `rbac_scorer`, `rbac_registrar`, `rbac_competitor`, `rbac_spectator`, `rbac_viewer`, `smoke_admin`, `tmpl_admin`, `h_admin`, `h_competitor`, `h_spectator`, `h_scorer`, `post_admin`, `comp_admin`, `comp_user`, `spec_viewer`
- 80+ test tournaments named "Flight Test 2026", "Test Tournament 2026", "Points Test 2026", "Relay Test 2026"
- Real admin account missing — user could not log in
- `/auth/bootstrap` "create a judge account" flow errored with `"one already exists"`
- Pollution recurred across sessions even after manual cleanup (user reported "solving this same fucking issue in loops over and over")
- Dev server process held a file lock on the polluted DB, surfacing as a misleading `Device or resource busy` error on `rm` until the server was killed

## What Didn't Work

Previous sessions attempted partial fixes. Each addressed only one failure mode and left the recurrence path open:

- **Setting `DATABASE_URL` in individual test files** — worked for new tests that remembered the override, but legacy `test_rbac_*.py` / `test_smoke_*.py` / `test_h_*.py` files imported `app` with the default URL pointing at `instance/proam.db`.
- **Relying on `TESTING=True` alone** — `TESTING` was set AFTER `create_app()` had already bound the engine to the production URI. The flag never got a chance to redirect the connection.
- **Trusting `BaseConfig.SQLALCHEMY_DATABASE_URI`** — the class attribute was computed once at module import and cached. Tests that set `DATABASE_URL` *after* importing `config` saw no effect.
- **Manual cleanup passes** (delete polluted rows between runs) — treated the symptom. The next `pytest` run re-polluted within minutes.
- **Per-test-file tempfile fixtures that called `create_app()` without env override** — the app factory resolved `DATABASE_URL` from `os.environ` at call time, and without the override, fell through to the default production path.
- **Debug skill step 3 "Check if production DB has test data"** (added 2026-04-05, `.claude/skills/debug/SKILL.md`) — documentation of awareness only, not code-level enforcement. Problem continued to recur for 16 days before this session's permanent fix. (session history)
- **QA-against-production risk recognition** (2026-04-08 session) — user and assistant agreed `/qa` must run against a local Flask instance, never against `https://missoula-pro-am-manager-production.up.railway.app/`. But this addressed the *browser-driven* pollution path. The `pytest` path that actually caused the observed `rbac_*` / `smoke_*` rows was a separate failure mode left unguarded. (session history)

None of these prior attempts installed a tripwire. Pollution was silent — there was no gate that failed loudly when a test wrote to the real DB.

## Solution

A four-layer defense-in-depth closes each distinct failure mode. The diagnose-cleanup-verify sequence used this session:

1. **Diagnose** — queried `User` and `Tournament` tables, confirmed 17 fixture users and 80+ fixture tournaments.
2. **Cleanup** — killed dev server PID holding the file lock (`taskkill //PID <pid> //F`), backed up `instance/proam.db` to `instance/proam.db.backup_testdata_2026-03-24`, deleted the polluted file, ran `flask db upgrade` to rebuild schema from migrations.
3. **Verify safeguards** — audited and confirmed all four layers active in `tests/conftest.py`, `app.py`, `tests/db_test_utils.py`, and `config.py`.
4. **Re-ran tests** — 2013 passed. Production DB byte count held constant at 167,936 bytes before and after the full session (fingerprint confirmed).

### Layer 1 — `tests/conftest.py` fingerprint monitor

Session-scoped autouse fixture + `pytest_unconfigure` hook that records `(exists, size, mtime)` of `instance/proam.db` at session start and compares at session end:

```python
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PROD_DB_PATH = _PROJECT_ROOT / 'instance' / 'proam.db'


def _prod_db_fingerprint():
    if _PROD_DB_PATH.exists():
        stat = _PROD_DB_PATH.stat()
        return (True, stat.st_size, stat.st_mtime)
    return (False, 0, 0)


def pytest_configure(config):
    config._prod_db_before = _prod_db_fingerprint()


def pytest_unconfigure(config):
    before = getattr(config, '_prod_db_before', None)
    if before is None:
        return
    after = _prod_db_fingerprint()
    if before != after:
        import warnings
        warnings.warn(
            f'\n\n*** PRODUCTION DATABASE MODIFIED BY TESTS ***\n'
            f'Before: exists={before[0]}, size={before[1]}, mtime={before[2]}\n'
            f'After:  exists={after[0]}, size={after[1]}, mtime={after[2]}\n'
            f'Path: {_PROD_DB_PATH}\nThis should NEVER happen.\n',
            stacklevel=1,
        )


@pytest.fixture(autouse=True, scope='session')
def _guard_production_db():
    before = _prod_db_fingerprint()
    yield
    after = _prod_db_fingerprint()
    if before != after:
        pytest.fail(
            f'FATAL: Tests modified the production database!\n'
            f'Before: exists={before[0]}, size={before[1]}\n'
            f'After:  exists={after[0]}, size={after[1]}\n'
            f'Path: {_PROD_DB_PATH}\n'
            f'All test data MUST use temporary databases via create_test_app().'
        )
```

The redundancy is deliberate. `pytest_unconfigure` emits a warning; the autouse fixture calls `pytest.fail`. Pytest plugins can swallow warnings, but an autouse fixture fails the run.

### Layer 2 — `app.py` runtime guard in `_create_app_inner()`

```python
# SAFEGUARD: Block tests from ever touching the production database.
if app.config.get('TESTING'):
    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if 'instance' in db_uri and 'proam.db' in db_uri:
        raise RuntimeError(
            'FATAL: Test is about to use the PRODUCTION database '
            f'({db_uri}). Tests MUST use a temporary database. '
            'Use create_test_app() from tests/db_test_utils.py.'
        )
```

Stops pollution at engine creation, before any SQL runs. Catches the case where `TESTING=True` was set but DB URI was not overridden.

### Layer 3 — `tests/db_test_utils.py::create_test_app()` sets env var BEFORE `create_app()`

```python
def create_test_app():
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    db_path = tmp.name
    tmp.close()

    # CRITICAL: Override DATABASE_URL BEFORE create_app() so config.py
    # never resolves to the production instance/proam.db path.
    old_db_url = os.environ.get('DATABASE_URL')
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'

    try:
        from flask_migrate import upgrade
        from app import create_app

        _app = create_app()
        _app.config.update({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}',
            'WTF_CSRF_ENABLED': False,
        })

        # Paranoia: double-check the app isn't pointing at prod
        uri = _app.config.get('SQLALCHEMY_DATABASE_URI', '')
        if 'instance' in uri and 'proam.db' in uri:
            os.unlink(db_path)
            raise RuntimeError(f'FATAL: Test app is using the production DB: {uri}.')

        with _app.app_context():
            _db.engine.dispose()
            upgrade(directory=migrations_dir)

        return _app, db_path
    finally:
        if old_db_url is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = old_db_url
```

Env-var-first is the only ordering that works with `create_app()`'s internal config resolution. Every test file must construct its app via this helper.

### Layer 4 — `config.py` re-resolves `DATABASE_URL` at app creation time

```python
# In get_config():
cfg.SQLALCHEMY_DATABASE_URI = _normalized_database_url()
```

Defeats the cached-class-attribute trap. Without this, `BaseConfig.SQLALCHEMY_DATABASE_URI` snapshots the URL at module import and ignores later env changes.

## Why This Works

The root cause was a chain: tests imported the Flask app without first overriding `DATABASE_URL`, so `config.py` resolved the default `sqlite:///instance/proam.db`. Once the engine was bound, all `db.session.commit()` calls wrote to the real file. Because the class-level attribute was cached, later attempts to set `DATABASE_URL` had no effect on the already-resolved config.

Each layer closes a distinct failure mode:

| Layer | Failure mode it closes |
|-------|------------------------|
| Layer 4 — `config.py` re-resolve | Cached-class-attribute trap. Config never goes stale. |
| Layer 3 — `create_test_app()` env-first | Ordering failure. Helper encodes the only safe sequence. |
| Layer 2 — `_create_app_inner` guard | Misconfigured-test-app failure. Factory refuses to return a prod-bound instance under `TESTING=True`. |
| Layer 1 — conftest fingerprint | Unknown-unknown failure. Catches anything the other three missed by watching the file itself. |

Together they convert a silent, recurring data-loss bug into a loud, immediate test failure. A test that tries to pollute will either raise `RuntimeError` at app creation (Layer 2), fail the session fingerprint check (Layer 1), or trip the verification check in Layer 3.

This also addresses the decoupling pattern noted in the [2026-04-08 Postgres recovery incident](../../../C:/Users/Alex%20Kaper/.claude/projects/c--Users-Alex-Kaper-Desktop-John-Ruffato-Startup-Challenge-Python-Missoula-Pro-Am/memory/incident_2026-04-08_postgres_recovery.md): test infrastructure was so decoupled from production infrastructure that real problems were invisible. The fingerprint check explicitly couples them — the test suite now has eyes on the production file.

## Prevention

Concrete practices and tests to prevent recurrence:

- **Always-on fingerprint check** in `tests/conftest.py` — redundant `pytest_unconfigure` warning AND session-scoped autouse fixture. Do not remove either half — they are layered for a reason.
- **Runtime `TESTING`-flag guard** in `_create_app_inner()` — refuses any app built with `TESTING=True` and a prod-looking DB URI.
- **`create_test_app()` is the single canonical entrypoint** for test app construction. Grep check for violations:
  ```bash
  grep -r "create_app()" tests/ | grep -v db_test_utils.py
  ```
  Any hit is a test file that should be refactored to use `create_test_app()`.
- **`config.py` must re-resolve `DATABASE_URL`** at each `get_config()` call. Never revert to a cached `BaseConfig.SQLALCHEMY_DATABASE_URI`.
- **CI runs the full test suite and fails on fingerprint mismatch.** Because Layer 1 calls `pytest.fail`, CI will exit non-zero. Do not suppress the warning in CI config.
- **Dev-side diagnostic** to detect pollution before it snowballs:
  ```bash
  sqlite3 instance/proam.db "SELECT username FROM users"
  ```
  Any username matching `^(rbac|smoke|tmpl|post|h|comp|spec)_` means pollution is back. Run weekly and after any test session that crashed mid-run.
- **Never call `db.create_all()` for schema changes.** Schema is managed exclusively by Flask-Migrate (project CLAUDE.md §6). `db.create_all()` against the prod DB outside migrations is another vector for state drift.
- **Kill stale dev server processes before DB cleanup.** File locks from `flask run` block `os.unlink`, giving a misleading `Device or resource busy` error that masks the actual pollution. Windows: `taskkill //PID <pid> //F`.
- **Backup before cleanup, always.** Back up `instance/proam.db` before `rm`, then `flask db upgrade` to rebuild. Treat the backup as disposable once verification passes, but never skip taking it.

## Related

- [docs/solutions/test-failures/tests-asserting-contradictory-behavior.md](tests-asserting-contradictory-behavior.md) — sibling doc: tests failing because they ignore the env flags the harness runs under. Same category of test-harness environment mismatch.
- [docs/solutions/configuration-issues/railway-predeploy-command.md](../configuration-issues/railway-predeploy-command.md) — related Railway/DB incident: `releaseCommand` silently ignored on Railway; `preDeployCommand` needed. Different defect, same production-DB blast radius. Both are "silent DB-surface failures that only surface in production."
- `CLAUDE.md` §6 "Test Isolation" — project-level principle this doc implements. Principle was present since project start; the 4-layer defense is the implementation.
- Auto-memory entry `incident_2026-04-08_postgres_recovery.md` — production was schemaless for 2 weeks because the release command silently wasn't running; CI tests passed against in-memory SQLite while prod was broken. Precedent for why prod-DB defense matters here.
- `tests/conftest.py` + `tests/db_test_utils.py` — Layers 1 and 3 of the defense. Do not refactor without reading this doc first.
