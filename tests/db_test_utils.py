"""
Shared test database helper — creates a Flask app backed by a temp-file
SQLite DB built via ``flask db upgrade`` (not ``db.create_all()``).

SAFEGUARD: The DATABASE_URL env var is set to a temp file BEFORE create_app()
runs, so the app never even sees the production database URI.  This prevents
any possibility of test data leaking into instance/proam.db.

Import this from any test file:
    from tests.db_test_utils import create_test_app

D14-B: set PROAM_UNIT_PG=1 to back the same factory with PostgreSQL instead
of SQLite. A schema template database (proam_unit_template) is built once
per interpreter via migrations, then every create_test_app() call clones it
(createdb -T, ~100ms) and returns a connection string to a private
throwaway database. Same engine as production: same locking, same NULL
ordering, same jsonb.

Which engine runs when, and why (c44):

  local default   SQLite. Roughly 9 minutes versus 5-8 for PostgreSQL, and
                  it needs no server, so `pytest` works on a laptop with
                  nothing installed. Flipping this default is a separate
                  call, because it breaks every checkout without a local
                  server and role.
  CI, fast lane   SQLite (the `test` job). Answer on every push in ~4 min.
  CI, unit-postgres
                  The full suite under PROAM_UNIT_PG=1. This is the gate
                  that matters: engine-specific bugs cannot merge past it.

Both engines are green on the same tree and the difference between them is
exactly two skips, both in the backup/restore path, both deliberate and
documented at skip_unless_sqlite(). Any new divergence is a bug in one of
the two, not an accepted cost of the split.
"""
import os
import tempfile

os.environ.setdefault('SECRET_KEY', 'test-secret-conftest')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

from database import db as _db

# The rig's local role is proam/proam. CI brings its own service with a
# different superuser, so every piece of the connection is overridable.
_PG_HOST = os.environ.get("PROAM_UNIT_PG_HOST", "localhost")
_PG_PORT = os.environ.get("PROAM_UNIT_PG_PORT", "5432")
_PG_USER = os.environ.get("PROAM_UNIT_PG_USER", "proam")
_PG_PASSWORD = os.environ.get("PROAM_UNIT_PG_PASSWORD", "proam")
_PG_URL = f"postgresql://{_PG_USER}:{_PG_PASSWORD}@{_PG_HOST}:{_PG_PORT}"
_PG_TEMPLATE = "proam_unit_template"
_pg_template_ready = False
_pg_counter = [0]


def _isolate_report_cache():
    """Clear process cache state and keep tests out of the production shelf."""
    from services import report_cache

    with report_cache._lock:
        report_cache._cache.clear()
        report_cache._prefix_generations.clear()
        report_cache._generation_counter = 0
    report_cache._read_state.misses = {}
    report_cache._shelf_path = None
    report_cache._shelf_resolved = True


def _pg_run(sql, dbname="postgres"):
    import subprocess
    return subprocess.run(
        ["psql", "-h", _PG_HOST, "-p", _PG_PORT, "-U", _PG_USER,
         "-d", dbname, "-tAc", sql],
        env={**os.environ, "PGPASSWORD": _PG_PASSWORD},
        capture_output=True, text=True)


def _pg_preflight():
    """Fail loudly and specifically when PROAM_UNIT_PG=1 has nothing to talk to.

    Without this, an unreachable server produces a chain of quiet non-zero
    psql exits, an empty template probe, a CREATE DATABASE that also fails,
    and finally an unrelated-looking error several layers away. The person
    reading that traceback has no way to tell it means "start postgres".
    """
    probe = _pg_run("SELECT 1")
    if probe.stdout.strip() == "1":
        return
    raise RuntimeError(
        "PROAM_UNIT_PG=1 but the unit-suite PostgreSQL server is not "
        f"reachable at {_PG_USER}@{_PG_HOST}:{_PG_PORT}.\n"
        f"psql said: {(probe.stderr or '').strip() or '(no output; is psql installed?)'}\n"
        "Either start it and ensure the role exists with CREATEDB, override "
        "PROAM_UNIT_PG_HOST / _PORT / _USER / _PASSWORD, or unset "
        "PROAM_UNIT_PG to run the suite on SQLite."
    )


