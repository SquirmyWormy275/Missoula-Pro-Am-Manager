"""Actual source writer exclusion, host schedule ownership and scoped scratches."""

import json
import os
import queue
import subprocess
import threading

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import text

from database import db
from models import Event, Heat, User
from services.managed_events import ManagedEventError
from tests.conftest import make_flight, make_heat, make_pro_competitor
from tests.test_managed_events import _projection_fixture
from tests.test_managed_host_policy import _binding_fixture


def _single_entry_final(event, user, config, *, state=None):
    from services import managed_events

    if state is None:
        state = managed_events.bind_event(event, user, config)

    def command(operation, payload=None):
        nonlocal state
        state = managed_events.submit(event, user, {
            'operation': operation, 'request_id': f'single-final-{operation}',
            'expected_revision': state['revision'], 'editor': 'missoula', 'payload': payload or {}})

    entry = config['roster'][0]['entry_id']
    command('fields', {'fields': [{'heat_id': 'single-final', 'entries': [entry], 'stands': ['1']}]})
    command('calculate', {'heat_id': 'single-final'})
    field = state['rounds'][-1]['fields'][0]
    command('review', {'heat_id': 'single-final', 'sheet_digest': field['sheet']['digest']})
    command('issue', {'heat_id': 'single-final', 'sheet_digest': field['sheet']['digest'], 'confirm': True})
    field = state['rounds'][-1]['fields'][0]
    command('outcomes', {'heat_id': 'single-final', 'issue_id': field['issue_id'], 'outcomes': [
        {'entry_id': entry, 'status': 'finished', 'timer1_seconds': '20', 'timer2_seconds': '20'}]})
    field = state['rounds'][-1]['fields'][0]
    command('accept', {'heat_id': 'single-final', 'result_digest': field['result_revisions'][-1]['digest']})
    command('finalize')
    return state


@pytest.mark.parametrize('change', ['add', 'remove'])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_awards_recheck_registration_after_accepted_summary(tmp_path, change):
    from models import ProCompetitor
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        _single_entry_final(event, user, config)
        managed_events.accept_export(event, user, managed_events.submit(event, user, {'operation': 'export'}))
        event.set_payouts({'1': 100})
        db.session.commit()
        entrant = ProCompetitor.query.one()
        if change == 'add':
            make_pro_competitor(db.session, event.tournament, 'Synthetic post-acceptance entrant', events=['Underhand'])
            db.session.commit()
        else:
            with db.engine.begin() as connection:
                connection.execute(ProCompetitor.__table__.update().where(
                    ProCompetitor.id == entrant.id).values(events_entered='[]'))
        with pytest.raises(ManagedEventError, match='active entered competitor'):
            managed_events.apply_host_awards(event, user)
        db.session.rollback()
        assert not db.session.get(Event, event_id).is_finalized
        assert db.session.get(Event, event_id).results.one().payout_amount == 0


