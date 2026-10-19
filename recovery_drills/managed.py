"""Synthetic counterparts using the real installed producer and PG host.

Only the owned recovery launcher imports this instrumentation/fixture driver.
"""

import concurrent.futures
import json
import subprocess
import sys
import uuid
from pathlib import Path

from recovery_drills.lab import Client, Inputs
from recovery_drills.ownership import publish
from recovery_drills.scenarios import check


def seed_managed(app, settings):
    from database import db
    from models import Event, ProCompetitor, Tournament, User, WoodConfig
    from services.managed_events import bind_event, submit

    producer = settings['managed_python']
    example = subprocess.run([producer, '-I', '-m', 'strathmark', 'example', '--json'], capture_output=True, text=True, check=True, timeout=30)
    config = json.loads(example.stdout)
    with app.app_context():
        tournament = db.session.get(Tournament, 1)
        event = Event(tournament_id=tournament.id, name='[SYNTHETIC] Managed Underhand', event_type='pro', scoring_type='time', stand_type='underhand', max_stands=4, is_handicap=True)
        db.session.add(event)
        db.session.add(WoodConfig(tournament_id=tournament.id, config_key='block_underhand_pro', species='S01', size_value=275, size_unit='mm'))
        user = User.query.filter_by(username='judge').one()
        db.session.flush()
        for index, row in enumerate(config['roster']):
            comp = ProCompetitor(tournament_id=tournament.id, name=f'Synthetic Managed {index}', gender=row['gender'], events_entered=json.dumps([event.name]))
            db.session.add(comp)
            db.session.flush()
            row['host_ref'] = f'pro:{comp.id}'
        db.session.commit()
        config.update(event_id=event.shadow_event_occurrence_id + ':managed', tournament_id=tournament.shadow_tournament_id)
        state = bind_event(event, user, config)

        def command(action, payload=None):
            nonlocal state
            state = submit(event, user, {'operation': action, 'request_id': str(uuid.uuid4()), 'expected_revision': state['revision'], 'editor': 'missoula', 'payload': payload or {}})

        eligible = [row['entry_id'] for row in config['roster']]
        for ordinal, rule in enumerate(config['round_rules']):
            cap = rule['capacity']
            fields = [{'heat_id': f'r{ordinal}h{n//cap}', 'entries': eligible[n:n+cap], 'stands': config['stands'][:len(eligible[n:n+cap])]} for n in range(0, len(eligible), cap)]
            command('fields', {'fields': fields})
            for spec in fields:
                command('calculate', {'heat_id': spec['heat_id']})
                heat = next(h for h in state['rounds'][-1]['fields'] if h['heat_id'] == spec['heat_id'])
                command('review', {'heat_id': heat['heat_id'], 'sheet_digest': heat['sheet']['digest']})
                command('issue', {'heat_id': heat['heat_id'], 'sheet_digest': heat['sheet']['digest'], 'confirm': True})
                heat = next(h for h in state['rounds'][-1]['fields'] if h['heat_id'] == spec['heat_id'])
                rows = [{'entry_id': r['entry_id'], 'status': 'finished', 'timer1_seconds': str(100+i-r['start_mark_seconds']), 'timer2_seconds': str(100+i-r['start_mark_seconds'])} for i, r in enumerate(heat['sheet']['rows'])]
                command('outcomes', {'heat_id': heat['heat_id'], 'issue_id': heat['issue_id'], 'outcomes': rows})
                heat = next(h for h in state['rounds'][-1]['fields'] if h['heat_id'] == spec['heat_id'])
                command('accept', {'heat_id': heat['heat_id'], 'result_digest': heat['result_revisions'][-1]['digest']})
            if ordinal < 2:
                command('advance')
                eligible = state['rounds'][-1]['advancing']
        command('finalize')
        publish(Path(settings['private'])/'managed-fixture.json', json.dumps({'event': event.id, 'source_revision': state['revision']}))


def correct_managed(app, settings):
    from database import db
    from models import Event, User
    from services.managed_events import submit

    fixture = json.loads((Path(settings['private'])/'managed-fixture.json').read_text())
    with app.app_context():
        event = db.session.get(Event, fixture['event'])
        user = User.query.filter_by(username='judge').one()
        state = submit(event, user, {'operation': 'read'})
        def command(action, payload=None):
            nonlocal state
            state = submit(event, user, {'operation': action, 'request_id': str(uuid.uuid4()), 'expected_revision': state['revision'], 'editor': 'missoula', 'payload': payload or {}})
        heat = state['rounds'][-1]['fields'][0]
        result = heat['result_revisions'][-1]
        rows = [{'entry_id': r['entry_id'], 'status': 'finished', 'timer1_seconds': str(110-i-r['start_mark_seconds']), 'timer2_seconds': str(110-i-r['start_mark_seconds'])} for i, r in enumerate(result['outcomes'])]
        command('correct', {'heat_id': heat['heat_id'], 'issue_id': heat['issue_id'], 'outcomes': rows, 'reason': 'Synthetic corrected final', 'supersedes': result['digest']})
        heat = state['rounds'][-1]['fields'][0]
        command('accept', {'heat_id': heat['heat_id'], 'result_digest': heat['result_revisions'][-1]['digest']})
        command('finalize')


