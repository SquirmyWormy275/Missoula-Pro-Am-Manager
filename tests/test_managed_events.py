"""Missoula is the first consumer of the actual shared event operations."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from services.managed_events import ManagedEventError, assert_host_owned


def test_legacy_mutation_refuses_managed_authority():
    class Event:
        operation_mode = "managed"

    with pytest.raises(ManagedEventError, match="managed"):
        assert_host_owned(Event())


def test_retained_mode_remains_independent():
    class Event:
        operation_mode = "host"

    assert_host_owned(Event())


def _projection_fixture(*, linked=True):
    from database import db
    from models import Event, Tournament, User
    from models.managed_event import ManagedEventLink
    from services.managed_events import CONTRACT, canonical
    from tests.db_test_utils import create_test_app

    app, _ = create_test_app(use_migrations=True)
    with app.app_context():
        tournament = Tournament(name='Synthetic projection ordering', year=2027)
        db.session.add(tournament)
        db.session.flush()
        event = Event(tournament_id=tournament.id, name='Underhand', event_type='pro', scoring_type='time')
        user = User(username='synthetic-projection-director', role='admin')
        user.set_password('synthetic-only-password')
        db.session.add_all([event, user])
        db.session.flush()
        state = {'schema_version': CONTRACT, 'event_id': 'synthetic:projection:event',
                 'show_id': 'synthetic:projection', 'revision': 1, 'rounds': []}
        if linked:
            db.session.add(ManagedEventLink(event=event, module_event_id=state['event_id'],
                                          show_id=state['show_id'], mapping_json='{}', state_json=canonical(state)))
        db.session.commit()
        return app, event.id, user.id, state


@pytest.mark.parametrize('operation', ['read', 'handover'])
def test_delayed_component_response_cannot_replace_newer_host_projection(monkeypatch, operation):
    from database import db
    from models import Event, User
    from models.managed_event import ManagedCommand, ManagedEventLink
    from services import managed_events

    app, event_id, user_id, initial = _projection_fixture()
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        newer = {**initial, 'revision': 3}
        delayed = {**initial, 'revision': 2}

        def response_after_other_request_commits(_user, link, _request):
            # This request has already loaded its ORM link when another request
            # commits the later correction. The component's older response then
            # arrives; its own receipt remains valid but cannot regress cache.
            assert json.loads(link.state_json)['revision'] == 1
            with db.engine.begin() as connection:
                connection.execute(ManagedEventLink.__table__.update().where(
                    ManagedEventLink.event_id == event_id).values(state_json=managed_events.canonical(newer)))
            return delayed

        monkeypatch.setattr(managed_events, '_call', response_after_other_request_commits)
        request = {'operation': operation, 'request_id': 'synthetic-delayed-response',
                   'expected_revision': 1, 'editor': 'missoula', 'payload': {'editor': 'gui'}}
        assert managed_events.submit(event, user, request) == delayed
        db.session.expire_all()
        assert json.loads(db.session.get(ManagedEventLink, event_id).state_json) == newer
        if operation != 'read':
            assert json.loads(ManagedCommand.query.one().response_json) == delayed


def test_acceptance_reloads_link_after_parent_lock():
    from database import db
    from models import Event, User
    from models.managed_event import ManagedAcceptance, ManagedEventLink
    from services.managed_events import ManagedEventError, accept_export

    app, event_id, user_id, initial = _projection_fixture()
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        assert event.managed_operation.accepted_revision == 0
        # Another request committed while this one retained an older ORM link.
        with db.engine.begin() as connection:
            connection.execute(ManagedEventLink.__table__.update().where(
                ManagedEventLink.event_id == event_id).values(accepted_revision=3))
        exported = {'schema_version': initial['schema_version'], 'event_id': initial['event_id'],
                    'show_id': initial['show_id'], 'synthetic': True, 'official_use': False,
                    'event_revision': 2, 'deliveries': [], 'status': 'active'}
        with pytest.raises(ManagedEventError, match='Stale managed export'):
            accept_export(event, user, exported)
        assert ManagedAcceptance.query.count() == 0
        db.session.rollback()
        assert db.session.get(ManagedEventLink, event_id).accepted_revision == 3


def test_export_older_than_known_correction_cannot_be_accepted():
    from database import db
    from models import Event, User
    from models.managed_event import ManagedAcceptance
    from services.managed_events import accept_export, canonical

    app, event_id, user_id, initial = _projection_fixture()
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        event.managed_operation.state_json = canonical({**initial, 'revision': 3})
        db.session.commit()
        exported = {'schema_version': initial['schema_version'], 'event_id': initial['event_id'],
                    'show_id': initial['show_id'], 'synthetic': True, 'official_use': False,
                    'event_revision': 2, 'deliveries': [], 'status': 'active'}
        with pytest.raises(ManagedEventError, match='Stale managed export'):
            accept_export(event, user, exported)
        assert ManagedAcceptance.query.count() == 0
        assert event.managed_operation.accepted_revision == 0


def test_awards_reload_current_source_after_parent_lock(monkeypatch):
    from database import db
    from models import Event, User
    from models.managed_event import ManagedEventLink
    from services.managed_events import apply_host_awards, canonical

    app, event_id, user_id, initial = _projection_fixture()
    install_projection_guard(monkeypatch)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        link = event.managed_operation
        link.state_json = canonical({**initial, 'status': 'finalized', 'summary_revision': 1})
        link.accepted_revision = link.summary_revision = 1
        db.session.commit()
        assert json.loads(link.state_json)['status'] == 'finalized'
        with db.engine.begin() as connection:
            connection.execute(ManagedEventLink.__table__.update().where(
                ManagedEventLink.event_id == event_id).values(state_json=canonical(
                    {**initial, 'revision': 3, 'status': 'active', 'summary_revision': 1})))
        with pytest.raises(ManagedEventError, match='Accept the current finalized'):
            apply_host_awards(event, user)
        assert event.is_finalized is False


def install_projection_guard(monkeypatch):
    """Policy-only fixtures have no numerical peer; real fixtures keep theirs."""
    from types import SimpleNamespace

    from models.managed_event import ManagedEventLink
    from services import managed_events

    real_guard = managed_events.guard_managed_awards
    real_context = managed_events._validate_host_context

    def guard(events, user):
        if events and all(e.managed_operation and e.managed_operation.module_event_id == 'synthetic:projection:event' for e in events):
            states = {e.managed_operation.module_event_id: json.loads(
                ManagedEventLink.query.filter_by(event_id=e.id).populate_existing().one().state_json) for e in events}
            return SimpleNamespace(states=states)
        return real_guard(events, user)

    monkeypatch.setattr(managed_events, 'guard_managed_awards', guard)

    def context(event, state, mapping):
        if event.managed_operation.module_event_id != 'synthetic:projection:event':
            return real_context(event, state, mapping)

    monkeypatch.setattr(managed_events, '_validate_host_context', context)


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires explicit separately installed managed-event component')
def test_real_component_multiround_acceptance_and_retry(tmp_path):
    from database import db
    from models import Event, EventResult, ProCompetitor, Tournament, User, WoodConfig
    from models.managed_event import ManagedAttemptRevision, ManagedCareerDelivery, ManagedCommand
    from services.managed_events import accept_export, apply_host_awards, bind_event, submit
    from tests.db_test_utils import create_test_app

    interpreter = os.environ['PROAM_MANAGED_TEST_PYTHON']
    example = subprocess.run([interpreter, '-I', '-c', 'import json; from strathmark.events.examples import synthetic_event; print(json.dumps(synthetic_event()))'], text=True, capture_output=True, check=True)
    config = json.loads(example.stdout)
    app, database_path = create_test_app(use_migrations=True)
    app.config.update({'MANAGED_EVENTS_ENABLED': True, 'MANAGED_EVENTS_PYTHON': interpreter, 'MANAGED_EVENTS_DATABASE': str(tmp_path / 'authority.sqlite3')})
    with app.app_context():
        tournament = Tournament(name='Synthetic modular Missoula', year=2027)
        db.session.add(tournament)
        db.session.flush()
        event = Event(tournament_id=tournament.id, name='Underhand', event_type='pro', scoring_type='time', is_handicap=True, stand_type='underhand', max_stands=4)
        db.session.add(event)
        db.session.add(WoodConfig(tournament_id=tournament.id, config_key='block_underhand_pro', species='S01', size_value=275, size_unit='mm'))
        user = User(username='synthetic-director', role='admin')
        user.set_password('synthetic-only-password')
        db.session.add(user)
        db.session.flush()
        for i, row in enumerate(config['roster']):
            competitor = ProCompetitor(tournament_id=tournament.id, name=f'Synthetic Cutter {i}', gender=row['gender'], events_entered=json.dumps(['Underhand']))
            db.session.add(competitor)
            db.session.flush()
            row['host_ref'] = f'pro:{competitor.id}'
        db.session.commit()
        config['event_id'] = event.shadow_event_occurrence_id + ':managed'
        config['tournament_id'] = tournament.shadow_tournament_id
        state = bind_event(event, user, config)
        assert event.operation_mode == 'managed'
        from services.managed_events import workspace_url
        app.config['MANAGED_WORKSPACE_ORIGIN'] = 'http://127.0.0.1:8789'
        assert workspace_url(event).startswith('http://127.0.0.1:8789/events/')

        # A selected component outage retains the host intent and accepted
        # projection. The retained 2.x library must not become a fallback.
        outage_request = {'operation': 'forecast', 'request_id': 'synthetic-component-outage',
                          'expected_revision': state['revision'], 'editor': 'missoula', 'payload': {}}
        app.config['MANAGED_EVENTS_PYTHON'] = str(tmp_path / 'unavailable-runtime')
        try:
            with pytest.raises(ManagedEventError, match='explicit installed'):
                submit(event, user, outage_request)
            db.session.rollback()
            pending = ManagedCommand.query.filter_by(event_id=event.id, request_id=outage_request['request_id']).one()
            assert pending.status == 'pending'
            assert json.loads(event.managed_operation.state_json)['revision'] == state['revision']
            other = Tournament(name='Synthetic independent registration during component outage', year=2027)
            db.session.add(other)
            db.session.flush()
            entrant = ProCompetitor(tournament_id=other.id, name='Synthetic Unrelated Entry', gender='F', events_entered='[]')
            db.session.add(entrant)
            db.session.commit()
            assert db.session.get(ProCompetitor, entrant.id).name == 'Synthetic Unrelated Entry'
        finally:
            app.config['MANAGED_EVENTS_PYTHON'] = interpreter
        from services.managed_events import recover_command
        state = recover_command(pending.id, user)
        assert pending.status == 'accepted' and state['revision'] == outage_request['expected_revision'] + 1

        def command(action, payload=None, request_id=None, supplied=None):
            nonlocal state
            request = supplied or {'operation': action, 'request_id': request_id or str(uuid.uuid4()), 'expected_revision': state['revision'], 'editor': 'missoula', 'payload': payload or {}}
            state = submit(event, user, request)
            return request

        eligible = [row['entry_id'] for row in config['roster']]
        for ordinal, rule in enumerate(config['round_rules']):
            capacity = rule['capacity']
            fields = [{'heat_id': f'r{ordinal}h{n//capacity}', 'entries': eligible[n:n+capacity], 'stands': config['stands'][:len(eligible[n:n+capacity])]} for n in range(0, len(eligible), capacity)]
            command('fields', {'fields': fields})
            for field in fields:
                command('calculate', {'heat_id': field['heat_id']})
                heat = next(h for h in state['rounds'][-1]['fields'] if h['heat_id'] == field['heat_id'])
                command('review', {'heat_id': heat['heat_id'], 'sheet_digest': heat['sheet']['digest']})
                command('issue', {'heat_id': heat['heat_id'], 'sheet_digest': heat['sheet']['digest'], 'confirm': True})
                heat = next(h for h in state['rounds'][-1]['fields'] if h['heat_id'] == field['heat_id'])
                rows = [{'entry_id': r['entry_id'], 'status': 'finished', 'timer1_seconds': str(100+i-r['start_mark_seconds']), 'timer2_seconds': str(100+i-r['start_mark_seconds'])} for i, r in enumerate(heat['sheet']['rows'])]
                command('outcomes', {'heat_id': heat['heat_id'], 'issue_id': heat['issue_id'], 'outcomes': rows})
                heat = next(h for h in state['rounds'][-1]['fields'] if h['heat_id'] == field['heat_id'])
                request = command('accept', {'heat_id': heat['heat_id'], 'result_digest': heat['result_revisions'][-1]['digest']})
                assert submit(event, user, request) == state
            if ordinal < 2:
                command('advance')
                eligible = state['rounds'][-1]['advancing']
        command('finalize')
        exported = submit(event, user, {'operation': 'export'})
        receipt = accept_export(event, user, exported)
        db.session.commit()
        assert accept_export(event, user, exported).id == receipt.id
        assert ManagedAttemptRevision.query.count() == 14
        assert ManagedCareerDelivery.query.filter_by(status='pending').count() == 14
        assert len(event.results.all()) == 8
        positions = {row.competitor_id: row.final_position for row in event.results.all()}
        apply_host_awards(event, user)
        db.session.commit()
        assert event.is_finalized
        assert positions == {row.competitor_id: row.final_position for row in event.results.all()}
        assert ManagedCommand.query.filter_by(status='pending').count() == 0
        # Accepting editor-only revisions preserves the same host award state.
        command('handover', {'editor': 'gui'})
        metadata_export = submit(event, user, {'operation': 'export'})
        accept_export(event, user, metadata_export)
        db.session.commit()
        assert event.is_finalized
        command('handover', supplied={'operation': 'handover', 'request_id': 'return-after-metadata-proof',
            'expected_revision': state['revision'], 'editor': 'gui', 'payload': {'editor': 'missoula'}})
        accept_export(event, user, submit(event, user, {'operation': 'export'}))
        db.session.commit()
        assert event.is_finalized
        from services.scoring_engine import calculate_positions

        with pytest.raises(ManagedEventError):
            calculate_positions(event)
        receiver_python = os.environ.get('PROAM_MNEMEX_TEST_PYTHON')
        if receiver_python:
            _real_mnemex_delivery(event, user, app, tmp_path, command, submit, accept_export, receiver_python)


def _real_mnemex_delivery(event, user, app, tmp_path, command, submit, accept_export, receiver_python):
    import requests

    from database import db
    from models.managed_event import ManagedAttemptRevision, ManagedCareerDelivery
    from services.managed_events import _call, apply_host_awards, deliver_career_results

    # Append a real source correction; old attempts and the host's previous
    # awards remain retained until explicit host acceptance/award actions.
    state = submit(event, user, {'operation': 'read'})
    heat = state['rounds'][-1]['fields'][0]
    result = heat['result_revisions'][-1]
    rows = [{'entry_id': r['entry_id'], 'status': 'dq'} for r in result['outcomes']]
    correction = {'operation': 'correct', 'request_id': 'external-client-correction',
                  'event_id': state['event_id'], 'expected_revision': state['revision'], 'editor': 'missoula',
                  'payload': {'heat_id': heat['heat_id'], 'issue_id': heat['issue_id'], 'outcomes': rows,
                              'reason': 'Synthetic source voids', 'supersedes': result['digest']}}
    _call(user, event.managed_operation, correction)
    with pytest.raises(ManagedEventError, match='current finalized'):
        apply_host_awards(event, user)
    db.session.rollback()
    command('correct', supplied=correction)
    state = submit(event, user, {'operation': 'read'})
    result = state['rounds'][-1]['fields'][0]['result_revisions'][-1]
    command('accept', {'heat_id': heat['heat_id'], 'result_digest': result['digest']})
    command('finalize')
    exported = submit(event, user, {'operation': 'export'})
    accept_export(event, user, exported)
    db.session.commit()
    assert ManagedAttemptRevision.query.count() == 16
    root = tmp_path/'actual-mnemex-receiver'
    server = subprocess.Popen([receiver_python, str(Path(__file__).with_name('mnemex_receiver_process.py')), str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic()+30
        while not (root/'ready.json').exists() and time.monotonic()<deadline and server.poll() is None:
            time.sleep(.05)
        assert (root/'ready.json').exists(), server.communicate(timeout=1)
        config = json.loads((root/'ready.json').read_text())
        app.config.update(MNEMEX_SHOW_RESULTS_URL=config['url'], MNEMEX_SHOW_RESULTS_TOKEN=config['token'])
        def outage(*_args, **_kwargs):
            raise requests.ConnectionError('synthetic outage')
        assert deliver_career_results(event, user, transport=outage)['remaining_pending'] == 16
        lost = [False]
        session = requests.Session()
        session.trust_env = False
        def response_loss(*args, **kwargs):
            response = session.post(*args, **kwargs)
            if not lost[0]:
                lost[0] = True
                raise requests.Timeout('synthetic committed response loss')
            return response
        delivered = deliver_career_results(event, user, transport=response_loss)
        assert delivered['remaining_pending'] == 1
        assert deliver_career_results(event, user)['remaining_pending'] == 0
        status = session.post(config['url'].replace('/ingest','/status'), headers={'Authorization':'Bearer '+config['token']}, timeout=5).json()
        assert status['source_revision_count'] == 16
        assert status['stream_count'] == 14
        assert status['conflict_count'] == 0
        assert all(row['current_revision'] in {1,2} and row['identity_state'] == 'unlinked' for row in status['streams'])
        assert ManagedCareerDelivery.query.filter_by(status='accepted').count() == 16
        assert all(row.receipt_json for row in ManagedCareerDelivery.query.all())
        session.close()
    finally:
        server.terminate()
        server.communicate(timeout=15)
        (root/'ready.json').unlink(missing_ok=True)


@pytest.mark.parametrize('response', [[], None, 'invalid', {'ok': True, 'result': []}, {'ok': True, 'result': None}])
def test_nonobject_local_rpc_response_is_a_managed_error(tmp_path, monkeypatch, response):
    from types import SimpleNamespace

    from database import db
    from models import Event, User
    from services import managed_events

    app, event_id, user_id, _ = _projection_fixture()
    with app.app_context():
        monkeypatch.setattr(managed_events, '_configured_runtime', lambda: (Path(sys.executable), tmp_path / 'authority.sqlite3'))
        monkeypatch.setattr(managed_events.subprocess, 'run', lambda *_args, **_kwargs:
                            SimpleNamespace(returncode=0, stdout=json.dumps(response)))
        with pytest.raises(ManagedEventError, match='response'):
            managed_events._call(db.session.get(User, user_id), db.session.get(Event, event_id).managed_operation,
                                 {'operation': 'read', 'event_id': 'synthetic:projection:event'})
