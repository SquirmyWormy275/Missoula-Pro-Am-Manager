"""A new local PostgreSQL cluster and one-worker Gunicorn application per run."""
from __future__ import annotations

import contextlib
import html.parser
import http.cookiejar
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.sql

from recovery_drills.ownership import (
    OwnedRun,
    Refused,
    child_environment,
    digest,
    no_alias,
    process_identity,
    publish,
    stop_process,
    wait_until,
)

ROOT = Path(__file__).resolve().parent.parent
VOLATILE = {
    'users': {'password_hash', 'updated_at'},
    'heats': {'locked_by_user_id', 'locked_at', 'version_id'},
    'background_jobs': {'owner_heartbeat_at'},
}


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


class Inputs(html.parser.HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.values = {}
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('type') == 'hidden' and attrs.get('name'):
            self.values[attrs['name']] = attrs.get('value', '')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, lab):
        self.lab = lab
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path, data=None, *, json_response=False, timeout=12):
        if not path.startswith('/') or path.startswith('//'):
            raise Refused('Client accepts paths on its owned origin only.')
        headers = {'Accept': 'application/json' if json_response else 'text/html'}
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        request = urllib.request.Request(self.lab.url + path, data=body, headers=headers)
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read()  # full response, never just status/headers
            if response.headers.get('Content-Length'):
                if len(payload) != int(response.headers['Content-Length']):
                    raise RuntimeError('Incomplete response body.')
            result = {'status': response.code, 'body': payload.decode('utf-8', errors='replace'),
                      'body_bytes': payload, 'content_type': response.headers.get('Content-Type'),
                      'location': response.headers.get('Location')}
        if json_response:
            try:
                result['json'] = json.loads(result['body'])
            except ValueError:
                result['json'] = None
        return result

    def login(self, user='judge'):
        form = self.request('/auth/login')
        if form['status'] != 200 or 'csrf_token' not in Inputs(form['body']).values:
            raise AssertionError('Real login form unavailable.')
        self.csrf = Inputs(form['body']).values['csrf_token']
        response = self.request('/auth/login', {
            **Inputs(form['body']).values, 'username': user,
            'password': 'synthetic-lab-password'})
        if response['status'] != 302 or '/auth/login' in (response['location'] or ''):
            raise AssertionError('Real CSRF-backed login failed.')

    def form(self, heat):
        response = self.request(f'/scoring/1/heat/{heat}/enter')
        fields = Inputs(response['body']).values
        if response['status'] != 200 or not all(fields.get(key) for key in (
                'csrf_token', 'heat_identity', 'scoring_state_digest', 'request_id')):
            raise AssertionError('Scoring form missing production identity fields.')
        return fields

    def score(self, heat, payload):
        self.lab.record('sent', heat, payload)
        try:
            response = self.request(f'/scoring/1/heat/{heat}/enter', payload, json_response=True)
        except (OSError, urllib.error.URLError):
            self.lab.record('ambiguous', heat, payload)
            raise
        state = 'client_acknowledged' if (
            response.get('json') or {}).get('receipt', {}).get('accepted') else 'rejected'
        self.lab.record(state, heat, payload, response)
        return response