@pytest.mark.parametrize('history', [False, True])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_unissued_roster_shrink_removes_only_empty_pending_host_result(tmp_path, history):
    from copy import deepcopy

    from models import EventResult
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        extra = make_pro_competitor(db.session, event.tournament, 'Synthetic withdrawn entrant',
                                    gender=config['roster'][0]['gender'], events=['Underhand'])
        config['roster'].append({**config['roster'][0], 'entry_id': 'withdrawn-entry',
                                'engine_id': 'synthetic:engine:withdrawn', 'host_ref': f'pro:{extra.id}'})
        db.session.commit()
        state = managed_events.bind_event(event, user, config)
        row = EventResult.query.filter_by(event_id=event.id, competitor_id=extra.id).one()
        if history:
            row.t1_run1 = 0  # Even a recorded zero is scoring history.
        extra.events_entered = '[]'
        db.session.commit()
        revised = deepcopy(config)
        revised['roster'] = revised['roster'][:1]
        request = {'operation': 'configure', 'request_id': 'shrink-before-issue',
                   'expected_revision': state['revision'], 'editor': 'missoula', 'payload': {'config': revised}}
        if history:
            with pytest.raises(ManagedEventError, match='history'):
                managed_events.submit(event, user, request)
            db.session.rollback()
            assert event.results.count() == 2
            assert EventResult.query.filter_by(event_id=event.id, competitor_id=extra.id).one().t1_run1 == 0
            assert len(json.loads(event.managed_operation.mapping_json)) == 2
        else:
            state = managed_events.submit(event, user, request)
            assert event.results.count() == 1
            _single_entry_final(event, user, revised, state=state)
            managed_events.accept_export(event, user, managed_events.submit(event, user, {'operation': 'export'}))
            managed_events.apply_host_awards(event, user)
            db.session.commit()
            assert event.is_finalized and event.results.count() == 1
            assert event.results.one().status == 'completed'


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_shared_editor_wood_only_change_cannot_project_host_fields(tmp_path):
    from copy import deepcopy

    from models.managed_event import ManagedFieldLink
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        state = managed_events.bind_event(event, user, config)
        changed = deepcopy(config)
        changed['wood']['diameter_mm'] = '280'
        state = managed_events._call(user, event.managed_operation, {
            'operation': 'configure', 'event_id': state['event_id'], 'request_id': 'shared-editor-wood-only',
            'expected_revision': state['revision'], 'editor': 'missoula', 'payload': {'config': changed}})
        managed_events._call(user, event.managed_operation, {
            'operation': 'fields', 'event_id': state['event_id'], 'request_id': 'shared-editor-wood-fields',
            'expected_revision': state['revision'], 'editor': 'missoula', 'payload': {'fields': [
                {'heat_id': 'foreign-wood-field', 'entries': [config['roster'][0]['entry_id']], 'stands': ['1']}]}})
        before = event.managed_operation.state_json
        with pytest.raises(ManagedEventError, match='wood differs'):
            managed_events.submit(event, user, {'operation': 'read'})
        db.session.rollback()
        assert event.managed_operation.state_json == before
        assert event.heats.count() == ManagedFieldLink.query.count() == 0


@pytest.mark.parametrize('change', ['add', 'remove'])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_acceptance_rechecks_current_registration_with_unchanged_source_roster(tmp_path, change):
    from models import ProCompetitor
    from models.managed_event import ManagedAcceptance, ManagedAttemptRevision
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        _single_entry_final(event, user, config)
        entrant = ProCompetitor.query.one()
        assert entrant.get_events_entered() == ['Underhand']
        if change == 'add':
            make_pro_competitor(db.session, event.tournament, 'Synthetic late registration', events=['Underhand'])
            db.session.commit()
        else:
            # Keep this request's already loaded competitor stale while an
            # independent registration transaction removes its event entry.
            with db.engine.begin() as connection:
                connection.execute(ProCompetitor.__table__.update().where(
                    ProCompetitor.id == entrant.id).values(events_entered='[]'))
        exported = managed_events.submit(event, user, {'operation': 'export'})
        with pytest.raises(ManagedEventError, match='active entered competitor'):
            managed_events.accept_export(event, user, exported)
        db.session.rollback()
        assert ManagedAcceptance.query.count() == ManagedAttemptRevision.query.count() == 0
        assert db.session.get(Event, event_id).status != 'completed'


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_accepted_export_retry_survives_later_registration_edit(tmp_path):
    from models import ProCompetitor
    from models.managed_event import ManagedAcceptance
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        managed_events.bind_event(event, user, config)
        exported = managed_events.submit(event, user, {'operation': 'export'})
        receipt = managed_events.accept_export(event, user, exported)
        db.session.commit()
        ProCompetitor.query.one().events_entered = '[]'
        db.session.commit()
        assert managed_events.accept_export(event, user, exported).id == receipt.id
        assert ManagedAcceptance.query.count() == 1


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_nonfinal_correction_acceptance_reopens_host_awards_until_refinalized(tmp_path):
    from models.managed_event import ManagedAcceptance, ManagedAttemptRevision
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        state = _single_entry_final(event, user, config)
        event.set_payouts({'1': 100})
        db.session.commit()
        managed_events.accept_export(event, user, managed_events.submit(event, user, {'operation': 'export'}))
        db.session.commit()
        managed_events.apply_host_awards(event, user)
        db.session.commit()
        assert event.is_finalized and event.status == 'completed'
        assert event.results.one().payout_amount == 100
        field = state['rounds'][-1]['fields'][0]
        state = managed_events.submit(event, user, {
            'operation': 'correct', 'request_id': 'single-final-correction', 'expected_revision': state['revision'],
            'editor': 'missoula', 'payload': {'heat_id': field['heat_id'], 'issue_id': field['issue_id'],
                'outcomes': [{'entry_id': config['roster'][0]['entry_id'], 'status': 'dq'}],
                'reason': 'Synthetic correction before final review', 'supersedes': field['result_revisions'][-1]['digest']}})
        assert state['status'] != 'finalized'
        exported = managed_events.submit(event, user, {'operation': 'export'})
        managed_events.accept_export(event, user, exported)
        db.session.commit()
        assert not event.is_finalized and event.status == 'in_progress'
        assert event.managed_operation.summary_revision == 0
        assert ManagedAcceptance.query.count() == 2
        # The corrected outcomes are retained in source state, but only become
        # a career attempt revision after the source judge accepts them.
        assert ManagedAttemptRevision.query.count() == 1
        with pytest.raises(ManagedEventError, match='finalized'):
            managed_events.apply_host_awards(event, user)
        db.session.rollback()
        field = state['rounds'][-1]['fields'][0]
        state = managed_events.submit(event, user, {'operation': 'accept', 'request_id': 'accept-corrected-final',
            'expected_revision': state['revision'], 'editor': 'missoula',
            'payload': {'heat_id': field['heat_id'], 'result_digest': field['result_revisions'][-1]['digest']}})
        managed_events.submit(event, user, {'operation': 'finalize', 'request_id': 'refinalize-corrected-final',
            'expected_revision': state['revision'], 'editor': 'missoula', 'payload': {}})
        managed_events.accept_export(event, user, managed_events.submit(event, user, {'operation': 'export'}))
        db.session.commit()
        assert ManagedAttemptRevision.query.count() == 2
        assert not event.is_finalized and event.status == 'completed'
        assert event.results.one().status == 'dq' and event.results.one().payout_amount == 100
        managed_events.apply_host_awards(event, user)
        db.session.commit()
        assert event.is_finalized and event.results.one().payout_amount == 0