_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'migrations')


def _chain_head():
    """The revision the migration chain currently ends at.

    Read from the files rather than from any database, because this is the
    thing a stamped database is being compared against.
    """
    from alembic.script import ScriptDirectory
    return ScriptDirectory(_MIGRATIONS_DIR).get_current_head()


def _template_is_stale(head):
    """True when proam_unit_template exists but is not stamped at `head`.

    The template is a real database that outlives the interpreter that built
    it, so "it exists" is not the same question as "it is the schema this
    checkout describes". Before this check, adding a revision left every
    developer and every CI runner with a warm template one revision behind,
    and the whole PostgreSQL lane failed against a schema no migration ever
    produced. That was found the hard way on s8a0b2c3d4e5: 77 failures, all
    of them "column heat_assignments.uid does not exist", none of them a bug
    in the code under test.

    A missing alembic_version table, an unreadable one, or more than one row
    all read as stale. Rebuilding costs one `flask db upgrade`; guessing
    wrong in the other direction costs a red lane nobody can explain.
    """
    stamped = _pg_run("SELECT version_num FROM alembic_version",
                      dbname=_PG_TEMPLATE)
    if stamped.returncode != 0:
        return True
    lines = stamped.stdout.split()
    return len(lines) != 1 or lines[0] != head


def _ensure_pg_template():
    """Build the schema template once per interpreter via flask db upgrade.

    Rebuilds it when a template left behind by an earlier interpreter is
    stamped at anything other than the current chain head.
    """
    global _pg_template_ready
    if _pg_template_ready:
        return
    _pg_preflight()
    # Sweep clones orphaned by earlier runs (callers that os.unlink() the
    # handle no-op on PG names, so crashes leave databases behind).
    stale = _pg_run(
        "SELECT datname FROM pg_database WHERE datname LIKE 'proam_unit_%' "
        f"AND datname <> '{_PG_TEMPLATE}'")
    for name in stale.stdout.split():
        _pg_run(f'DROP DATABASE IF EXISTS {name} (FORCE)')
    head = _chain_head()
    probe = _pg_run(
        f"SELECT 1 FROM pg_database WHERE datname='{_PG_TEMPLATE}'")
    if probe.stdout.strip() == "1" and _template_is_stale(head):
        _pg_run(f'DROP DATABASE IF EXISTS {_PG_TEMPLATE} (FORCE)')
        probe = _pg_run(
            f"SELECT 1 FROM pg_database WHERE datname='{_PG_TEMPLATE}'")
    if probe.stdout.strip() != "1":
        _pg_run(f'CREATE DATABASE {_PG_TEMPLATE}')
        old = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f"{_PG_URL}/{_PG_TEMPLATE}"
        try:
            from flask_migrate import upgrade

            from app import create_app
            _tapp = create_app()
            with _tapp.app_context():
                _db.engine.dispose()
                upgrade(directory=_MIGRATIONS_DIR)
                # createdb TEMPLATE requires zero connections to the source;
                # drop ours and terminate any pooled stragglers.
                _db.engine.dispose()
            _pg_run("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{_PG_TEMPLATE}' AND pid <> pg_backend_pid()")
        finally:
            if old is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = old
    _pg_template_ready = True


def _create_test_app_pg():
    """Postgres twin of create_test_app(): clone the schema template.

    Returns (app, handle) where handle is the throwaway database NAME
    (callers that os.unlink(db_path) get a no-op via drop_test_db)."""
    _ensure_pg_template()
    _pg_counter[0] += 1
    name = f"proam_unit_{os.getpid()}_{_pg_counter[0]}"
    import atexit
    atexit.register(lambda n=name: _pg_run(f'DROP DATABASE IF EXISTS {n} (FORCE)'))
    r = _pg_run(f'CREATE DATABASE {name} TEMPLATE {_PG_TEMPLATE}')
    if r.returncode != 0:
        raise RuntimeError(f"clone of {_PG_TEMPLATE} failed: {r.stderr}")
    old = os.environ.get('DATABASE_URL')
    os.environ['DATABASE_URL'] = f"{_PG_URL}/{name}"
    try:
        from app import create_app
        _app = create_app()
        _app.config.update({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f"{_PG_URL}/{name}",
            'WTF_CSRF_ENABLED': False,
            'WTF_CSRF_CHECK_DEFAULT': False,
            'SERVER_NAME': None,
        })
        with _app.app_context():
            _db.engine.dispose()
        _isolate_report_cache()
        return _app, name
    finally:
        if old is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = old


