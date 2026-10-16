"""Business assertions shared by real-process drills; no replacement scoring."""
from __future__ import annotations

import concurrent.futures
import copy
import json
import re
import signal
import time
import urllib.error
import uuid
from pathlib import Path

import psycopg2

from recovery_drills.lab import Client, score_payload
from recovery_drills.ownership import digest, publish, stop_process, wait_until

SCENARIOS = {
    'restore-fresh-postgres': ('R01', 'core'),
    'score-precommit-crash': ('R02', 'core'),
    'score-postcommit-response-loss': ('R03', 'core'),
    'database-outage': ('R04', 'extended'),
    'bad-backup': ('R05', 'extended'),
    'browser-offline-replay': ('R06', 'extended'),
    'background-worker-crash': ('R07', 'extended'),
    'shadow-delivery': ('R08', 'extended'),
    'code-schema-rollback': ('R09', 'extended'),
    'sqlite-offline-restore': ('R10', 'extended'),
    'managed-component-portability': ('R11', 'managed'),
    'managed-acceptance-crashes': ('R12', 'managed'),
}


def check(result, name, actual, expected=True):
    outcome = actual == expected
    result['checks'].append({'name': name, 'status': 'PASS' if outcome else 'FAIL',
                             'expected': expected, 'actual': actual})
    if not outcome:
        raise AssertionError(f'{name}: expected/actual recorded in checks.')


def differences(left, right):
    output = {}
    def keyed(rows):
        return {str(row.get('id', row.get('request_id', row.get('uid', json.dumps(row, sort_keys=True))))): row
                for row in rows or []}
    for table in sorted(left.keys() | right.keys()):
        if left.get(table) == right.get(table):
            continue
        before, after = keyed(left.get(table)), keyed(right.get(table))
        output[table] = {}
        for identity in sorted(before.keys() | after.keys()):
            a, b = before.get(identity, {}), after.get(identity, {})
            changed = {field: {'expected': a.get(field), 'actual': b.get(field)}
                       for field in sorted(a.keys() | b.keys()) if a.get(field) != b.get(field)}
            if changed:
                output[table][identity] = changed
    return output


def same_state(result, name, before, after):
    diff = differences(before, after)
    check(result, name, diff, {})


def accepted(result, response, replay=False):
    result.setdefault('responses', []).append({
        'status': response['status'], 'json': response.get('json')})
    check(result, 'score response is JSON acceptance',
          response['status'] == 200 and isinstance(response.get('json'), dict)
          and response['json'].get('ok') is True
          and response['json'].get('receipt', {}).get('accepted') is True)
    check(result, 'receipt replay disposition', response['json'].get('receipt_replayed'), replay)


def submit(lab, result, client, heat, ids, values):
    form = client.form(heat)
    payload = score_payload(form, ids, values)
    response = client.score(heat, payload)
    accepted(result, response)
    return payload


def verify_values(lab, result, expected):
    rows = lab.query('SELECT competitor_id, result_value FROM event_results ORDER BY competitor_id')
    actual = [float(row['result_value']) if row['result_value'] is not None else None for row in rows]
    check(result, 'independent committed cutting durations', actual, expected)


def enforce(lab, result, client):
    heat = lab.fixture['heats'][0]
    ids = lab.fixture['competitors'][:2]
    payload = score_payload(client.form(heat), ids, (11.1, 12.1))
    before = lab.snapshot()
    for missing, status in [('csrf_token', 400), ('heat_identity', 409),
                             ('scoring_state_digest', 409)]:
        wrong = dict(payload)
        wrong.pop(missing)
        response = client.score(heat, wrong)
        check(result, 'reject missing ' + missing, response['status'], status)
    for user in ('viewer', 'outsider'):
        other = Client(lab)
        other.login(user)
        # Its own valid token makes this a role/scope check, not a CSRF rejection.
        wrong = {**payload, 'csrf_token': other.csrf}
        response = other.score(heat, wrong)
        check(result, 'reject unauthorized ' + user, response['status'], 403)
    same_state(result, 'rejected requests preserve business state', before, lab.snapshot())


