"""Delegation preserves host sporting rules and recoverable initial binding."""

import json
import os
import subprocess
from decimal import Decimal

import pytest

from database import db
from models import Event, User
from services.managed_events import ManagedEventError
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_event_result,
    make_heat,
    make_pro_competitor,
    make_team,
)
from tests.test_managed_events import _projection_fixture, install_projection_guard


@pytest.fixture(autouse=True)
def projection_policy_peer(monkeypatch):
    install_projection_guard(monkeypatch)


def _award_fixture(kind):
    app, event_id, user_id, initial = _projection_fixture()
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.event_type = kind
        event.is_handicap = True
        event.set_payouts({'1': 100, '2': 50})
        team = make_team(db.session, event.tournament) if kind == 'college' else None
        if team:
            entrants = [make_college_competitor(db.session, event.tournament, team, f'Synthetic {i}') for i in range(2)]
        else:
            entrants = [make_pro_competitor(db.session, event.tournament, f'Synthetic {i}') for i in range(2)]
        for entrant in entrants:
            make_event_result(db.session, event, entrant, competitor_type=kind,
                              result_value=30, final_position=1, status='completed')
        if team:
            other = make_event(db.session, event.tournament, 'Synthetic independent points', event_type='college')
            make_event_result(db.session, other, entrants[0], competitor_type='college', points_awarded=3, status='completed')
        link = event.managed_operation
        link.state_json = json.dumps({**initial, 'status': 'finalized', 'summary_revision': 1})
        link.accepted_revision = link.summary_revision = 1
        db.session.commit()
        return app, event_id, user_id, team.id if team else None


def test_managed_college_awards_and_corrections_rebuild_team_totals():
    from models import Team
    from services.managed_events import apply_host_awards

    app, event_id, user_id, team_id = _award_fixture('college')
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        apply_host_awards(event, user)
        db.session.commit()
        assert [r.points_awarded for r in event.results.all()] == [Decimal('8.50'), Decimal('8.50')]
        assert db.session.get(Team, team_id).total_points == Decimal('20.00')
        rows = event.results.all()
        rows[0].status, rows[0].final_position = 'dq', None
        rows[1].final_position = 1
        apply_host_awards(event, user)
        db.session.commit()
        assert db.session.get(Team, team_id).total_points == Decimal('13.00')
        apply_host_awards(event, user)
        db.session.commit()
        assert db.session.get(Team, team_id).total_points == Decimal('13.00')


def test_managed_pro_ties_preserve_shared_position_payouts():
    from services.managed_events import apply_host_awards

    app, event_id, user_id, _ = _award_fixture('pro')
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        apply_host_awards(event, user)
        db.session.commit()
        assert [r.payout_amount for r in event.results.all()] == [100, 100]
        assert [r.final_position for r in event.results.all()] == [1, 1]


def test_managed_payout_template_route_preserves_positions():
    from models.payout_template import PayoutTemplate
    from services.managed_events import apply_host_awards

    app, event_id, user_id, _ = _award_fixture('pro')
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        apply_host_awards(event, user)
        template = PayoutTemplate(name='Synthetic managed purse')
        template.set_payouts({'1': 200, '2': 80})
        db.session.add(template)
        db.session.commit()
        tid, template_id = event.tournament_id, template.id
    client = app.test_client()
    client.post('/auth/login', data={'username': 'synthetic-projection-director', 'password': 'synthetic-only-password'})
    response = client.post(f'/scoring/{tid}/event/{event_id}/payouts',
                           data={'action': 'apply_template', 'template_id': template_id})
    assert response.status_code == 302
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert [r.final_position for r in event.results.all()] == [1, 1]
        assert [r.payout_amount for r in event.results.all()] == [200, 200]


def test_tournament_setup_cannot_change_managed_configuration():
    app, event_id, _, _ = _projection_fixture()
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.gender, event.is_handicap, event.max_stands, event.stand_type = 'M', True, 4, 'underhand'
        db.session.commit()
        tid = event.tournament_id
    client = app.test_client()
    client.post('/auth/login', data={'username': 'synthetic-projection-director', 'password': 'synthetic-only-password'})
    response = client.post(f'/scheduling/{tid}/events/setup', data={
        'action_scope': 'pro', 'enable_underhand': 'on', 'enable_underhand_M': 'on',
        'handicap_format_underhand': 'championship', 'stands_underhand': '2'})
    assert response.status_code == 409
    with app.app_context():
        event = db.session.get(Event, event_id)
        assert event.is_handicap is True and event.max_stands == 4
    # The independent college configuration remains available.
    assert client.post(f'/scheduling/{tid}/events/setup', data={'action_scope': 'college'}).status_code == 302


