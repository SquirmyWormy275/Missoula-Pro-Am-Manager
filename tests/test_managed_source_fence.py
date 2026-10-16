"""Actual source writer exclusion, host schedule ownership and scoped scratches."""

import json
import os
import queue
import subprocess
import threading

import pytest
from sqlalchemy import text

from database import db
from models import Event, Heat, User
from services.managed_events import ManagedEventError
from tests.conftest import make_flight, make_heat, make_pro_competitor
from tests.test_managed_events import _projection_fixture
from tests.test_managed_host_policy import _binding_fixture


def test_competitor_scoped_scratch_cannot_remove_managed_roster():
    from services.scratch_cascade import ScratchCascadeConflict, execute_cascade

    app, event_id, user_id, _ = _projection_fixture()
    with app.app_context():
        event = db.session.get(Event, event_id)
        entrant = make_pro_competitor(db.session, event.tournament, 'Synthetic cascade entrant')
        event.managed_operation.mapping_json = json.dumps({'entry': entrant.id})
        heat = make_heat(db.session, event, competitors=[entrant.id])
        db.session.commit()
        tid, comp_id, heat_id = event.tournament_id, entrant.id, heat.id
        with pytest.raises(ScratchCascadeConflict, match='STRATHMARK'):
            execute_cascade(entrant, [], user_id, event.tournament)
        db.session.rollback()
        assert db.session.get(Heat, heat_id).get_competitors() == [comp_id]
        assert entrant.status == 'active'
        heat.status = 'completed'
        db.session.commit()
        with pytest.raises(ScratchCascadeConflict, match='STRATHMARK'):
            execute_cascade(entrant, [], user_id, event.tournament)
        db.session.rollback()
    client = app.test_client()
    client.post('/auth/login', data={'username': 'synthetic-projection-director', 'password': 'synthetic-only-password'})
    response = client.post(f'/scoring/{tid}/competitor/{comp_id}/scratch-confirm', data={'competitor_type': 'pro'})
    assert response.status_code == 409 and b'STRATHMARK' in response.data


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_invalid_initial_profile_never_reaches_source(tmp_path, monkeypatch):
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    config['format'] = 'championship'
    def forbidden(*_args):
        pytest.fail('Invalid host profile reached source creation')
    monkeypatch.setattr(managed_events, '_call', forbidden)
    with app.app_context():
        event = db.session.get(Event, event_id)
        with pytest.raises(ManagedEventError, match='configuration differs'):
            managed_events.bind_event(event, db.session.get(User, user_id), config)
        assert event.managed_operation is None


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_source_change_between_export_and_acceptance_is_refused(tmp_path):
    from models.managed_event import ManagedAcceptance
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        state = managed_events.bind_event(event, user, config)
        exported = managed_events.submit(event, user, {'operation': 'export'})
        managed_events._call(user, event.managed_operation, {'operation': 'handover',
            'event_id': state['event_id'], 'request_id': 'source-changed-after-export',
            'expected_revision': 1, 'editor': 'missoula', 'payload': {'editor': 'gui'}})
        with pytest.raises(ManagedEventError, match='Stale source export'):
            managed_events.accept_export(event, user, exported)
        db.session.rollback()
        assert ManagedAcceptance.query.count() == 0