def portability(lab, result):
    producer = lab.settings.get('managed_python')
    if not producer:
        raise FileNotFoundError('Explicit installed managed-event interpreter required.')
    output = lab.private/'component-portability'
    lab.command([producer, '-I', '-m', 'strathmark.events.verification', '--output', str(output)], timeout=240)
    report = json.loads((output/'report.json').read_text())
    check(result, 'identical installed component completes two isolated shows', len(report['shows']), 2)
    check(result, 'all round attempts and separate summaries survive CLI process restarts', [(r['attempts'], r['summary_entries']) for r in report['shows']], [(14, 8), (14, 8)])
    check(result, 'same numeric component serves both configurations', len({r['numeric_identity'] for r in report['shows']}), 1)
    for show in report['shows']:
        show_id = show['show_id']
        directory = output / show_id.replace(':', '-')
        database = directory / 'authority.sqlite3'
        event_id = show_id + ':underhand'
        def cli(action, payload=None, state=None):
            command = [producer, '-I', '-m', 'strathmark', 'event', action, '--database', str(database), '--event-id', event_id, '--show', show_id, '--json']
            if payload is not None:
                supplied = directory / ('correction-' + uuid.uuid4().hex + '.json')
                supplied.write_text(json.dumps(payload))
                command += ['--input', str(supplied), '--request-id', uuid.uuid4().hex, '--expected-revision', str(state['revision'])]
            return json.loads(lab.command(command).stdout)
        state = cli('status')
        final = state['rounds'][-1]['fields'][0]
        issue_id = final['issue_id']
        rows = [{'entry_id': entry, 'status': 'finished', 'timer1_seconds': str(101+i), 'timer2_seconds': str(101+i)} for i, entry in enumerate(final['entries'])]
        rows[0] = {'entry_id': final['entries'][0], 'status': 'void'}
        state = cli('correct', {'heat_id': final['heat_id'], 'issue_id': issue_id, 'supersedes': final['result_revisions'][-1]['digest'], 'reason': 'Synthetic CLI correction and explicit void', 'outcomes': rows}, state)
        final = state['rounds'][-1]['fields'][0]
        state = cli('accept', {'heat_id': final['heat_id'], 'result_digest': final['result_revisions'][-1]['digest']}, state)
        state = cli('finalize', {}, state)
        exported = cli('export')
        check(result, show_id + ' CLI correction appends two source revisions and re-finalizes separate summary', (len(exported['deliveries']),len(exported['summary']),state['summary_revision']), (16,8,2))
        check(result, show_id + ' issued field remains unchanged by correction', state['rounds'][-1]['fields'][0]['issue_id'], issue_id)
    result['limitations'].append('Synthetic V2 and SQLite component evidence; no V3, provider, Windows or official-use qualification.')


def acceptance_crashes(lab, result):
    if not lab.settings.get('managed_python'):
        raise FileNotFoundError('Explicit installed managed-event interpreter required.')
    settings = str(lab.private/'settings.json')
    lab.command([sys.executable, '-m', 'recovery_drills.worker', settings, 'managed-seed'], timeout=240)
    fixture = json.loads((lab.private/'managed-fixture.json').read_text())
    path = f"/managed/1/event/{fixture['event']}"
    client = Client(lab)
    client.login()
    for phase, expected_before, expected_after in [('precommit', 0, 1), ('postcommit', 2, 2)]:
        if phase == 'postcommit':
            lab.command([sys.executable, '-m', 'recovery_drills.worker', settings, 'managed-correct'], timeout=90)
        page = client.request(path)
        check(result, 'normal managed operator page loads', page['status'], 200)
        request_id = str(uuid.uuid4())
        form = {'csrf_token': Inputs(page['body']).values['csrf_token'], 'action': 'accept_export', 'request_id': request_id}
        lab.arm('managed-accept-' + phase, request_id)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(client.request, path, form)
            marker = lab.reached()
            result['fault']['reached'] = True
            count = lab.query('SELECT count(*) AS n FROM managed_acceptances')[0]['n']
            check(result, phase + ' independent acceptance visibility', count, expected_before)
            if phase == 'precommit':
                check(result, 'no partial attempt or delivery survives before commit', lab.query('SELECT (SELECT count(*) FROM managed_attempt_revisions) AS attempts,(SELECT count(*) FROM managed_career_deliveries) AS deliveries')[0], {'attempts': 0, 'deliveries': 0})
            lab.kill_worker(marker)
            try:
                pending.result(timeout=20)
            except (OSError, ValueError):
                pass
        result['fault']['observed'] = True
        response = client.request(path, form)
        check(result, phase + ' exact accepted retry returns normal redirect', response['status'], 303)
        check(result, phase + ' one acceptance per source revision', lab.query('SELECT count(*) AS n FROM managed_acceptances')[0]['n'], expected_after)
    check(result, 'all original and corrected attempts remain', lab.query('SELECT count(*) AS n FROM managed_attempt_revisions')[0]['n'], 16)
    check(result, 'all accepted attempts have durable career intents', lab.query('SELECT count(*) AS n FROM managed_career_deliveries')[0]['n'], 16)
    check(result, 'acceptance does not settle money', lab.query('SELECT count(*) AS n FROM event_results WHERE payout_amount <> 0')[0]['n'], 0)
    check(result, 'host summary never overwrites per-round attempts', lab.query('SELECT count(*) AS n FROM event_results WHERE event_id=%s', (fixture['event'],))[0]['n'], 8)
    result['limitations'].append('Real PostgreSQL/Gunicorn acceptance faults with installed V2 component; no MNEMEX receiver/provider claim.')
