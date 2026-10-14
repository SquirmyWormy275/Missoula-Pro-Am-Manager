"""Required extensions using the same owned resources and real workflows."""
from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import psycopg2

from recovery_drills.lab import Client, score_payload
from recovery_drills.ownership import Refused, digest, publish, stop_process, wait_until
from recovery_drills.scenarios import (
    accepted,
    check,
    restore,
    same_state,
    submit,
    verify_values,
)


def database_outage(lab, result):
    client = Client(lab)
    client.login()
    ids, heats = lab.fixture['competitors'], lab.fixture['heats']
    submit(lab, result, client, heats[0], ids[:2], (11.1, 12.1))
    payload = score_payload(client.form(heats[1]), ids[2:], (9.1, 14.1))
    before = lab.snapshot()
    lab.arm('score-precommit', payload['request_id'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.score, heats[1], payload)
        reached = lab.reached()
        postgres = next(item['identity'] for item in reversed(lab.owner.record['processes'])
                        if item['kind'] == 'postgres')
        started = time.monotonic()
        lab.owner.signal(postgres, signal.SIGINT)  # owned cluster fast shutdown, not a host service
        from recovery_drills.ownership import process_identity
        wait_until(lambda: process_identity(postgres['pid']) is None, 8, 'database disconnection')
        publish(lab.private / 'release', 'release')
        response = pending.result(timeout=15)
        check(result, 'established score connection cannot fabricate acceptance', response['status'], 500)
        response = client.request('/scoring/1/heat/2/enter')
        check(result, 'new connection failure is bounded and visible', response['status'], 500)
    for name in ('fault.json', 'reached.json', 'release'):
        (lab.private / name).unlink(missing_ok=True)
    lab.pg = lab.launch([lab.bin / 'postgres', '-D', lab.cluster, '-h', '127.0.0.1',
                         '-p', str(lab.port), '-k', '', '-c', 'fsync=on',
                         '-c', 'synchronous_commit=on'], 'postgres')
    def available():
        try:
            return lab.query('SELECT 1 AS ok')
        except psycopg2.OperationalError:
            return None
    wait_until(available, 15, 'same owned cluster restart')
    same_state(result, 'database outage lost no previously accepted work or partial submission', before, lab.snapshot())
    payload['csrf_token'] = client.form(heats[1])['csrf_token']
    accepted(result, client.score(heats[1], payload))
    verify_values(lab, result, [11.1, 12.1, 9.1, 14.1])
    # A distinct lock-wait lane: observe actual pg blocking, then release the
    # owner normally. This is not a claim that unbounded locks have a new SLA.
    fresh = score_payload(client.form(heats[1]), ids[2:], (10.1, 14.1))
    with lab.connect() as blocker:
        with blocker.cursor() as cursor:
            cursor.execute('SELECT id FROM tournaments WHERE id=1 FOR UPDATE')
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.score, heats[1], fresh)
            wait_until(lambda: lab.query("SELECT pid FROM pg_stat_activity WHERE datname='source' "
                                          "AND usename='lab_app' AND wait_event_type='Lock'"),
                       5, 'real scoring lock wait')
            check(result, 'lock-wait request has no early acknowledgment', pending.done(), False)
            blocker.rollback()
            accepted(result, pending.result(timeout=12))
    check(result, 'post-outage report is usable', client.request('/reporting/1/event/1/results')['status'], 200)
    result['fault'] = {'point': reached['point'], 'mechanism': 'owned PostgreSQL fast shutdown',
                        'reached': True, 'observed': True}
    result['timings_seconds']['fault_to_verified_workflow'] = time.monotonic() - started
    result['limitations'].append('Controlled lock was observed and released within the lab watchdog. '
                                  'No new application lock timeout or production recovery objective was introduced.')


def validate_artifact(path, manifest, expected):
    from recovery_drills.ownership import no_alias
    no_alias(path)
    if manifest != expected or not manifest.get('complete'):
        raise Refused('Artifact identity/schema/completion metadata mismatch.')
    if digest(path) != expected['sha256']:
        raise Refused('Artifact byte integrity mismatch.')