def test_drag_move_cannot_bypass_managed_heat_guard():
    app, event_id, _, _ = _projection_fixture()
    with app.app_context():
        event = db.session.get(Event, event_id)
        entrant = make_pro_competitor(db.session, event.tournament, 'Synthetic guarded entrant')
        source = make_heat(db.session, event, heat_number=1, competitors=[entrant.id])
        target = make_heat(db.session, event, heat_number=2, competitors=[])
        db.session.commit()
        tid, source_id, target_id, entrant_id = event.tournament_id, source.id, target.id, entrant.id
    client = app.test_client()
    client.post('/auth/login', data={'username': 'synthetic-projection-director', 'password': 'synthetic-only-password'})
    response = client.post(f'/scheduling/{tid}/heats/{source_id}/drag-move',
                           json={'competitor_ids': [entrant_id], 'target_heat_id': target_id})
    assert response.status_code == 409 and b'Use STRATHMARK' in response.data


def test_advanced_command_cannot_bypass_validated_source_creation(monkeypatch):
    from models.managed_event import ManagedCommand
    from services import managed_events

    app, event_id, user_id, _ = _projection_fixture()
    with app.app_context():
        def forbidden_call(*_args):
            pytest.fail('Unvalidated source creation reached the numerical process')
        monkeypatch.setattr(managed_events, '_call', forbidden_call)
        with pytest.raises(ManagedEventError, match='validated event binding'):
            managed_events.submit(db.session.get(Event, event_id), db.session.get(User, user_id),
                                  {'operation': 'create', 'request_id': 'foreign-create',
                                   'editor': 'missoula', 'payload': {'event_id': 'synthetic:foreign'}})
        assert ManagedCommand.query.count() == 0


def _binding_fixture(tmp_path):
    from models import WoodConfig

    interpreter = os.environ['PROAM_MANAGED_TEST_PYTHON']
    result = subprocess.run([interpreter, '-I', '-c',
                             'import json; from strathmark.events.examples import synthetic_event; print(json.dumps(synthetic_event()))'],
                            text=True, capture_output=True, check=True)
    config = json.loads(result.stdout)
    config['roster'] = config['roster'][:1]
    config['round_rules'] = [{'round_id': 'final', 'capacity': 4, 'qualifiers_per_heat': 0}]
    app, event_id, user_id, _ = _projection_fixture(linked=False)
    app.config.update(MANAGED_EVENTS_ENABLED=True, MANAGED_EVENTS_PYTHON=interpreter,
                      MANAGED_EVENTS_DATABASE=str(tmp_path / 'authority.sqlite3'))
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.is_handicap, event.max_stands, event.stand_type = True, 4, 'underhand'
        entrant = make_pro_competitor(db.session, event.tournament, 'Synthetic bind entrant',
                                      gender=config['roster'][0]['gender'], events=['Underhand'])
        config['roster'][0]['host_ref'] = f'pro:{entrant.id}'
        config['event_id'] = event.shadow_event_occurrence_id + ':managed'
        config['tournament_id'] = event.tournament.shadow_tournament_id
        db.session.add(WoodConfig(tournament_id=event.tournament_id, config_key='block_underhand_pro',
                                 species='S01', size_value=275, size_unit='mm'))
        db.session.commit()
    return app, event_id, user_id, config


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_rejected_initial_config_can_be_corrected_without_losing_request(tmp_path):
    from models.managed_event import ManagedCommand
    from services.managed_events import bind_event

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        config['quality_policy'] = 'unsupported'
        with pytest.raises(ManagedEventError, match='unsupported_quality_policy'):
            bind_event(event, user, config)
        db.session.rollback()
        original = ManagedCommand.query.one()
        assert original.status == 'pending' and event.managed_operation.state_json is None
        config['quality_policy'] = 'record_only_v2'
        config['roster'][0]['entry_id'] = 'corrected-entry'
        state = bind_event(event, user, config)
        assert state['revision'] == 1
        assert set(json.loads(event.managed_operation.mapping_json)) == {'corrected-entry'}
        assert ManagedCommand.query.count() == 2
        assert original.status == 'conflicting'
        assert json.loads(original.response_json)['error'] == 'initial_creation_superseded'


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_ambiguous_initial_creation_keeps_original_source_and_mapping(tmp_path, monkeypatch):
    from models.managed_event import ManagedCommand
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        original_entry = config['roster'][0]['entry_id']
        real_call = managed_events._call

        def lose_committed_response(*args):
            real_call(*args)
            raise ManagedEventError('Synthetic committed create response loss')

        monkeypatch.setattr(managed_events, '_call', lose_committed_response)
        with pytest.raises(ManagedEventError, match='response loss'):
            managed_events.bind_event(event, user, config)
        db.session.rollback()
        pending_id = ManagedCommand.query.one().id
        monkeypatch.setattr(managed_events, '_call', real_call)
        config['roster'][0]['entry_id'] = 'cannot-replace-original'
        with pytest.raises(ManagedEventError, match='event_already_exists'):
            managed_events.bind_event(event, user, config)
        db.session.rollback()
        state = managed_events.recover_command(pending_id, user)
        assert state['config']['roster'][0]['entry_id'] == original_entry
        assert set(json.loads(event.managed_operation.mapping_json)) == {original_entry}


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_binding_refuses_existing_pending_legacy_heats(tmp_path):
    from services.managed_events import bind_event

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        make_heat(db.session, event)
        db.session.commit()
        with pytest.raises(ManagedEventError, match='fresh'):
            bind_event(event, user, config)
        assert event.managed_operation is None and event.heats.count() == 1


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_changed_host_roster_cannot_prepare_source_forecast(tmp_path):
    from models import ProCompetitor
    from services.managed_events import bind_event, submit

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        state = bind_event(event, user, config)
        prior_request = {'operation': 'forecast', 'request_id': 'accepted-before-roster-edit',
                         'expected_revision': state['revision'], 'editor': 'missoula', 'payload': {}}
        accepted = submit(event, user, prior_request)
        entrant = ProCompetitor.query.one()
        entrant.events_entered = '[]'
        db.session.commit()
        assert submit(event, user, prior_request) == accepted
        with pytest.raises(ManagedEventError, match='active entered competitor'):
            submit(event, user, {'operation': 'forecast', 'request_id': 'stale-roster',
                                'expected_revision': accepted['revision'], 'editor': 'missoula', 'payload': {}})


