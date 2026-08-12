"""
PostgreSQL runtime smoke tests.

These tests only run when DATABASE_URL points to PostgreSQL. They validate the
real application factory, database connectivity, migrations, and a minimal ORM
write/read path against a PostgreSQL database.
"""
from __future__ import annotations

import os

import pytest

from database import db
from database import db as _db

if not os.environ.get('DATABASE_URL', '').startswith('postgresql://'):
    pytest.skip(
        'PostgreSQL runtime smoke tests require DATABASE_URL=postgresql://...',
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.fixture(scope='module')
def app():
    from app import create_app

    _app = create_app()
    _app.config.update({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
        'WTF_CSRF_CHECK_DEFAULT': False,
        'SERVER_NAME': None,
    })

    with _app.app_context():
        yield _app
        _db.session.remove()
        _db.engine.dispose()


def test_database_url_is_postgresql(app):
    assert app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgresql://')


def test_health_endpoint_reports_db_and_migration_current(app):
    response = app.test_client().get('/health')
    assert response.status_code == 200

    payload = response.get_json()
    assert payload is not None
    assert payload['db'] is True
    assert payload['migration_current'] is True


def test_postgresql_round_trip_insert_and_lookup(app):
    from models import Tournament

    with app.app_context():
        tournament = Tournament(name='Postgres Smoke', year=2099, status='setup')
        _db.session.add(tournament)
        _db.session.commit()

        loaded = _db.session.get(Tournament, tournament.id)
        assert loaded is not None
        assert loaded.name == 'Postgres Smoke'

        _db.session.delete(loaded)
        _db.session.commit()


def test_scratch_cascade_row_lock_uses_postgresql_nowait(app, monkeypatch):
    """Race-day scratch locks must be real NOWAIT locks on PostgreSQL."""
    from models.competitor import ProCompetitor
    from models.tournament import Tournament
    from services import scratch_cascade

    with app.app_context():
        tournament = Tournament(name='Postgres Scratch Lock', year=2099, status='setup')
        _db.session.add(tournament)
        _db.session.flush()
        competitor = ProCompetitor(
            tournament_id=tournament.id,
            name='Postgres Lock Competitor',
            gender='M',
            status='active',
        )
        _db.session.add(competitor)
        _db.session.commit()

        statements = []
        original_execute = _db.session.execute

        def capture_execute(statement, *args, **kwargs):
            statements.append(str(statement.compile(dialect=_db.engine.dialect)))
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(_db.session, 'execute', capture_execute)
        scratch_cascade._lock_cascade_rows(competitor, tournament, [])

        assert any('FOR UPDATE NOWAIT' in statement for statement in statements)

        _db.session.delete(competitor)
        _db.session.delete(tournament)
        _db.session.commit()
