"""Publication and staff authority boundaries from the final verification scan."""

import re

import pytest

from database import db
from models import Event, EventResult, Tournament, User
from models.competitor import ProCompetitor
from routes.proam_relay import _relay_payout_state_digest
from tests.test_security_cloud_regressions import _login_as
from tests.test_security_cloud_regressions import security_app as security_app  # noqa: F401


def _staff(app, role, *, scope=None):
    with app.app_context():
        user = User(username=f'final-{role}-{scope}', role=role, tournament_id=scope)
        user.set_password('synthetic-password-only')
        db.session.add(user)
        db.session.commit()
        return user.id


def _event(app, *, name='Underhand', status='in_progress'):
    with app.app_context():
        event = Event(tournament_id=app.config['TID'], name=name, event_type='pro', scoring_type='time', gender='M', status=status)
        db.session.add(event)
        db.session.commit()
        return event.id


@pytest.mark.parametrize('role,allowed', [('admin', True), ('judge', True), ('scorer', False)])
def test_relay_payout_write_uses_financial_capability_with_current_digest_and_csrf(security_app, role, allowed):
    app = security_app
    event_id = _event(app, name='Pro-Am Relay')
    client = _login_as(app, _staff(app, role))
    app.config['WTF_CSRF_ENABLED'] = True
    path = f'/tournament/{app.config["TID"]}/proam-relay/payouts'
    page = client.get(path)
    assert page.status_code == 200
    csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
    with app.app_context():
        tournament = db.session.get(Tournament, app.config['TID'])
        event = db.session.get(Event, event_id)
        digest = _relay_payout_state_digest(tournament, event)
    response = client.post(path, data={'csrf_token': csrf, 'expected_payout_digest': digest, 'payout_1': '275'})
    assert response.status_code == (302 if allowed else 403)
    with app.app_context():
        assert db.session.get(Event, event_id).get_payouts() == ({'1': 275.0} if allowed else {})
    if not allowed:
        assert b'<fieldset disabled>' in page.data


@pytest.mark.parametrize('prefix', ['/api', '/api/v1'])
def test_public_handicap_feed_publishes_only_completed_history(security_app, prefix):
    app = security_app
    expected = []
    with app.app_context():
        for index, (event_status, result_status) in enumerate([
            ('pending', 'completed'), ('in_progress', 'completed'),
            ('completed', 'pending'), ('completed', 'completed'),
        ]):
            event = Event(tournament_id=app.config['TID'], name=f'Underhand {index}', event_type='pro', scoring_type='time', gender='M', status=event_status)
            competitor = ProCompetitor(tournament_id=app.config['TID'], name=f'Synthetic {index}', gender='M')
            db.session.add_all([event, competitor])
            db.session.flush()
            result = EventResult(event_id=event.id, competitor_id=competitor.id, competitor_type='pro', competitor_name=competitor.name, status=result_status, result_value=25 + index, final_position=1, points_awarded=10, payout_amount=100)
            db.session.add(result)
            if event_status == result_status == 'completed':
                expected.append(competitor.id)
        db.session.commit()
        from services.handicap_export import build_chopping_rows
        # Staff's full export remains available; public projection changes no data.
        assert len(build_chopping_rows(db.session.get(Tournament, app.config['TID']))) == 4
    response = app.test_client().get(f'{prefix}/public/tournaments/{app.config["TID"]}/handicap-input')
    assert response.status_code == 200
    assert [row['competitor_id'] for row in response.json['chopping_results']] == expected


def test_scoped_scorer_cannot_finalize_other_tournament(security_app):
    app = security_app
    with app.app_context():
        other = Tournament(name='Other scoped tournament', year=2028)
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    event_id = _event(app)
    client = _login_as(app, _staff(app, 'scorer', scope=other_id))
    response = client.post(f'/scoring/{app.config["TID"]}/event/{event_id}/finalize')
    assert response.status_code == 403
    with app.app_context():
        assert db.session.get(Event, event_id).status == 'in_progress'


def test_scoped_judge_cannot_open_other_tournament_shadow_page(security_app):
    app = security_app
    with app.app_context():
        other = Tournament(name='Other judge tournament', year=2028)
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    event_id = _event(app)
    client = _login_as(app, _staff(app, 'judge', scope=other_id))
    response = client.get(f'/scheduling/{app.config["TID"]}/events/{event_id}/shadow-marks')
    assert response.status_code == 403


def test_every_tournament_management_route_denies_cross_scope_before_dispatch(security_app):
    from app import MANAGEMENT_BLUEPRINTS

    app = security_app
    with app.app_context():
        other = Tournament(name='Assigned tournament', year=2029)
        db.session.add(other)
        db.session.commit()
        scope_id = other.id
    client = _login_as(app, _staff(app, 'admin', scope=scope_id))
    adapter = app.url_map.bind('localhost')
    checked = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint.split('.', 1)[0] not in MANAGEMENT_BLUEPRINTS:
            continue
        scope_names = rule.arguments & {'tournament_id', 'tid'}
        if not scope_names:
            continue
        values = {name: 1 for name in rule.arguments}
        values.update({name: app.config['TID'] for name in scope_names})
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            path = adapter.build(rule.endpoint, values, method=method)
            response = client.open(path, method=method, data={'tournament_id': scope_id})
            assert response.status_code == 403, (rule.endpoint, method, path, response.status_code)
            checked.add(rule.endpoint)
    assert {'scoring.finalize_event', 'scoring.record_throwoff', 'scoring.import_results',
            'scoring.scratch_confirm', 'scoring.scratch_undo', 'scoring.undo_heat_save',
            'scheduling.shadow_marks', 'proam_relay.save_relay_payouts'} <= checked


@pytest.mark.parametrize('role', ['admin', 'judge', 'scorer'])
@pytest.mark.parametrize('scoped', [True, False])
def test_own_or_global_scoring_access_remains_available(security_app, role, scoped):
    app = security_app
    event_id = _event(app)
    client = _login_as(app, _staff(app, role, scope=app.config['TID'] if scoped else None))
    path = f'/scoring/{app.config["TID"]}/event/{event_id}'
    assert client.get(path + '/results').status_code == 200
    assert client.post(path + '/throwoff').status_code == 302


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_cross_scope_shadow_denial_never_contacts_provider(security_app, monkeypatch, method):
    app = security_app
    with app.app_context():
        other = Tournament(name='Separate shadow authority', year=2029)
        db.session.add(other)
        db.session.commit()
        scope_id = other.id
    event_id = _event(app)
    client = _login_as(app, _staff(app, 'judge', scope=scope_id))
    monkeypatch.setattr('routes.scheduling.shadow_marks._shadow_client', lambda: pytest.fail('cross-scope remote call'))
    response = client.open(f'/scheduling/{app.config["TID"]}/events/{event_id}/shadow-marks', method=method, data={'action': 'calculate'})
    assert response.status_code == 403