@pytest.mark.parametrize('through_route', [False, True])
@pytest.mark.parametrize('issued', [False, True])
def test_registration_gear_sync_preserves_managed_fields_and_repairs_host_fields(through_route, issued):
    from models.managed_event import ManagedFieldLink
    from services.gear_sharing import fix_heat_gear_conflicts
    from tests.conftest import make_event

    app, event_id, _, _ = _projection_fixture()
    with app.app_context():
        event = db.session.get(Event, event_id)
        host = make_event(db.session, event.tournament, 'Synthetic host gear event')
        entrants = [make_pro_competitor(db.session, event.tournament, f'Synthetic gear entrant {i}',
                                       events=[event.id, host.id]) for i in range(3)]
        for first, second in [(entrants[0], entrants[1]), (entrants[1], entrants[0])]:
            first.gear_sharing = json.dumps({str(e.id): second.name for e in (event, host)})
        managed_heats = [make_heat(db.session, event, heat_number=1, competitors=[e.id for e in entrants[:2]]),
                         make_heat(db.session, event, heat_number=2, competitors=[entrants[2].id])]
        host_heats = [make_heat(db.session, host, heat_number=1, competitors=[e.id for e in entrants[:2]]),
                      make_heat(db.session, host, heat_number=2, competitors=[entrants[2].id])]
        db.session.add(ManagedFieldLink(event_id=event.id, module_heat_id='protected-gear-field',
            heat_id=managed_heats[0].id, field_digest='a' * 64, issue_id='b' * 64 if issued else None))
        db.session.commit()
        before = {h.id: (h.get_competitors(), h.get_stand_assignments()) for h in managed_heats}
        tid, host_ids = event.tournament_id, [h.id for h in host_heats]
        if not through_route:
            result = fix_heat_gear_conflicts(event.tournament)
            assert result['fixed'] == 1 and result['managed_events_skipped'] == [event.id]
            db.session.commit()
    if through_route:
        client = app.test_client()
        client.post('/auth/login', data={'username': 'synthetic-projection-director', 'password': 'synthetic-only-password'})
        response = client.post(f'/registration/{tid}/pro/gear-sharing/sync-heats')
        assert response.status_code == 302
        with client.session_transaction() as session:
            assert any('STRATHMARK' in message for _category, message in session['_flashes'])
    with app.app_context():
        assert {hid: (db.session.get(Heat, hid).get_competitors(), db.session.get(Heat, hid).get_stand_assignments())
                for hid in before} == before
        assert [len(db.session.get(Heat, hid).get_competitors()) for hid in host_ids] == [1, 2]


