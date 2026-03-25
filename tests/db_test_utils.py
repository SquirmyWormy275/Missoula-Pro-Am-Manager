"""
Shared test database helper — creates a Flask app backed by a temp-file
SQLite DB built via ``flask db upgrade`` (not ``db.create_all()``).

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
    """
    from app import create_app
    from flask_migrate import upgrade

    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    db_path = tmp.name
    tmp.close()
    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'migrations',
    )

    _app = create_app()
    _app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}',
        'WTF_CSRF_ENABLED': False,
        'WTF_CSRF_CHECK_DEFAULT': False,
        'SERVER_NAME': None,
    })

    with _app.app_context():
        _db.engine.dispose()
        if os.environ.get('TEST_USE_CREATE_ALL') == '1':
            # CI fallback: use db.create_all() to avoid migration chain issues
            _db.create_all()
        else:
            upgrade(directory=migrations_dir)

    return _app, db_path
