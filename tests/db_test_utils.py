"""
Shared test database helper — creates a Flask app backed by a temp-file
SQLite DB built via ``flask db upgrade`` (not ``db.create_all()``).

SAFEGUARD: The DATABASE_URL env var is set to a temp file BEFORE create_app()
runs, so the app never even sees the production database URI.  This prevents
any possibility of test data leaking into instance/proam.db.

Import this from any test file:
    from tests.db_test_utils import create_test_app
"""
import os
import tempfile

os.environ.setdefault('SECRET_KEY', 'test-secret-conftest')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

from database import db as _db


def create_test_app():
    """Create a Flask app backed by a temp-file SQLite DB built via migrations.

    Returns ``(app, db_path)`` — caller must delete ``db_path`` when done.
    Alembic cannot run against ``:memory:`` (it opens its own connection),
    so we use a temp file that survives the full test module lifetime.

    IMPORTANT: We set DATABASE_URL env var BEFORE importing/calling create_app()
    so that config.py resolves to the temp DB, not the production one.  The env
    var is restored after create_app() returns.
    """
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
        from app import create_app
        from flask_migrate import upgrade

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