@pytest.mark.parametrize('delete_team', [False, True])
@pytest.mark.parametrize('issued', [False, True])
def test_registration_deletion_preserves_pending_managed_college_mapping(delete_team, issued):
    from models import CollegeCompetitor, Team
    from models.managed_event import ManagedFieldLink
    from tests.conftest import make_college_competitor, make_team

    app, event_id, _, _ = _projection_fixture()
    with app.app_context():
        event = db.session.get(Event, event_id)
        event.event_type = 'college'
        team = make_team(db.session, event.tournament)
        entrant = make_college_competitor(db.session, event.tournament, team, 'Synthetic protected registration')
        independent_team = make_team(db.session, event.tournament, code='INDEP')
        independent = make_college_competitor(db.session, event.tournament, independent_team, 'Synthetic independent registration')
        event.managed_operation.mapping_json = json.dumps({'mapped-entry': entrant.id})
        heat_id = None
        if issued:
            heat = make_heat(db.session, event, competitors=[entrant.id])
            heat_id = heat.id
            db.session.add(ManagedFieldLink(event_id=event.id, module_heat_id='issued-delete-guard',
                heat_id=heat.id, field_digest='a' * 64, issue_id='b' * 64))
        db.session.commit()
        tid, cid, team_id = event.tournament_id, entrant.id, team.id
        independent_id, independent_team_id = independent.id, independent_team.id
    client = app.test_client()
    client.post('/auth/login', data={'username': 'synthetic-projection-director', 'password': 'synthetic-only-password'})
    suffix = f'team/{team_id}' if delete_team else f'competitor/{cid}'
    assert client.post(f'/registration/{tid}/college/{suffix}/delete').status_code == 302
    with app.app_context():
        assert db.session.get(CollegeCompetitor, cid) is not None
        assert db.session.get(Team, team_id) is not None
        assert json.loads(db.session.get(Event, event_id).managed_operation.mapping_json) == {'mapped-entry': cid}
        if issued:
            assert db.session.get(Heat, heat_id).get_competitors() == [cid]
    suffix = f'team/{independent_team_id}' if delete_team else f'competitor/{independent_id}'
    assert client.post(f'/registration/{tid}/college/{suffix}/delete').status_code == 302
    with app.app_context():
        assert db.session.get(CollegeCompetitor, independent_id) is None
        assert db.session.get(CollegeCompetitor, cid) is not None
        if delete_team:
            assert db.session.get(Team, independent_team_id) is None


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


@pytest.mark.parametrize('outcome', ['commit', 'rollback', 'close', 'savepoint_commit', 'savepoint_rollback', 'midcommit_helper_exit'])
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
            if outcome == 'midcommit_helper_exit':
                def after_before_commit(_connection):
                    # Engine commit dispatch is after Session.before_commit
                    # and before the DBAPI commit. The helper may disappear;
                    # the host-owned source mutex must still exclude writers.
                    guard.process.kill()
                    guard.process.wait(timeout=5)
                    with pytest.raises(queue.Empty):
                        output.get(timeout=1)
                sa_event.listen(db.engine, 'commit', after_before_commit, once=True)
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
        guard.source_connection.close()
        with pytest.raises(ManagedEventError, match='Source fence was lost'):
            db.session.commit()
        db.session.rollback()
        assert ManagedAcceptance.query.count() == 0
        assert managed_events.submit(event, user, {'operation': 'read'})['revision'] == 1


