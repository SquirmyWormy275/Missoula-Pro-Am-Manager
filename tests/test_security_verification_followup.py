"""Regression coverage for reachable findings from the verification scan."""
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import event as sa_event

from database import db
from models import Event, Heat
from models.competitor import ProCompetitor
from models.user import User
from tests.test_security_cloud_regressions import _login_as
from tests.test_security_cloud_regressions import security_app as security_app  # noqa: F401


@pytest.mark.parametrize('target', ['////example.invalid/path', '//example.invalid', '/\\example.invalid', '/\n/evil', 'https://example.invalid'])
def test_language_redirect_stays_on_origin(security_app, target):
    response = security_app.test_client().get('/language/en', query_string={'next': target})
    assert response.status_code == 302
    assert response.headers['Location'] == '/'


def test_language_redirect_keeps_local_query(security_app):
    response = security_app.test_client().get('/language/en', query_string={'next': '/portal/?view=mobile'})
    assert response.headers['Location'] == '/portal/?view=mobile'


def test_legacy_pin_login_is_revoked_and_boolean_upgrade_is_closed(security_app):
    app = security_app
    tid, cid = app.config['TID'], app.config['CID']
    path = f'/portal/competitor/{tid}/pro/{cid}/my-results'
    client = app.test_client()
    with app.app_context():
        competitor = db.session.get(ProCompetitor, cid)
        competitor.set_portal_pin('12345678')
        db.session.commit()
    with client.session_transaction() as session:
        session[f'competitor_auth_{tid}_pro_{cid}'] = True
    assert b'name="pin"' in client.get(path).data
    assert b'name="pin"' not in client.post(path, data={'pin': '12345678'}).data
    with app.app_context():
        competitor = db.session.get(ProCompetitor, cid)
        competitor.set_portal_pin('87654321')
        db.session.commit()
    assert b'name="pin"' in client.get(path).data
    assert b'name="pin"' not in client.post(path, data={'pin': '87654321'}).data


def test_pin_target_limit_covers_both_paths_and_ignores_spoofed_form_ids(security_app):
    app = security_app
    app.config['RATELIMIT_ENABLED'] = True
    from routes.api import _limiter
    with app.app_context():
        _limiter.reset()
        competitor = db.session.get(ProCompetitor, app.config['CID'])
        competitor.set_portal_pin('12345678')
        db.session.commit()
    tid, cid = app.config['TID'], app.config['CID']
    for index in range(6):
        form = {'tournament_id': tid, 'competitor_type': 'pro', 'competitor_id': cid, 'pin': 'wrong'}
        if index % 2:
            path = f'/portal/competitor/{tid}/pro/{cid}/my-results'
            form.update(tournament_id=999 + index, competitor_id=999 + index)
        else:
            path = '/portal/competitor/claim'
        response = app.test_client().post(path, data=form, environ_base={'REMOTE_ADDR': f'192.0.2.{index + 1}'})
        assert (response.status_code == 429) == (index == 5)


@pytest.mark.parametrize('role,fee_allowed,payout_allowed', [
    ('admin', True, True), ('judge', True, True), ('registrar', True, False),
    ('scorer', False, False), ('spectator', False, False), ('viewer', False, False),
])
def test_financial_writes_require_explicit_capability(security_app, role, fee_allowed, payout_allowed):
    app = security_app
    with app.app_context():
        user = User(username='finance-' + role, role=role)
        user.set_password('test-password-long')
        event = Event(tournament_id=app.config['TID'], name='Underhand', event_type='pro', scoring_type='time', gender='M')
        db.session.add_all([user, event])
        db.session.flush()
        user_id, event_id = user.id, event.id
        competitor = db.session.get(ProCompetitor, app.config['CID'])
        competitor.entry_fees = '{"example": 10}'
        db.session.commit()
    client = _login_as(app, user_id)
    tid = app.config['TID']
    response = client.post(f'/reporting/{tid}/pro/fee-tracker', data={'competitor_id': app.config['CID']})
    assert response.status_code == (302 if fee_allowed else 403)
    with app.app_context():
        assert bool(db.session.get(ProCompetitor, app.config['CID']).get_fees_paid()) == fee_allowed
    response = client.post(f'/reporting/{tid}/pro/event-fees', data={})
    assert response.status_code == (302 if fee_allowed else 403)
    for path in [f'/scoring/{tid}/event/{event_id}/payouts', f'/scoring/{tid}/pro/payout-manager']:
        response = client.post(path, data={'payout_1': '20'})
        assert response.status_code == (302 if payout_allowed else 403)
    if role == 'scorer':
        assert client.get(f'/scoring/{tid}/event/{event_id}/results').status_code == 200
        assert b'<fieldset disabled>' in client.get(f'/reporting/{tid}/pro/fee-tracker').data


