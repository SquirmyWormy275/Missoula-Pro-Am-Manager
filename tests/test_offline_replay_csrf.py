"""Real CSRF enforcement must permit the documented authenticated replay path."""
import re

import pytest
from sqlalchemy import text

from database import db
from tests.db_test_utils import create_test_app, drop_test_db
from tests.test_offline_scoring_u2 import _make_user, _seed_score


@pytest.fixture()
def app():
    application, handle = create_test_app(use_migrations=True)
    application.config.update(TESTING=False, WTF_CSRF_ENABLED=True,
                              WTF_CSRF_CHECK_DEFAULT=True, PROPAGATE_EXCEPTIONS=False)
    try:
        with application.app_context():
            yield application
            db.session.remove()
            db.engine.dispose()
    finally:
        drop_test_db(handle)


def _login_and_payload(client, db_session):
    user = _make_user(db_session)
    tournament, _event, heat, competitor, request_id, payload = _seed_score(db_session, user_id=user.id)
    username = user.username
    tid, hid, cid = tournament.id, heat.id, competitor.id
    db_session.commit()
    form = client.get('/auth/login').get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', form).group(1)
    assert client.post('/auth/login', data={'csrf_token': token, 'username': username,
                                          'password': 'test-password'}).status_code == 302
    authority = client.get('/scoring/api/replay-token').get_json()
    assert authority['ok'] is True
    payload.update(csrf_token='expired-synthetic-csrf', replay_token=authority['replay_token'])
    return tid, hid, cid, request_id, payload


def test_replay_accepts_user_bound_hmac_after_normal_form_csrf_expires(client, db_session):
    tid, hid, cid, request_id, payload = _login_and_payload(client, db_session)
    headers = {'Accept': 'application/json'}
    rejected = client.post(f'/scoring/{tid}/heat/{hid}/enter', data=payload, headers=headers)
    assert rejected.status_code == 400
    assert rejected.get_json()['error']['code'] == 'csrf_expired'
    recovered = client.post('/scoring/api/replay', data=payload, headers=headers)
    assert recovered.status_code == 200
    assert recovered.get_json()['receipt']['request_id'] == request_id
    with db.engine.connect() as connection:
        assert connection.execute(text('SELECT result_value FROM event_results WHERE competitor_id=:id'),
                                  {'id': cid}).scalar_one() == 9
        assert connection.execute(text('SELECT count(*) FROM score_submission_receipts WHERE request_id=:id'),
                                  {'id': request_id}).scalar_one() == 1


@pytest.mark.parametrize('token', ['', 'forged'])
def test_replay_exemption_still_rejects_missing_or_forged_authority(client, db_session, token):
    _tid, _hid, cid, _request_id, payload = _login_and_payload(client, db_session)
    payload['replay_token'] = (payload['replay_token'].split('.')[0] + '.' + '0' * 32
                               if token == 'forged' else token)
    response = client.post('/scoring/api/replay', data=payload, headers={'Accept': 'application/json'})
    assert response.status_code == 403
    assert response.get_json()['error']['code'] == (
        'authorization_denied' if token == 'forged' else 'replay_token_required')
    with db.engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM score_submission_receipts')).scalar_one() == 0
        assert connection.execute(text('SELECT count(*) FROM event_results WHERE competitor_id=:id '
                                       'AND result_value IS NOT NULL'), {'id': cid}).scalar_one() == 0