@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_binding_waits_for_schedule_writer_and_rechecks_freshness(tmp_path, monkeypatch):
    from models.managed_event import ManagedEventLink
    from services import managed_events
    from services.flight_builder import lock_tournament_schedule, sqlite_schedule_writer_guard

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        tid = db.session.get(Event, event_id).tournament_id
    schedule_locked, generate, generated = threading.Event(), threading.Event(), threading.Event()
    binding_started, permit_bind = threading.Event(), threading.Event()
    results = queue.Queue()
    real_wood = managed_events._validate_show_wood

    def pause_after_freshness(*args):
        assert permit_bind.wait(timeout=15)
        return real_wood(*args)
    monkeypatch.setattr(managed_events, '_validate_show_wood', pause_after_freshness)

    def generator():
        try:
            with app.app_context(), sqlite_schedule_writer_guard(tid):
                lock_tournament_schedule(tid)
                event = db.session.get(Event, event_id)
                assert event.operation_mode == 'host'
                schedule_locked.set()
                assert generate.wait(timeout=15)
                make_heat(db.session, event)
                db.session.commit()
                generated.set()
        except Exception as exc:
            results.put(('generator_error', repr(exc)))

    def binding():
        try:
            with app.app_context():
                event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
                # Warm the relationship before waiting: freshness must reload.
                assert event.managed_operation is None
                binding_started.set()
                try:
                    managed_events.bind_event(event, user, config)
                except ManagedEventError as exc:
                    db.session.rollback()
                    results.put(('binding_refused', str(exc)))
                else:
                    results.put(('binding_accepted', None))
        except Exception as exc:
            results.put(('binding_error', repr(exc)))

    first = threading.Thread(target=generator, daemon=True)
    second = threading.Thread(target=binding, daemon=True)
    first.start()
    assert schedule_locked.wait(timeout=15)
    second.start()
    assert binding_started.wait(timeout=15)
    # Let the old unlocked path reach its paused validation. The fixed path
    # remains behind the schedule writer until its existing heat commits.
    threading.Event().wait(.3)
    generate.set()
    assert generated.wait(timeout=15)
    permit_bind.set()
    first.join(timeout=15)
    second.join(timeout=15)
    assert not first.is_alive() and not second.is_alive()
    result, detail = results.get(timeout=1)
    assert result == 'binding_refused' and 'fresh' in detail, (result, detail)
    with app.app_context():
        assert db.session.get(Event, event_id).heats.count() == 1
        assert ManagedEventLink.query.count() == 0


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


@pytest.mark.parametrize('change', ['add', 'remove', 'gear', 'division'])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_shared_field_refresh_rechecks_unchanged_source_roster(tmp_path, change):
    from models import ProCompetitor
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        state = managed_events.bind_event(event, user, config)
        entrant = ProCompetitor.query.one()
        if change == 'add':
            make_pro_competitor(db.session, event.tournament, 'Synthetic unprojected entrant', events=['Underhand'])
        elif change == 'remove':
            entrant.events_entered = '[]'
        elif change == 'gear':
            entrant.gear_sharing = json.dumps({'Underhand': 'Synthetic unresolved gear owner'})
        else:
            entrant.gender = 'M' if entrant.gender == 'F' else 'F'
        db.session.commit()
        managed_events._call(user, event.managed_operation, {
            'operation': 'fields', 'event_id': state['event_id'], 'request_id': 'shared-refreshed-field',
            'expected_revision': state['revision'], 'editor': 'missoula', 'payload': {'fields': [
                {'heat_id': 'refreshed-field', 'entries': [config['roster'][0]['entry_id']], 'stands': ['1']}]}})
        with pytest.raises(ManagedEventError):
            managed_events.submit(event, user, {'operation': 'read'})
        db.session.rollback()
        assert event.heats.count() == 0
        assert json.loads(event.managed_operation.state_json)['revision'] == state['revision']


@pytest.mark.parametrize('size', [None, 0, float('inf')])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_incomplete_or_invalid_host_wood_is_a_managed_validation_error(tmp_path, size):
    from models import WoodConfig
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        WoodConfig.query.one().size_value = size
        db.session.commit()
        with pytest.raises(ManagedEventError, match='wood'):
            managed_events.bind_event(db.session.get(Event, event_id), db.session.get(User, user_id), config)
        db.session.rollback()
        assert db.session.get(Event, event_id).managed_operation is None