def bad_backup(lab, result):
    client = Client(lab)
    client.login()
    submit(lab, result, client, lab.fixture['heats'][0], lab.fixture['competitors'][:2], (11.1, 12.1))
    before = lab.snapshot()
    lab.worker('backup')
    backup = json.loads((lab.private / 'backup.json').read_text())
    positive = Path(backup['dest'])
    manifest = {'sha256': digest(positive), 'complete': True, 'fixture': lab.fixture,
                 'schema': 'a6b7c8d9e0f1', 'source': lab.owner.record['run_id']}
    for name, mutate_bytes, mutate_manifest in (
            ('truncated', lambda data: data[:80], {}),
            ('modified', lambda data: data[:50] + bytes([data[50] ^ 1]) + data[51:], {}),
            ('interrupted', lambda data: data[:1000], {'complete': False}),
            ('wrong-digest', lambda data: data, {'sha256': '0' * 64}),
            ('wrong-fixture', lambda data: data, {'fixture': {'tournament': 999}}),
            ('wrong-source', lambda data: data, {'source': 'another-synthetic-source'}),
            ('incompatible-metadata', lambda data: data, {'schema': 'not-supported'})):
        candidate = lab.private / (name + '.dump')
        candidate.write_bytes(mutate_bytes(positive.read_bytes()))
        try:
            validate_artifact(candidate, {**manifest, **mutate_manifest}, manifest)
        except Refused:
            check(result, 'reject ' + name + ' before restore', True)
        else:
            check(result, 'reject ' + name + ' before restore', False)
    # Recompute a checksum for a genuinely incompatible artifact: metadata and
    # integrity alone cannot pass the actual migrated-schema check.
    with lab.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE alembic_version SET version_num='incompatible-lab-schema'")
        connection.commit()
    (lab.private / 'backup.json').unlink()
    lab.worker('backup')
    wrong = json.loads((lab.private / 'backup.json').read_text())
    with lab.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE alembic_version SET version_num='a6b7c8d9e0f1'")
        connection.commit()
    restore(lab, wrong['dest'], 'incompatible')
    lab.database = 'incompatible'
    check(result, 'restored incompatible schema refused before activation',
          lab.query('SELECT version_num FROM alembic_version')[0]['version_num'], 'incompatible-lab-schema')
    lab.database = 'source'
    same_state(result, 'bad artifacts leave known good source unchanged', before, lab.snapshot())
    # Use only a new synthetic age identity. Restore decrypted ciphertext bytes.
    if not __import__('shutil').which('age', path=lab.env['PATH']):
        raise FileNotFoundError('age and age-keygen are required for synthetic encryption.')
    identity = lab.private / 'synthetic-age-key'
    lab.command(['age-keygen', '-o', identity])
    recipient = lab.command(['age-keygen', '-y', identity]).stdout.strip()
    cipher = lab.owner.path / 'synthetic-backup.dump.age'
    lab.command(['age', '-r', recipient, '-o', cipher, positive])
    decrypted = lab.private / 'decrypted.dump'
    lab.command(['age', '-d', '-i', identity, '-o', decrypted, cipher])
    check(result, 'ciphertext decrypts to exact retained backup bytes', digest(decrypted), backup['sha256'])
    wrong_key = lab.private / 'wrong-key'
    lab.command(['age-keygen', '-o', wrong_key])
    rejected = lab.command(['age', '-d', '-i', wrong_key, '-o', lab.private / 'wrong-output', cipher], ok=False)
    check(result, 'wrong synthetic identity rejected', rejected.returncode != 0)
    altered = lab.private / 'altered.age'
    data = cipher.read_bytes()
    altered.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    rejected = lab.command(['age', '-d', '-i', identity, '-o', lab.private / 'altered-output', altered], ok=False)
    check(result, 'altered ciphertext rejected', rejected.returncode != 0)
    restore(lab, decrypted, 'candidate')
    lab.database = 'candidate'
    same_state(result, 'actual decrypted artifact restores semantic checkpoint', before, lab.snapshot())
    lab.database = 'source'
    result['artifacts'].append({'path': cipher.name, 'sha256': digest(cipher),
                                 'encryption_identity_class': 'ephemeral synthetic age X25519',
                                 'key_retention': 'deleted; regenerate with a fresh run'})