@pytest.mark.parametrize('outcome', ['commit', 'rollback', 'close', 'savepoint_commit', 'savepoint_rollback'])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_source_writer_waits_for_outer_host_transaction(tmp_path, outcome):
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        state = managed_events.bind_event(event, user, config)
        exported = managed_events.submit(event, user, {'operation': 'export'})
        managed_events.accept_export(event, user, exported)
        guard = db.session.info['managed_source_fence']
        if outcome.startswith('savepoint_'):
            nested = db.session.begin_nested()
            nested.commit() if outcome == 'savepoint_commit' else nested.rollback()
            assert guard.closed is False
        code = """
import json,sys
from strathmark.events.client import local_request
from strathmark.events.operations import EventOperations
print('READY',flush=True)
result=local_request(EventOperations(sys.argv[1]),json.loads(sys.stdin.readline()))
print(json.dumps(result),flush=True)
"""
        envelope = {'principal': {'actor_id': user.shadow_actor_id, 'shows': [config['show_id']], 'role': 'director'},
                    'request': {'operation': 'handover', 'event_id': state['event_id'], 'request_id': 'waiting-source-writer',
                                'expected_revision': 1, 'editor': 'missoula', 'payload': {'editor': 'gui'}}}
        executable, database = managed_events._configured_runtime()
        writer = subprocess.Popen([str(executable), '-I', '-c', code, str(database)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=managed_events._runtime_environment(database))
        try:
            writer.stdin.write(json.dumps(envelope) + '\n')
            writer.stdin.close()
            output = queue.Queue()
            def lines():
                output.put(writer.stdout.readline())
                output.put(writer.stdout.readline())
            threading.Thread(target=lines, daemon=True).start()
            assert output.get(timeout=15).strip() == 'READY'
            with pytest.raises(queue.Empty):
                output.get(timeout=.2)
            with db.engine.connect() as independent:
                assert independent.execute(text('SELECT count(*) FROM managed_acceptances')).scalar() == 0
            if outcome == 'rollback':
                db.session.rollback()
            elif outcome == 'close':
                db.session.remove()
            else:
                db.session.commit()
            assert guard.closed is True
            assert json.loads(output.get(timeout=15))['revision'] == 2
            assert writer.wait(timeout=15) == 0
            with db.engine.connect() as independent:
                assert independent.execute(text('SELECT count(*) FROM managed_acceptances')).scalar() == (0 if outcome in {'rollback', 'close'} else 1)
        finally:
            db.session.rollback()
            if writer.poll() is None:
                writer.kill()
                writer.wait(timeout=5)
            writer.stdout.close()
            writer.stderr.close()


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_lost_source_fence_refuses_host_commit(tmp_path):
    from models.managed_event import ManagedAcceptance
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        managed_events.bind_event(event, user, config)
        exported = managed_events.submit(event, user, {'operation': 'export'})
        managed_events.accept_export(event, user, exported)
        guard = db.session.info['managed_source_fence']
        guard.process.kill()
        guard.process.wait(timeout=5)
        with pytest.raises(ManagedEventError, match='Source fence was lost'):
            db.session.commit()
        db.session.rollback()
        assert ManagedAcceptance.query.count() == 0
        assert managed_events.submit(event, user, {'operation': 'read'})['revision'] == 1


@pytest.mark.parametrize('retain_heat_id', [False, True])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_scheduled_field_requires_explicit_unscheduling_before_replacement(tmp_path, retain_heat_id):
    from models.managed_event import ManagedCommand, ManagedFieldLink
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        state = managed_events.bind_event(event, user, config)
        entry = config['roster'][0]['entry_id']
        request = {'operation': 'fields', 'request_id': 'initial-fields', 'expected_revision': state['revision'],
                   'editor': 'missoula', 'payload': {'fields': [{'heat_id': 'initial', 'entries': [entry], 'stands': ['1']}]}}
        state = managed_events.submit(event, user, request)
        heat_id = ManagedFieldLink.query.filter_by(module_heat_id='initial').one().heat_id
        heat = db.session.get(Heat, heat_id)
        flight = make_flight(db.session, event.tournament)
        heat.flight_id, heat.flight_position = flight.id, 1
        db.session.commit()
        new_id = 'initial' if retain_heat_id else 'replacement'
        replacement = {**request, 'request_id': 'replace-fields', 'expected_revision': state['revision'],
                       'payload': {'fields': [{'heat_id': new_id, 'entries': [entry], 'stands': ['2']}]}}
        with pytest.raises(ManagedEventError, match='host-scheduled'):
            managed_events.submit(event, user, replacement)
        db.session.rollback()
        assert db.session.get(Heat, heat_id).flight_id == flight.id
        placement = managed_events.placement_digest(event)
        pending = ManagedCommand.query.filter_by(request_id='replace-fields').one()
        assert pending.status == 'pending'
        with pytest.raises(ManagedEventError, match='confirm'):
            managed_events.release_unissued_placement(event, user, heat_id, '0' * 64, confirm=True)
        db.session.rollback()
        managed_events.release_unissued_placement(event, user, heat_id, placement, confirm=True)
        db.session.commit()
        managed_events.recover_command(pending.id, user)
        replacement_link = ManagedFieldLink.query.filter_by(module_heat_id=new_id).one()
        assert db.session.get(Heat, replacement_link.heat_id).flight_id is None
        assert ManagedFieldLink.query.filter_by(module_heat_id='initial').count() == int(retain_heat_id)
        assert event.heats.count() == 1


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_bulk_payout_configuration_uses_one_fence_for_all_managed_events(tmp_path):
    from copy import deepcopy

    from models import ProCompetitor, WoodConfig
    from models.payout_template import PayoutTemplate
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        first, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        managed_events.bind_event(first, user, config)
        entrant = ProCompetitor.query.one()
        entrant.events_entered = json.dumps(['Underhand', 'Standing Block'])
        second = Event(tournament_id=first.tournament_id, name='Standing Block', event_type='pro',
                       scoring_type='time', is_handicap=True, stand_type='standing_block', max_stands=4)
        db.session.add(second)
        db.session.add(WoodConfig(tournament_id=first.tournament_id, config_key='block_standing_block_pro',
                                 species='S01', size_value=275, size_unit='mm'))
        db.session.flush()
        second_config = deepcopy(config)
        second_config.update(event_id=second.shadow_event_occurrence_id + ':managed', discipline='SB')
        managed_events.bind_event(second, user, second_config)
        template = PayoutTemplate(name='Synthetic batch purse')
        template.set_payouts({'1': 100, '2': 50})
        db.session.add(template)
        db.session.commit()
        tid, second_id, template_id = first.tournament_id, second.id, template.id
    client = app.test_client()
    client.post('/auth/login', data={'username': 'synthetic-projection-director', 'password': 'synthetic-only-password'})
    response = client.post(f'/scoring/{tid}/pro/payout-manager',
        data={'action': 'bulk_apply', 'template_id': template_id, 'event_ids': [event_id, second_id]})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Event, event_id).get_payout_for_position(1) == 100
        assert db.session.get(Event, second_id).get_payout_for_position(1) == 100