@pytest.mark.parametrize('operation', ['accept', 'awards'])
@pytest.mark.parametrize('change', ['wood', 'gear'])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_uncoordinated_context_writer_waits_through_actual_host_commit(tmp_path, monkeypatch, operation, change):
    from models import ProCompetitor, WoodConfig
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    attempted, finished = threading.Event(), threading.Event()
    errors = queue.Queue()
    writer = None
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        _single_entry_final(event, user, config)
        exported = managed_events.submit(event, user, {'operation': 'export'})
        if operation == 'awards':
            managed_events.accept_export(event, user, exported)
            db.session.commit()
        real_context = managed_events._validate_host_context

        def write_without_parent_lock():
            try:
                with app.app_context():
                    if change == 'wood':
                        row = WoodConfig.query.one()
                        row.size_value = 280
                    else:
                        row = ProCompetitor.query.one()
                        row.gear_sharing = json.dumps({'Underhand': 'Synthetic later gear change'})
                    attempted.set()
                    # Deliberately no tournament lock/route decorator: this
                    # models the existing Woodboss/gear writers' transaction.
                    db.session.commit()
            except Exception as exc:
                errors.put(exc)
            finally:
                finished.set()

        def after_context_validation(*args):
            nonlocal writer
            real_context(*args)
            if writer is None:
                writer = threading.Thread(target=write_without_parent_lock)
                writer.start()
                assert attempted.wait(10)
                assert not finished.wait(0.25), 'Host context changed between validation and commit'

        monkeypatch.setattr(managed_events, '_validate_host_context', after_context_validation)
        try:
            if operation == 'awards':
                managed_events.apply_host_awards(event, user)
            else:
                managed_events.accept_export(event, user, exported)
            assert not finished.is_set()
            db.session.commit()
        finally:
            db.session.rollback()
            if writer is not None:
                writer.join(10)
        assert finished.is_set() and not writer.is_alive()
        assert errors.empty(), list(errors.queue)
        assert db.session.get(Event, event_id).status == 'completed'
        if operation == 'awards':
            assert db.session.get(Event, event_id).is_finalized


@pytest.mark.parametrize('diameter', [None, {}, 'not-a-diameter', float('nan')])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_supplied_wood_diameter_is_validated_before_source_creation(tmp_path, diameter):
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    config['wood']['diameter_mm'] = diameter
    with app.app_context():
        with pytest.raises(ManagedEventError, match='wood'):
            managed_events.bind_event(db.session.get(Event, event_id), db.session.get(User, user_id), config)
        db.session.rollback()
        assert db.session.get(Event, event_id).managed_operation is None


@pytest.mark.parametrize('history', [False, True])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_initial_binding_reconciles_only_empty_stale_pending_results(tmp_path, history):
    from models import EventResult
    from services import managed_events
    from tests.conftest import make_event_result

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        withdrawn = make_pro_competitor(db.session, event.tournament, 'Synthetic old pending entry', events=[])
        row = make_event_result(db.session, event, withdrawn, status='pending')
        if history:
            row.t1_run1 = 0
        db.session.commit()
        if history:
            with pytest.raises(ManagedEventError, match='history|fresh'):
                managed_events.bind_event(event, user, config)
            db.session.rollback()
            assert event.managed_operation is None
            assert EventResult.query.filter_by(competitor_id=withdrawn.id).one().t1_run1 == 0
        else:
            managed_events.bind_event(event, user, config)
            assert event.results.count() == 1
            assert event.results.one().competitor_id != withdrawn.id


@pytest.mark.parametrize('change', ['wood', 'gear'])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_initial_source_creation_holds_host_context_fence(tmp_path, monkeypatch, change):
    from models import ProCompetitor, WoodConfig
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    attempted, finished = threading.Event(), threading.Event()
    errors = queue.Queue()
    writer = None
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        real_call = managed_events._call

        def write_without_parent_lock():
            try:
                with app.app_context():
                    if change == 'wood':
                        WoodConfig.query.one().size_value = 280
                    else:
                        ProCompetitor.query.one().gear_sharing = json.dumps({'Underhand': 'Synthetic later gear owner'})
                    attempted.set()
                    db.session.commit()
            except Exception as exc:
                errors.put(exc)
            finally:
                finished.set()

        def source_call_under_fence(*args):
            nonlocal writer
            if args[2]['operation'] == 'create':
                writer = threading.Thread(target=write_without_parent_lock)
                writer.start()
                assert attempted.wait(10)
                assert not finished.wait(0.25), 'Context writer crossed initial source creation'
                result = real_call(*args)
                assert not finished.is_set()
                return result
            return real_call(*args)

        monkeypatch.setattr(managed_events, '_call', source_call_under_fence)
        try:
            managed_events.bind_event(event, user, config)
        finally:
            db.session.rollback()
            if writer is not None:
                writer.join(10)
        assert finished.is_set() and not writer.is_alive()
        assert errors.empty(), list(errors.queue)
        assert event.managed_operation.state_json