class Lab:
    def __init__(self, owner: OwnedRun, pg_bin, library_path=None, tools_bin=None):
        self.owner = owner
        self.private = owner.path / 'private'
        self.private.mkdir(mode=0o700)
        self.bin = Path(pg_bin).resolve()
        self.clients = {name: str(self.bin / name) if (self.bin / name).is_file()
                        else shutil.which(name) for name in ('pg_dump', 'pg_restore')}
        self.env = child_environment(self.private, self.bin, library_path)
        if tools_bin:
            self.env['PATH'] = str(Path(tools_bin).resolve()) + ':' + self.env['PATH']
        self.cluster = self.private / 'pgdata'
        self.port = free_port()
        self.app_port = free_port()
        self.url = f'http://127.0.0.1:{self.app_port}'
        self.env['PROAM_LAB_RUN_ID'] = owner.record['run_id']
        self.settings = {'run_id': owner.record['run_id'], 'private': str(self.private),
                         'database_url': self.uri('source'), 'secret': secrets.token_hex(32),
                         'ports': [self.port, self.app_port], 'app_port': self.app_port}
        self.processes = []
        self.commands = []
        self.server = None
        self.database = 'source'
        self.system_id = None
        self.expected_tournaments = 2
        self.write_settings()
        self.ledger = []

    def uri(self, database, user='lab_app'):
        if database not in ('source', 'restored', 'candidate', 'incompatible'):
            raise Refused('Database was not allocated by this lab.')
        return f'postgresql://{user}@127.0.0.1:{self.port}/{database}'

    def write_settings(self):
        publish(self.private / 'settings.json', json.dumps(self.settings), replace=True)

    def command(self, args, *, timeout=90, ok=True):
        args = list(args)
        if Path(args[0]).name in self.clients:
            args[0] = self.clients[Path(args[0]).name]
        result = subprocess.run([str(arg) for arg in args], cwd=ROOT, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout, text=True)
        # Raw diagnostics stay private: they can contain paths and synthetic tokens.
        index = len(self.commands)
        (self.private / f'command-{index}.log').write_text(result.stdout)
        self.commands.append({'command': [str(arg) for arg in args], 'exit': result.returncode})
        if ok and result.returncode:
            raise RuntimeError(f'Lab command {index} failed ({Path(args[0]).name}).')
        return result

    def launch(self, args, kind):
        intent = self.owner.intent(kind, [str(arg) for arg in args])
        output = (self.private / (kind + '.log')).open('a')
        try:
            process = subprocess.Popen([str(arg) for arg in args], cwd=ROOT, env=self.env,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        finally:
            output.close()
        def stable_identity():
            current = process_identity(process.pid)
            return current if current and current['command'] == [str(arg) for arg in args] else None
        identity = wait_until(stable_identity, 3, 'allocated executable and argv')
        intent['pid'] = process.pid
        self.owner.remember(identity, kind)
        self.processes.append(process)
        return process

    def start_database(self):
        self.command([self.bin / 'initdb', '-D', self.cluster, '-U', 'lab_admin',
                      '--auth=trust', '--encoding=UTF8', '--no-locale',
                      '-L', self.bin.parent / 'share/postgresql']) if (
                          self.bin.parent / 'share/postgresql').is_dir() else self.command([
                              self.bin / 'initdb', '-D', self.cluster, '-U', 'lab_admin',
                              '--auth=trust', '--encoding=UTF8', '--no-locale'])
        self.pg = self.launch([self.bin / 'postgres', '-D', self.cluster,
                               '-h', '127.0.0.1', '-p', str(self.port),
                               '-k', '', '-c', 'fsync=on',
                               '-c', 'synchronous_commit=on'], 'postgres')
        def ready():
            try:
                with self.connect('postgres', 'lab_admin', verify=False) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT system_identifier::text FROM pg_control_system()')
                        return cursor.fetchone()[0]
            except psycopg2.OperationalError:
                return None
        self.system_id = wait_until(ready, 20, 'owned PostgreSQL startup')
        self.owner.record['postgres'] = {'system_identifier': self.system_id,
                                         'port': self.port, 'data': str(self.cluster),
                                         'databases': ['source', 'restored', 'candidate', 'incompatible']}
        self.owner.save()
        with self.connect('postgres', 'lab_admin') as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute('CREATE ROLE lab_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
                cursor.execute('CREATE ROLE lab_read LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
                cursor.execute('ALTER ROLE lab_read SET default_transaction_read_only = on')
                cursor.execute('CREATE DATABASE source OWNER lab_app')

    @contextlib.contextmanager
    def connect(self, database=None, user='lab_admin', *, verify=True):
        self.owner.validate()
        if database not in (None, 'postgres', 'source', 'restored', 'candidate', 'incompatible'):
            raise Refused('Unowned database requested.')
        identity = next(entry['identity'] for entry in reversed(self.owner.record['processes'])
                        if entry['kind'] == 'postgres')
        if process_identity(identity['pid']) != identity:
            raise Refused('Owned database process no longer has its recorded identity.')
        connection = psycopg2.connect(host='127.0.0.1', port=self.port,
                                      user=user, dbname=database or self.database,
                                      connect_timeout=3, options='-c statement_timeout=7000')
        try:
            if verify and user == 'lab_admin':
                with connection.cursor() as cursor:
                    cursor.execute('SELECT system_identifier::text FROM pg_control_system()')
                    if cursor.fetchone()[0] != self.system_id:
                        raise Refused('Database cluster identity changed.')
                connection.rollback()
            yield connection
        finally:
            connection.close()

    def query(self, sql, parameters=()):
        with self.connect() as connection:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(sql, parameters)
                return [dict(row) for row in cursor.fetchall()] if cursor.description else []

    def worker(self, action):
        return self.command([sys.executable, '-m', 'recovery_drills.worker',
                             self.private / 'settings.json', action], timeout=120)

    def initialize(self):
        self.start_database()
        self.worker('migrate')
        self.worker('seed')
        self.fixture = json.loads((self.private / 'fixture.json').read_text())
        if len(self.fixture['heats']) != 2 or len(self.fixture['competitors']) != 4:
            raise AssertionError('Nonempty independent fixture required.')
        self.start_app()

    def remember_workers(self):
        known = {item['identity']['pid'] for item in self.owner.record['processes']}
        for path in self.private.glob('worker-*.json'):
            item = json.loads(no_alias(path).read_text())
            identity = item['identity']
            if identity['pid'] in known or process_identity(identity['pid']) is None:
                continue
            masters = {item['identity']['pid'] for item in self.owner.record['processes']
                       if item['kind'] == 'gunicorn'}
            if (item['run_id'] != self.owner.record['run_id'] or item['parent'] not in masters
                    or identity != process_identity(identity['pid'])):
                raise Refused('Unbound worker marker.')
            self.owner.remember(identity, 'worker')

    def start_app(self):
        self.server = self.launch([sys.executable, '-m', 'recovery_drills.worker',
                                   self.private / 'settings.json', 'serve'], 'gunicorn')
        self.await_app()

    def await_app(self):
        def ready():
            try:
                response = Client(self).request('/health', json_response=True, timeout=1)
                return response['status'] == 200 and response['json'] is not None
            except (OSError, urllib.error.URLError):
                return False
        wait_until(ready, 25, 'Gunicorn health')
        self.remember_workers()

    def arm(self, point, request_id=None):
        for name in ('fault.json', 'reached.json', 'release'):
            (self.private / name).unlink(missing_ok=True)
        publish(self.private / 'fault.json', json.dumps({'point': point, 'request_id': request_id}))

    def reached(self):
        path = self.private / 'reached.json'
        wait_until(path.exists, 20, 'exact fault barrier')
        return json.loads(path.read_text())

    def kill_worker(self, barrier):
        self.remember_workers()
        identity = barrier['process']
        if identity not in [item['identity'] for item in self.owner.record['processes']
                            if item['kind'] == 'worker']:
            raise Refused('Fault did not identify an owned Gunicorn worker.')
        self.owner.signal(identity, signal.SIGKILL)
        wait_until(lambda: process_identity(identity['pid']) is None, 5, 'SIGKILL observation')
        for name in ('fault.json', 'reached.json', 'release'):
            (self.private / name).unlink(missing_ok=True)
        self.await_app()  # existing master automatically respawns one worker

    def snapshot(self):
        tables = self.query("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        result = {}
        for row in tables:
            table = row['tablename']
            with self.connect() as connection:
                with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    cursor.execute(psycopg2.sql.SQL('SELECT * FROM {}').format(psycopg2.sql.Identifier(table)))
                    rows = [dict(value) for value in cursor.fetchall()]
            cleaned = []
            for value in rows:
                if table == 'audit_logs' and value.get('action') == 'login':
                    continue
                cleaned.append({key: str(item) if item is not None else None
                                for key, item in value.items() if key not in VOLATILE.get(table, set())})
            result[table] = sorted(cleaned, key=lambda item: json.dumps(item, sort_keys=True))
        if not result.get('event_results') or len(result.get('tournaments', [])) != self.expected_tournaments:
            raise AssertionError('Semantic comparison requires a populated two-tournament database.')
        return result

    def record(self, state, heat, payload, response=None):
        # Exclude transport credentials, retaining logical payload and identity.
        self.ledger.append({'state': state, 'heat': heat, 'monotonic': time.monotonic(),
                            'payload': {key: value for key, value in payload.items()
                                        if key not in ('csrf_token', 'replay_token')},
                            'response_status': response.get('status') if response else None})
        publish(self.owner.path / 'ledger.json', json.dumps(self.ledger, indent=2), replace=True)

    def cleanup(self):
        self.remember_workers()
        self.owner.stop_all()
        for process in self.processes:
            process.wait(timeout=8)
        self.owner.cleanup_scratch()
        # Remove only the exact new private tree, after every process is gone.
        no_alias(self.private)
        for path in self.private.rglob('*'):
            if path.is_symlink() or (path.is_file() and path.stat().st_nlink != 1):
                raise Refused('Private cleanup tree contains an alias; retained for inspection.')
        shutil.rmtree(self.private)
        self.owner.record['cleanup'] = 'PASS'
        self.owner.save()


def score_payload(form, ids, values):
    # Form metadata retained exactly for receipt retry; only CSRF may be refreshed.
    payload = dict(form)
    for competitor, value in zip(ids, values):
        payload.update({f't1_run1_{competitor}': str(value - 0.1),
                        f't2_run1_{competitor}': str(value + 0.1),
                        f'status_{competitor}': 'completed'})
    payload['payload_sha256'] = ''
    return payload