def drop_test_db(handle):
    """Dispose a create_test_app() product regardless of backend."""
    if os.path.exists(handle):
        try:
            os.unlink(handle)
        except OSError:
            pass
        return
    if handle.startswith("proam_unit_"):
        _pg_run(f'DROP DATABASE IF EXISTS {handle} (FORCE)')


def create_test_app(*, use_migrations=False):
    """Create a Flask app backed by a temp-file SQLite DB built via migrations.

    Returns ``(app, db_path)`` — caller must delete ``db_path`` when done.
    Alembic cannot run against ``:memory:`` (it opens its own connection),
    so we use a temp file that survives the full test module lifetime.
    ``use_migrations=True`` bypasses the CI create-all shortcut for tests that
    exercise a schema transition itself.

    IMPORTANT: We set DATABASE_URL env var BEFORE importing/calling create_app()
    so that config.py resolves to the temp DB, not the production one.  The env
    var is restored after create_app() returns.
    """
    if os.environ.get('PROAM_UNIT_PG') == '1':
        return _create_test_app_pg()
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    db_path = tmp.name
    tmp.close()
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'migrations',
    )

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
            'WTF_CSRF_CHECK_DEFAULT': False,
            'SERVER_NAME': None,
        })

        # Verify the app is NOT pointing at the production DB
        uri = _app.config.get('SQLALCHEMY_DATABASE_URI', '')
        if 'instance' in uri and 'proam.db' in uri:
            os.unlink(db_path)
            raise RuntimeError(
                f'FATAL: Test app is using the production DB: {uri}. '
                'This should never happen. Check create_test_app().'
            )

        with _app.app_context():
            _db.engine.dispose()
            if os.environ.get('TEST_USE_CREATE_ALL') == '1' and not use_migrations:
                _db.create_all()
            else:
                upgrade(directory=migrations_dir)

        _isolate_report_cache()
        return _app, db_path
    finally:
        # Restore original DATABASE_URL
        if old_db_url is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = old_db_url


def skip_unless_sqlite(app, subject):
    """Skip a test whose subject is a SQLite-only code path.

    The decision is made from the app in front of us, not from the
    PROAM_UNIT_PG env var, for the same reason `_set_sqlite_pragma` in
    app.py decides from the connection: an env var describes what the
    runner intended, the URI describes what the test actually got.

    `subject` names the production code path, not the test, so the skip
    line in the pytest report reads as a statement about the codebase.
    Every use of this helper is a place where production behaviour differs
    by engine, and Railway production runs PostgreSQL. Treat a new call
    site as a finding, not as a way to quiet a red test.
    """
    import pytest
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not uri.startswith('sqlite'):
        pytest.skip(
            f'{subject} is implemented for SQLite only; this app is on '
            f'{uri.split(":", 1)[0]}. See PROAM_2026_C44 restore gap.'
        )


_source_db_verdict = {}


def _probe_reason(exc):
    """Pull one readable line out of whatever flask_migrate raised.

    flask_migrate turns an alembic failure into ``sys.exit(1)``, so the
    exception a caller actually catches is ``SystemExit: 1`` and says
    nothing at all. The real error is hanging off ``__context__``, which is
    where a migration guard's own message lives.
    """
    detail = exc
    hops = 0
    while (isinstance(detail, SystemExit)
           and detail.__context__ is not None
           and hops < 10):
        detail = detail.__context__
        hops += 1
    lines = str(detail).strip().splitlines()
    first = lines[0] if lines else repr(detail)
    return f"{type(detail).__name__}: {first}"


