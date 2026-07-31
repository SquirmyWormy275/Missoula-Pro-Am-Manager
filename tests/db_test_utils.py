"""
Shared test database helper — creates a Flask app backed by a temp-file
SQLite DB built via ``flask db upgrade`` (not ``db.create_all()``).

SAFEGUARD: The DATABASE_URL env var is set to a temp file BEFORE create_app()
runs, so the app never even sees the production database URI.  This prevents
any possibility of test data leaking into instance/proam.db.

Import this from any test file:
    from tests.db_test_utils import create_test_app

D14-B (c43): set PROAM_UNIT_PG=1 to back the same factory with PostgreSQL
instead of SQLite. A schema template database (proam_unit_template) is built
once per interpreter via migrations, then every create_test_app() call
clones it (createdb -T, ~100ms) and returns a connection string to a private
throwaway database. Same engine as production: same locking, same NULL
ordering, same jsonb. The SQLite path remains the default until a full
green run under PG flips it; the split-engine caveats retire with it.
"""
import os
import tempfile

os.environ.setdefault('SECRET_KEY', 'test-secret-conftest')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

from database import db as _db

_PG_HOST = os.environ.get("PROAM_UNIT_PG_HOST", "localhost")
_PG_URL = f"postgresql://proam:proam@{_PG_HOST}:5432"
_PG_TEMPLATE = "proam_unit_template"
_pg_template_ready = False
_pg_counter = [0]


def _pg_run(sql, dbname="postgres"):
    import subprocess
    return subprocess.run(
        ["psql", "-h", _PG_HOST, "-U", "proam", "-d", dbname, "-tAc", sql],
        env={**os.environ, "PGPASSWORD": "proam"},
        capture_output=True, text=True)


def _ensure_pg_template():
    """Build the schema template once per interpreter via flask db upgrade."""
    global _pg_template_ready
    if _pg_template_ready:
        return
    # Sweep clones orphaned by earlier runs (callers that os.unlink() the
    # handle no-op on PG names, so crashes leave databases behind).
    stale = _pg_run(
        "SELECT datname FROM pg_database WHERE datname LIKE 'proam_unit_%' "
        f"AND datname <> '{_PG_TEMPLATE}'")
    for name in stale.stdout.split():
        _pg_run(f'DROP DATABASE IF EXISTS {name} (FORCE)')
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
                upgrade(directory=os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'migrations'))
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


def create_test_app():
    """Create a Flask app backed by a temp-file SQLite DB built via migrations.

    Returns ``(app, db_path)`` — caller must delete ``db_path`` when done.
    Alembic cannot run against ``:memory:`` (it opens its own connection),
    so we use a temp file that survives the full test module lifetime.

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
            if os.environ.get('TEST_USE_CREATE_ALL') == '1':
                _db.create_all()
            else:
                upgrade(directory=migrations_dir)

        return _app, db_path
    finally:
        # Restore original DATABASE_URL
        if old_db_url is None:
            os.environ.pop('DATABASE_URL', None)
        else:
            os.environ['DATABASE_URL'] = old_db_url