def background_crash(lab, result):
    client = Client(lab)
    client.login()
    submit(lab, result, client, lab.fixture['heats'][0], lab.fixture['competitors'][:2], (11.1, 12.1))
    before = lab.snapshot()
    lab.arm('job-running')
    response = client.request('/reporting/1/export-results/async', {'csrf_token': client.csrf})
    check(result, 'real async export submission redirects to its persisted job', response['status'], 302)
    job_id = response['location'].rsplit('/', 1)[1]
    reached = lab.reached()
    job = lab.query('SELECT * FROM background_jobs WHERE id=%s', (job_id,))[0]
    check(result, 'job is persisted running before termination', job['status'], 'running')
    check(result, 'partial file is not advertised as completed', job['result_json'], None)
    started = time.monotonic()
    lab.kill_worker(reached)
    wait_until(lambda: lab.query('SELECT status FROM background_jobs WHERE id=%s', (job_id,))[0]['status']
               == 'interrupted', 42, 'default 30-second abandoned-owner lease')
    job = lab.query('SELECT * FROM background_jobs WHERE id=%s', (job_id,))[0]
    check(result, 'dead owner cannot advertise a partial export', job['result_json'], None)
    check(result, 'operator sees interrupted job, not download',
          client.request(response['location'])['status'], 302)
    lab.settings.update(job_id=job_id, obsolete_owner=job['owner_boot_id'])
    lab.write_settings()
    lab.worker('obsolete-job')
    check(result, 'real ownership predicate rejects obsolete completion',
          json.loads((lab.private / 'obsolete.json').read_text())['accepted'], False)
    check(result, 'rejected old completion remains interrupted',
          lab.query('SELECT status FROM background_jobs WHERE id=%s', (job_id,))[0]['status'], 'interrupted')
    lab.arm('job-running')
    retry = client.request('/reporting/1/export-results/async', {'csrf_token': client.csrf})
    new_id = retry['location'].rsplit('/', 1)[1]
    check(result, 'operator retry has a new job identity', new_id != job_id)
    lab.reached()
    lab.settings.update(job_id=new_id, obsolete_owner=job['owner_boot_id'])
    lab.write_settings()
    (lab.private / 'obsolete.json').unlink()
    lab.worker('obsolete-job')
    check(result, 'old boot cannot complete a running job owned by the replacement',
          json.loads((lab.private / 'obsolete.json').read_text())['accepted'], False)
    publish(lab.private / 'release', 'release')
    wait_until(lambda: lab.query('SELECT status FROM background_jobs WHERE id=%s', (new_id,))[0]['status']
               == 'completed', 20, 'legitimate export retry')
    workbook = client.request(retry['location'])
    check(result, 'verified complete workbook downloadable', workbook['body_bytes'][:2] == b'PK')
    import openpyxl
    book = openpyxl.load_workbook(io.BytesIO(workbook['body_bytes']), read_only=True)
    try:
        check(result, 'download contains real synthetic tournament results',
              any('Synthetic Alpha' in str(value) for sheet in book
                  for row in sheet.iter_rows(values_only=True) for value in row))
    finally:
        book.close()
    after = lab.snapshot()
    for key in ('background_jobs', 'audit_logs'):
        before.pop(key, None)
        after.pop(key, None)
    same_state(result, 'export interruption and retry preserve tournament scoring', before, after)
    result['fault'] = {'point': 'job-running', 'mechanism': 'SIGKILL owned Gunicorn worker',
                        'reached': True, 'observed': True, 'recovery': 'interrupted, operator retry'}
    result['timings_seconds']['fault_to_verified_export'] = time.monotonic() - started


