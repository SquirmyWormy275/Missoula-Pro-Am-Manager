"""Transaction-boundary regressions for the preflight auto-fix route."""

from unittest.mock import patch

import pytest

from database import db
from tests.db_test_utils import create_test_app, drop_test_db


@pytest.fixture(scope='module')
def app():
    app, db_handle = create_test_app()
    with app.app_context():
        yield app
        db.session.remove()
    drop_test_db(db_handle)


def test_preflight_autofix_rolls_back_and_reports_failure(app):
    from flask import session

    from models import Tournament
    from routes.scheduling.preflight import preflight_check

    with app.app_context():
        tournament = Tournament(
            name='Preflight Atomicity',
            year=2027,
            status='pro_active',
        )
        db.session.add(tournament)
        db.session.commit()
        tournament_id = tournament.id

    def _fail_after_flush(candidate, _saturday_ids):
        candidate.name = 'Partially Mutated'
        db.session.flush()
        raise RuntimeError('forced preflight spillover failure')

    with app.test_request_context(
            f'/scheduling/{tournament_id}/preflight',
            method='POST',
            data={'action': 'autofix'}):
        with patch(
            'routes.scheduling.preflight.run_preflight_autofix',
            side_effect=_fail_after_flush,
        ), patch('routes.scheduling.preflight.log_action'):
            response = preflight_check(tournament_id)
        flashes = list(session.get('_flashes', []))

    assert response.status_code in (302, 303)
    db.session.expire_all()
    assert db.session.get(Tournament, tournament_id).name == 'Preflight Atomicity'
    assert not [message for category, message in flashes if category == 'success']
    assert any(
        category == 'error' and 'rolled back' in message.lower()
        for category, message in flashes
    )