@pytest.mark.parametrize('change', ['timer', 'physical_alias'])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_binding_preserves_host_timer_and_physical_stand_contract(tmp_path, change):
    from services.managed_events import bind_event

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    if change == 'timer':
        config['timer_policy'] = 'single'
    else:
        config['stands'] = ['1', '01']
    with app.app_context():
        with pytest.raises(ManagedEventError, match='dual|Physical'):
            bind_event(db.session.get(Event, event_id), db.session.get(User, user_id), config)


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_committed_command_response_loss_recovers_after_host_roster_edit(tmp_path, monkeypatch):
    from models import ProCompetitor
    from models.managed_event import ManagedCommand
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        state = managed_events.bind_event(event, user, config)
        request = {'operation': 'forecast', 'request_id': 'committed-before-edit',
                   'expected_revision': state['revision'], 'editor': 'missoula', 'payload': {}}
        real_call = managed_events._call

        def lose_response(*args):
            real_call(*args)
            raise ManagedEventError('Synthetic committed forecast response loss')

        monkeypatch.setattr(managed_events, '_call', lose_response)
        with pytest.raises(ManagedEventError, match='response loss'):
            managed_events.submit(event, user, request)
        db.session.rollback()
        command = ManagedCommand.query.filter_by(request_id=request['request_id']).one()
        assert command.status == 'pending'
        ProCompetitor.query.one().events_entered = '[]'
        db.session.commit()
        monkeypatch.setattr(managed_events, '_call', real_call)
        recovered = managed_events.recover_command(command.id, user)
        assert recovered['revision'] == 2 and command.status == 'accepted'
        assert managed_events.submit(event, user, request) == recovered


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_unissued_configuration_reconciles_new_host_roster_mapping(tmp_path):
    from copy import deepcopy

    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        state = managed_events.bind_event(event, user, config)
        entrant = make_pro_competitor(db.session, event.tournament, 'Synthetic new entrant',
                                      gender='F', events=['Underhand'])
        db.session.commit()
        row = deepcopy(config['roster'][0])
        row.update(entry_id='new-entry', engine_id='synthetic:engine:new-entry',
                   host_ref=f'pro:{entrant.id}', gender='F', gear_group=None)
        config['roster'].append(row)
        updated = managed_events.submit(event, user, {
            'operation': 'configure', 'request_id': 'reconcile-new-entry', 'expected_revision': state['revision'],
            'editor': 'missoula', 'payload': {'config': config}})
        assert updated['revision'] == 2
        assert json.loads(event.managed_operation.mapping_json)['new-entry'] == entrant.id
        assert managed_events.submit(event, user, {'operation': 'forecast', 'request_id': 'new-roster-forecast',
            'expected_revision': 2, 'editor': 'missoula', 'payload': {}})['revision'] == 3


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_shared_client_cannot_import_a_different_host_timer_policy(tmp_path):
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        state = managed_events.bind_event(event, user, config)
        config['timer_policy'] = 'single'
        # Another presentation can use general component configuration, but
        # this host must refuse an incompatible show profile before importing.
        managed_events._call(user, event.managed_operation, {
            'operation': 'configure', 'event_id': state['event_id'], 'request_id': 'other-client-config',
            'expected_revision': 1, 'editor': 'missoula', 'payload': {'config': config}})
        with pytest.raises(ManagedEventError, match='dual timer policy'):
            managed_events.submit(event, user, {'operation': 'export'})
        db.session.rollback()
        assert json.loads(event.managed_operation.state_json)['revision'] == 1