@pytest.mark.parametrize('change', ['wood', 'gear'])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_creation_rechecks_host_context_after_durable_intent_commit(tmp_path, monkeypatch, change):
    from models import ProCompetitor, WoodConfig
    from models.managed_event import ManagedCommand
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        commits = 0
        session = db.session()
        def alter_after_intent(_session):
            nonlocal commits
            commits += 1
            if commits == 2:  # Link, then durable create intent, before RPC.
                with db.engine.begin() as connection:
                    if change == 'wood':
                        connection.execute(WoodConfig.__table__.update().values(size_value=280))
                    else:
                        connection.execute(ProCompetitor.__table__.update().values(
                            gear_sharing=json.dumps({'Underhand': 'Synthetic changed gear'})))
        def forbidden_call(*_args):
            pytest.fail('Source create reached with context changed after intent commit')
        monkeypatch.setattr(managed_events, '_call', forbidden_call)
        sa_event.listen(session, 'after_commit', alter_after_intent)
        try:
            with pytest.raises(ManagedEventError, match='wood|gear'):
                managed_events.bind_event(event, user, config)
        finally:
            sa_event.remove(session, 'after_commit', alter_after_intent)
            db.session.rollback()
        assert commits == 2 and ManagedCommand.query.one().status == 'pending'
        assert event.managed_operation.state_json is None
        assert not (tmp_path / 'authority.sqlite3').exists()


@pytest.mark.parametrize('receipt', [[], None, 7, 'not-an-object'])
def test_nonobject_career_receipt_is_retained_for_retry(receipt):
    from types import SimpleNamespace

    from models.managed_event import ManagedCareerDelivery
    from services import managed_events

    app, event_id, user_id, _ = _projection_fixture()
    app.config.update(MNEMEX_SHOW_RESULTS_URL='http://127.0.0.1/partners/results/ingest',
                      MNEMEX_SHOW_RESULTS_TOKEN='synthetic-scoped-token')
    with app.app_context():
        event, user = db.session.get(Event, event_id), db.session.get(User, user_id)
        event.managed_operation.mapping_json = json.dumps({'e0': 1})
        payload = {'source_result_id': 'synthetic:result:0', 'source_revision': 1, 'outcome': {'entry_id': 'e0'}}
        item = ManagedCareerDelivery(delivery_id='d' * 64, event_id=event.id,
            payload_json=managed_events.canonical(payload), payload_digest=managed_events.digest(payload))
        db.session.add(item)
        db.session.commit()
        response = SimpleNamespace(status_code=200, content=b'[]', json=lambda: receipt)
        result = managed_events.deliver_career_results(event, user, transport=lambda *_args, **_kw: response)
        assert result['remaining_pending'] == result['pending_attempts'] == 1
        assert result['accepted'] == 0 and item.status == 'pending'
        assert item.last_error_code == 'receiver_unavailable_or_invalid_receipt'
        assert item.receipt_json is None


@pytest.mark.parametrize('show_id', [None, {}, 7, '', 'x' * 225, 'contains spaces'])
@pytest.mark.skipif(not os.environ.get('PROAM_MANAGED_TEST_PYTHON'), reason='requires installed component')
def test_invalid_show_identity_cannot_allocate_a_pending_link(tmp_path, show_id):
    from services import managed_events

    app, event_id, user_id, config = _binding_fixture(tmp_path)
    config['show_id'] = show_id
    with app.app_context():
        with pytest.raises(ManagedEventError):
            managed_events.bind_event(db.session.get(Event, event_id), db.session.get(User, user_id), config)
        db.session.rollback()
        assert db.session.get(Event, event_id).managed_operation is None
        assert not (tmp_path / 'authority.sqlite3').exists()