def schema(lab):
    output = {
        'columns': lab.query("SELECT table_name,column_name,data_type,is_nullable,column_default "
                             "FROM information_schema.columns WHERE table_schema='public' "
                             "ORDER BY table_name,ordinal_position"),
        'indexes': lab.query("SELECT tablename,indexname,indexdef FROM pg_indexes "
                             "WHERE schemaname='public' ORDER BY tablename,indexname"),
        'constraints': lab.query("SELECT conrelid::regclass::text AS relation,conname,"
                                 "pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                                 "WHERE connamespace='public'::regnamespace ORDER BY 1,2"),
        'triggers': lab.query("SELECT tgrelid::regclass::text AS relation,tgname,"
                              "pg_get_triggerdef(oid) AS definition FROM pg_trigger "
                              "WHERE NOT tgisinternal ORDER BY 1,2"),
    }
    # A dump/reparse can distribute an array-to-text cast over its literals.
    # Normalize only that spelling, retaining column, operator and every value.
    for constraint in output['constraints']:
        definition = constraint['definition']
        match = re.match(r'CHECK \(\(\(([a-z_]+)\)::text = ANY \(', definition)
        if match:
            values = re.findall(r"'((?:[^']|'')*)'::character varying", definition)
            constraint['definition'] = {'text_equality_any_column': match[1], 'values': values}
    return output


def stop_app(lab):
    lab.remember_workers()
    entries = [entry for entry in lab.owner.record['processes'] if entry['kind'] in ('gunicorn', 'worker')]
    for entry in entries:
        lab.owner.signal(entry['identity'])
    from recovery_drills.ownership import process_identity
    for entry in entries:
        wait_until(lambda: process_identity(entry['identity']['pid']) is None, 8, 'app stop')


def restore(lab, artifact, database='restored'):
    # Only run-generated databases exist, in the identity-verified owned cluster.
    with lab.connect('postgres') as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(psycopg2.sql.SQL('CREATE DATABASE {} OWNER lab_app').format(
                psycopg2.sql.Identifier(database)))
    lab.command([lab.bin / 'pg_restore', '--host', '127.0.0.1', '--port', str(lab.port),
                 '--username', 'lab_app', '--dbname', database, '--exit-on-error',
                 '--single-transaction', '--no-owner', '--no-acl', artifact], timeout=90)


def fresh_restore(lab, result):
    client = Client(lab)
    client.login()
    enforce(lab, result, client)
    ids = lab.fixture['competitors']
    heats = lab.fixture['heats']
    submit(lab, result, client, heats[0], ids[:2], (11.1, 12.1))
    checkpoint = lab.snapshot()
    expected_schema = schema(lab)
    lab.worker('backup')  # actual services.backup.backup_database -> pg_dump local backend
    backup = json.loads((lab.private / 'backup.json').read_text())
    artifact = Path(backup['dest'])
    check(result, 'application backup digest', digest(artifact), backup['sha256'])
    result['artifacts'].append({'kind': 'application-pg-dump', 'sha256': backup['sha256'],
                                 'size_bytes': backup['size_bytes'], 'retention': 'deleted at cleanup'})
    same_state(result, 'quiescent backup does not alter business state', checkpoint, lab.snapshot())
    later = submit(lab, result, client, heats[1], ids[2:], (9.1, 14.1))
    stop_app(lab)
    started = time.monotonic()
    restore(lab, artifact)
    lab.database = 'restored'
    same_state(result, 'checkpoint A restored before app startup', checkpoint, lab.snapshot())
    check(result, 'all columns indexes constraints and triggers restored', schema(lab), expected_schema)
    check(result, 'checkpoint B receipt intentionally outside snapshot', lab.query(
        'SELECT request_id FROM score_submission_receipts WHERE request_id=%s',
        (later['request_id'],)), [])
    result['request_reconciliation']['lost_expected'] = [later['request_id']]
    with lab.connect('restored') as connection:
        with connection.cursor() as cursor:
            cursor.execute('GRANT USAGE ON SCHEMA public TO lab_read')
            cursor.execute('GRANT SELECT ON ALL TABLES IN SCHEMA public TO lab_read')
        connection.commit()
    with lab.connect('restored', 'lab_app') as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user')
            check(result, 'application is a normal non-superuser', cursor.fetchone(), (False, False, False))
    with lab.connect('restored', 'lab_read') as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM tournaments')
            check(result, 'read-only user reads restored tournament set', cursor.fetchone()[0], 2)
            try:
                cursor.execute("UPDATE tournaments SET name='forbidden'")
            except psycopg2.Error as error:
                check(result, 'read-only write denied', error.pgcode in ('25006', '42501'))
                connection.rollback()
            else:
                check(result, 'read-only write denied', False)
    lab.settings['database_url'] = lab.uri('restored')
    lab.write_settings()
    lab.start_app()
    same_state(result, 'startup changes reported separately (none)', checkpoint, lab.snapshot())
    client = Client(lab)
    client.login()
    response = client.request('/reporting/1/event/1/results')
    check(result, 'restored judge report contains correct score',
          response['status'] == 200 and '11.10' in response['body'] and 'Synthetic Alpha' in response['body'])
    partial = score_payload(client.form(heats[1]), ids[2:], (10.1, 15.1))
    partial.pop(f't2_run1_{ids[2]}')
    saved_partial = client.score(heats[1], partial)
    accepted(result, saved_partial)
    check(result, 'one-timer save retains its documented warning',
          saved_partial['json']['category'], 'warning')
    state = lab.query('SELECT status,result_value,t1_run1,t2_run1 FROM event_results '
                      'WHERE competitor_id=%s', (ids[2],))[0]
    check(result, 'partial timer saved without fabricated average',
          [state['status'], state['result_value'], state['t1_run1'], state['t2_run1']],
          ['partial', None, 10.0, None])
    check(result, 'partial result cannot auto-finalize restored event',
          lab.query('SELECT is_finalized FROM events WHERE id=1')[0]['is_finalized'], False)
    submit(lab, result, client, heats[1], ids[2:], (10.1, 15.1))
    spectator = Client(lab).request('/portal/spectator/1/event/1')
    check(result, 'restored spectator output contains completed results',
          spectator['status'] == 200 and 'Synthetic Gamma' in spectator['body'] and '10.1' in spectator['body'])
    verify_values(lab, result, [11.1, 12.1, 10.1, 15.1])
    positions = lab.query('SELECT final_position,payout_amount FROM event_results ORDER BY competitor_id')
    check(result, 'new score restores independently expected positions',
          [row['final_position'] for row in positions], [2, 3, 1, 4])
    check(result, 'new score restores independently expected payout accounting',
          [float(row['payout_amount']) for row in positions], [50., 25., 100., 10.])
    result['timings_seconds']['restore_to_verified_workflow'] = time.monotonic() - started
    result['limitations'].append('Database recovery plus recreated synthetic configuration; uploads, '
                                  'browser queues and external peers are outside pg_dump. Logical restore is not PITR.')


def crash(lab, result, after_commit):
    client = Client(lab)
    client.login()
    enforce(lab, result, client)
    ids, heats = lab.fixture['competitors'], lab.fixture['heats']
    submit(lab, result, client, heats[0], ids[:2], (11.1, 12.1))
    payload = score_payload(client.form(heats[1]), ids[2:], (9.1, 14.1))
    before = lab.snapshot()  # after legitimate GET edit-lock transaction
    point = 'score-postcommit' if after_commit else 'score-precommit'
    lab.arm(point, payload['request_id'])
    def send():
        try:
            return client.score(heats[1], payload)
        except (OSError, urllib.error.URLError) as error:
            return {'ambiguous': type(error).__name__}
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(send)
        reached = lab.reached()
        check(result, 'exact requested transaction barrier reached', reached['point'], point)
        check(result, 'no success response exposed at barrier', future.done(), False)
        receipts = lab.query('SELECT request_id FROM score_submission_receipts WHERE request_id=%s',
                             (payload['request_id'],))
        check(result, 'independent observer sees acceptance at correct boundary', bool(receipts), after_commit)
        if after_commit:
            verify_values(lab, result, [11.1, 12.1, 9.1, 14.1])
            lab.record('observed_committed_without_ack', heats[1], payload)
            result['request_reconciliation']['observed_committed_without_ack'] = 1
        else:
            same_state(result, 'staged score audit receipt and finalization are invisible', before, lab.snapshot())
        fault_start = time.monotonic()
        lab.kill_worker(reached)
        response = future.result(timeout=15)
    check(result, 'client observes ambiguous loss, never acceptance',
          'ambiguous' in response or response.get('status', 0) >= 500)
    result['fault'] = {'point': point, 'mechanism': 'SIGKILL owned Gunicorn worker',
                        'reached': True, 'observed': True, 'dead_pid': reached['process']['pid'],
                        'recovery': 'existing one-worker Gunicorn master respawn'}
    if not after_commit:
        same_state(result, 'precommit crash rolled back whole declared transaction', before, lab.snapshot())
    # Refresh only transport CSRF. The accepted business payload/identity is unchanged.
    payload['csrf_token'] = client.form(heats[1])['csrf_token']
    response = client.score(heats[1], payload)
    accepted(result, response, replay=after_commit)
    lab.record('observed_committed', heats[1], payload, response)
    verify_values(lab, result, [11.1, 12.1, 9.1, 14.1])
    stable = lab.snapshot()
    response = client.score(heats[1], payload)
    accepted(result, response, replay=True)
    same_state(result, 'exact retry has no second business effect', stable, lab.snapshot())
    wrong = {**payload, f't1_run1_{ids[2]}': '99'}
    check(result, 'conflicting payload identity rejected', client.score(heats[1], wrong)['status'], 409)
    for heat, user in ((heats[0], 'judge'), (heats[1], 'otherjudge')):
        other = Client(lab)
        other.login(user)
        wrong = {**payload, 'heat_id': str(heat), 'csrf_token': other.csrf}
        check(result, 'receipt heat/user binding rejected', other.score(heat, wrong)['status'], 409)
    same_state(result, 'conflicts preserve authoritative state', stable, lab.snapshot())
    report = client.request('/reporting/1/event/1/results')
    check(result, 'post-crash report uses committed rankings', report['body'].index(
        '<strong>Synthetic Gamma</strong>') < report['body'].index('<strong>Synthetic Alpha</strong>'))
    # Later legitimate correction, followed by older receipt replay.
    submit(lab, result, client, heats[1], ids[2:], (20.1, 14.1))
    corrected = lab.snapshot()
    response = client.score(heats[1], payload)
    accepted(result, response, replay=True)
    same_state(result, 'old accepted outcome cannot overwrite newer correction', corrected, lab.snapshot())
    result['timings_seconds']['fault_to_verified_workflow'] = time.monotonic() - fault_start


FUNCTIONS = {
    'restore-fresh-postgres': fresh_restore,
    'score-precommit-crash': lambda lab, result: crash(lab, result, False),
    'score-postcommit-response-loss': lambda lab, result: crash(lab, result, True),
}