def source_db_reaches_head(source_db):
    """Can a copy of this developer database be migrated to chain head?

    Returns ``None`` when it can, or a one-line reason when it cannot.

    Three test files seed themselves by copying ``instance/proam.db`` into a
    tempfile and replaying the alembic chain over the copy:
    ``test_route_smoke.py``, ``test_edge_cases.py`` and
    ``test_integration_qa.py``. That worked for as long as every revision in
    the chain was willing to run against whatever a developer machine
    happened to be holding. D12-C commit F3 ended it. Revision
    ``t9b3c4d5e6f7`` refuses to drop ``heats.competitors`` when a heat names
    a roster in that column and has no ``heat_assignments`` rows, because the
    column is then the only copy of that roster. Any database stamped before
    D12-C commit E can be in exactly that state, and when it is, every
    fixture built on it errors at setup: 255 of them on this tree, not one of
    them about anything the tests were written to check.

    The guard is not softened here and must not be. Auto-seating the orphan
    is not available to it either: the JSON column holds bare integers with
    no kind, so turning one into a competitor means guessing pro or college
    from ``event.event_type``, which is the unsound inference
    ``services/reference_audit.py`` documents and the whole reason D12-C
    phase 1 put a ``uid`` on ``heat_assignments``. The container this was
    found on proves the point: its ``instance/proam.db`` holds a heat whose
    roster is ``[1]``, and it has both a pro competitor 1 and a college
    competitor 1. There is no sound answer. Refusing is correct.

    So the fixtures ask this once instead of finding out per test. A caller
    with a synthetic seed path takes it. A caller without one skips and
    prints this reason, which carries the guard's own words, so a developer
    sees what to seat or that the file wants rebuilding.

    Cached per interpreter: the answer is a property of a file that does not
    change during a run, and the probe costs a full chain replay.
    """
    key = str(source_db)
    if key in _source_db_verdict:
        return _source_db_verdict[key]

    import shutil

    probe_dir = tempfile.mkdtemp(prefix="proam-source-probe-")
    probe_db = os.path.join(probe_dir, "probe.db")
    shutil.copy2(str(source_db), probe_db)
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{probe_db}"
    reason = None
    try:
        from flask_migrate import upgrade

        from app import create_app
        _papp = create_app()
        with _papp.app_context():
            try:
                upgrade(directory=_MIGRATIONS_DIR)
            except (SystemExit, Exception) as exc:  # noqa: BLE001
                reason = _probe_reason(exc)
            finally:
                _db.engine.dispose()
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        shutil.rmtree(probe_dir, ignore_errors=True)

    _source_db_verdict[key] = reason
    return reason


def skip_unless_migrated(session, subject):
    """Skip a test that asserts on schema only Alembic produces.

    TEST_USE_CREATE_ALL=1 builds the schema from the models via
    db.create_all() instead of replaying the migration chain. The two are
    not the same schema. Constraints declared inline on a column
    (unique=True, db.ForeignKey(...)) come out ANONYMOUS under create_all,
    while the migration that added them gave them names. Constraints
    declared in __table_args__ with an explicit name= survive both paths,
    which is why most of these guards pass either way.

    Detected from the database rather than from the env var: create_all
    never writes an alembic_version table, so its absence means this schema
    did not come from migrations. Same principle as skip_unless_sqlite.

    That divergence is itself worth knowing about. If you want these
    assertions to hold on both paths, the fix is a naming_convention on the
    model MetaData, not a looser test. That is a change with autogenerate
    consequences and it has not been made.
    """
    import pytest
    import sqlalchemy as sa
    if 'alembic_version' not in sa.inspect(session.get_bind()).get_table_names():
        pytest.skip(
            f'{subject} asserts on migration-produced schema; this database '
            'was built by db.create_all() (TEST_USE_CREATE_ALL=1), which '
            'emits anonymous constraints for inline unique/ForeignKey.'
        )