def test_repair_followup_keeps_credentials_out_of_stdout(capsys):
    from scripts.repair_era1_references import _print_reproject_followup
    _print_reproject_followup([('events.payouts', 7)], 'postgresql://name:secret@host/db?password=other-secret')
    output = capsys.readouterr().out
    assert 'secret' not in output and 'postgresql://' not in output
    assert 'python -m scripts.reproject_birling 7' in output
    assert 'DATABASE_URL' in output


def test_holding_bin_renders_stored_names_as_literal_text():
    source = Path('templates/pro/flights.html').read_text()
    function = source[source.index('    function renderBin()'):source.index('    function renderBin()') + 2000]
    function = function[:function.index('\n    }') + 6]
    script = '''
const assert = require('node:assert/strict');
function element() {
  return {dataset: {}, children: [], classList: {toggle() {}},
    appendChild(child) { this.children.push(child); },
    set innerHTML(value) { assert.equal(value, '', 'untrusted active HTML'); }};
}
const document = {createElement: element};
const binItemsEl = element(), binCountEl = {}, binRoot = element();
const attack = '<form action="https://example.invalid"><input name="password"></form>';
const loadBin = () => [{id: 1, name: attack, event_name: attack}];
''' + function + '''
renderBin();
assert.equal(binItemsEl.children[0].children[1].textContent, attack);
assert.equal(binItemsEl.children[0].children[2].textContent, attack);
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)


def _seed_public_events(app, count=1, heats=10):
    with app.app_context():
        event_ids = []
        for index in range(count):
            event = Event(tournament_id=app.config['TID'], name=f'Underhand {index}', event_type='pro', scoring_type='time', gender='M', status='in_progress')
            db.session.add(event)
            db.session.flush()
            event_ids.append(event.id)
            for number in range(heats):
                heat = Heat(event_id=event.id, heat_number=number + 1, status='in_progress')
                db.session.add(heat)
                heat.set_roster('pro', [app.config['CID']])
        db.session.commit()
    return event_ids


def test_public_heat_and_kiosk_query_counts_do_not_grow_per_heat(security_app):
    from routes.portal import _build_event_heat_rows, _build_kiosk_payload
    app = security_app
    event_ids = _seed_public_events(app, count=12, heats=8)
    with app.app_context():
        from models import Tournament
        tournament = db.session.get(Tournament, app.config['TID'])
        event = db.session.get(Event, event_ids[0])
        queries = []
        def listener(*args):
            queries.append(args[2])
        sa_event.listen(db.engine, 'before_cursor_execute', listener)
        try:
            rows = _build_event_heat_rows(event)
            assert len(rows) == 8
            assert len(queries) <= 5
            queries.clear()
            payload = _build_kiosk_payload(tournament)
            assert len(payload['active_heats']) == 48
            assert all(row['competitors'][0]['name'] == 'Test Competitor' for row in payload['active_heats'])
            assert len(queries) <= 9
            json.dumps(payload, default=str)
        finally:
            sa_event.remove(db.engine, 'before_cursor_execute', listener)


def test_public_result_builds_only_selected_representation(security_app, monkeypatch):
    import routes.portal as portal
    app = security_app
    event_id = _seed_public_events(app)[0]
    with app.app_context():
        db.session.get(Event, event_id).status = 'completed'
        db.session.commit()
    monkeypatch.setattr(portal, '_build_event_heat_rows', lambda event: pytest.fail('ranking must not build heat view'))
    response = app.test_client().get(f'/portal/spectator/{app.config["TID"]}/event/{event_id}?sort=ranking')
    assert response.status_code == 200


@pytest.mark.parametrize('suffix', ['kiosk/{tid}', 'spectator/{tid}/event/{eid}'])
def test_public_expensive_views_are_throttled(security_app, suffix):
    app = security_app
    event_id = _seed_public_events(app)[0]
    app.config['RATELIMIT_ENABLED'] = True
    from routes.api import _limiter
    with app.app_context():
        _limiter.reset()
    client = app.test_client()
    path = '/portal/' + suffix.format(tid=app.config['TID'], eid=event_id)
    for _ in range(30):
        assert client.get(path).status_code != 429
    assert client.get(path).status_code == 429