def code_rollback(lab, result):
    from recovery_drills.lab import ROOT
    from recovery_drills.scenarios import stop_app
    client = Client(lab)
    client.login()
    ids, heats = lab.fixture['competitors'], lab.fixture['heats']
    submit(lab, result, client, heats[0], ids[:2], (11.1, 12.1))
    prior = submit(lab, result, client, heats[1], ids[2:], (9.1, 14.1))
    before = lab.snapshot()
    stop_app(lab)
    baseline = '39024813a15bf478f2cc8ddc0a28c50823badca1'
    archive = lab.private / 'baseline.tar'
    lab.command(['git', 'archive', '--format=tar', '--output', archive, baseline])
    checkout = lab.private / 'baseline-source'
    checkout.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(checkout, filter='data')
    lab.settings['application_root'] = str(checkout)
    lab.write_settings()
    lab.start_app()
    same_state(result, 'compatible baseline startup keeps accepted history', before, lab.snapshot())
    client = Client(lab)
    client.login()
    check(result, 'baseline authenticated report is usable',
          client.request('/reporting/1/event/1/results')['status'], 200)
    prior['csrf_token'] = client.form(heats[1])['csrf_token']
    accepted(result, client.score(heats[1], prior), replay=True)
    same_state(result, 'rollback preserves original durable receipt authority', before, lab.snapshot())
    submit(lab, result, client, heats[1], ids[2:], (10.1, 14.1))
    verify_values(lab, result, [11.1, 12.1, 10.1, 14.1])
    recorded = lab.snapshot()
    stop_app(lab)
    lab.settings.pop('application_root')
    lab.write_settings()
    lab.start_app()
    same_state(result, 'candidate returns without undoing baseline accepted data', recorded, lab.snapshot())
    result['compatibility'] = {'baseline': baseline, 'schema': 'a6b7c8d9e0f1',
                                'candidate_has_schema_changes': False,
                                'flow': 'scratch timed scoring; handicap semantic correction is deliberately separate'}
    result['limitations'].append('No schema downgrade. Incompatible schema activation is refused by R05; '
                                  'code rollback does not repair data or replace provider recovery.')


def sqlite_recovery(lab, result):
    from recovery_drills.lab import ROOT
    # Existing tests already kill a child process at the sidecar boundaries,
    # check the exclusive fence and prove the web route stages without swapping.
    output = lab.private / 'sqlite-lane'
    output.mkdir()
    env = dict(lab.env, DATABASE_URL='sqlite:///' + str(output / 'control.sqlite'),
               SECRET_KEY=lab.settings['secret'], FLASK_ENV='testing')
    junit = output / 'results.xml'
    command = [sys.executable, '-m', 'pytest', str(ROOT / 'tests/test_restore_workflow.py'),
               str(ROOT / 'tests/test_reporting_backup.py'), str(ROOT / 'tests/test_sms_backup.py'),
               '--basetemp', str(output / 'tmp'), '--junitxml', str(junit),
               '-p', 'no:cacheprovider', '-ra']
    # Tests using cwd/instance do so under this new owned scratch directory.
    env['PYTHONPATH'] = str(ROOT)
    with (output / 'output.log').open('w') as log:
        process = subprocess.run(command, cwd=output, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, timeout=180)
    if junit.exists():
        suites = ET.parse(junit).getroot().findall('testsuite')
        counts = {key: sum(int(suite.get(key, 0)) for suite in suites)
                  for key in ('tests', 'failures', 'errors', 'skipped')}
        result['pytest'] = counts
        publish(lab.owner.path / 'sqlite-results.xml', junit.read_bytes())
    # pytest creates convenience symlinks to its own temporary directories.
    # Remove only these verified internal links, without following them during
    # general cleanup or relaxing the rejection of external aliases.
    for path in (output / 'tmp').rglob('*current'):
        if path.is_symlink():
            if not path.resolve().is_relative_to(output / 'tmp'):
                raise Refused('pytest temporary link escaped the owned scratch tree.')
            path.unlink()
    check(result, 'existing real SQLite recovery lane succeeds', process.returncode, 0)
    check(result, 'nonempty SQLite restore coverage executed', counts['tests'] >= 35)
    result['limitations'].append('Linux SQLite process/fence/journal evidence. Windows ACL tests use '
                                  'their existing platform adapter; native Windows recovery NOT_PERFORMED. '
                                  'Schema/tournament/digest continuity is not signed backup provenance.')


FUNCTIONS = {'database-outage': database_outage, 'bad-backup': bad_backup,
             'background-worker-crash': background_crash, 'code-schema-rollback': code_rollback,
             'sqlite-offline-restore': sqlite_recovery}
