"""Bounded synthetic runner and atomic, truthful reports."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from recovery_drills.ownership import OwnedRun, Refused, no_alias, publish

SCHEMA = 'proam-recovery-drill-v1'


def source():
    root = Path(__file__).resolve().parent.parent
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root)
    patch = git('diff', 'HEAD', '--binary')
    for name in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
        if name:
            patch += name + b'\0' + (root / os.fsdecode(name)).read_bytes()
    return {'commit': git('rev-parse', 'HEAD').decode().strip(), 'dirty': bool(patch),
            'patch_sha256': hashlib.sha256(patch).hexdigest() if patch else None}


def environment(pg_bin):
    distributions = ('Flask', 'Flask-SQLAlchemy', 'SQLAlchemy', 'psycopg2-binary', 'gunicorn', 'strathmark')
    versions = {name: importlib.metadata.version(name) for name in distributions}
    producer = importlib.metadata.distribution('strathmark')
    direct = json.loads(producer.read_text('direct_url.json') or '{}')
    clients = {}
    for name in ('pg_dump', 'pg_restore'):
        executable = str(Path(pg_bin) / name) if (Path(pg_bin) / name).is_file() else shutil.which(name)
        clients[name] = subprocess.check_output([executable, '--version'], text=True).strip() if executable else None
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'dependencies': versions, 'strathmark_source': direct.get('vcs_info'),
            'app_process_model': 'gunicorn: 1 worker, 4 threads, timeout 120; TESTING=False; CSRF enabled',
            'postgres_server_binary': str(Path(pg_bin).resolve() / 'postgres'),
            'postgres_clients': clients,
            'network': 'new loopback-only namespace; no external routes',
            'production_server_version': 'NOT_OBSERVED'}


def markdown(report):
    lines = ['# Synthetic Missoula recovery drill', '',
             f"Run: `{report['run_id']}`; source: `{report['source']['commit']}`; "
             f"dirty: {report['source']['dirty']}", '',
             f"Overall: **{report['status']}**. Production recovery qualification: **NOT_PERFORMED**.", '',
             '| Scenario | Result | Cleanup |', '| --- | --- | --- |']
    for item in report['scenarios']:
        lines.append(f"| {item['scenario_id']} | {item['scenario_status']} | {item['cleanup_status']} |")
    for item in report['scenarios']:
        lines.extend(['', f"## {item['scenario_id']}", '',
                      f"Fault: {json.dumps(item['fault'], sort_keys=True)}", '',
                      f"Timings (monotonic seconds): {json.dumps(item['timings_seconds'])}", ''])
        for check in item['checks']:
            lines.append(f"- {check['status']}: {check['name']}")
            if check['status'] != 'PASS':
                lines.append(f"  Expected/actual: {json.dumps(check, default=str)}")
        for limit in item['limitations']:
            lines.append('- Limitation: ' + limit)
    return '\n'.join(lines) + '\n'


def validate_report(report):
    if report.get('schema_version') != SCHEMA or not report.get('scenarios'):
        raise Refused('Corrupt, empty or unsupported report.')
    statuses = {'PASS', 'FAIL', 'BLOCKED', 'NOT_RUN', 'INVALID_RUN'}
    for item in report['scenarios']:
        if item.get('scenario_status') not in statuses or item.get('cleanup_status') not in statuses:
            raise Refused('Invalid scenario status.')
        if item['scenario_status'] == 'PASS' and not item.get('checks'):
            raise Refused('An empty scenario cannot pass.')
        if item['scenario_status'] == 'PASS' and item.get('scenario_id') in (
                'score-precommit-crash', 'score-postcommit-response-loss', 'database-outage',
                'browser-offline-replay', 'background-worker-crash', 'shadow-delivery'):
            if not item.get('fault', {}).get('reached') or not item.get('fault', {}).get('observed'):
                raise Refused('A process fault scenario cannot pass without an observed fault.')
    success = all(item['scenario_status'] == 'PASS' and item['cleanup_status'] == 'PASS'
                  and all(check['status'] == 'PASS' for check in item['checks'])
                  for item in report['scenarios'])
    if (report.get('status') == 'PASS') != success:
        raise Refused('Report aggregate contradicts selected checks or cleanup.')
    return report


def sanitized(report, lab_paths=()):
    """Public reports contain evidence, never private storage paths or secrets."""
    raw = json.dumps(report, default=str)
    for value, replacement in lab_paths:
        raw = raw.replace(value, replacement)
    raw = raw.replace(str(Path.home()), '[user-home]')
    return json.loads(raw)


def prerequisites(pg_bin):
    return {name: str(Path(pg_bin) / name) if (Path(pg_bin) / name).is_file()
            else shutil.which(name) if name in ('pg_dump', 'pg_restore') else None
            for name in ('initdb', 'postgres', 'pg_dump', 'pg_restore')}


def run(args):
    from recovery_drills.extended import FUNCTIONS as extensions
    from recovery_drills.lab import Lab
    from recovery_drills.scenarios import FUNCTIONS, SCENARIOS
    FUNCTIONS.update(extensions)
    from recovery_drills.browser import run_browser
    FUNCTIONS['browser-offline-replay'] = run_browser
    from recovery_drills.shadow import run_shadow
    FUNCTIONS['shadow-delivery'] = run_shadow
    selected = [args.scenario] if args.scenario else [name for name, (_, suite) in SCENARIOS.items()
                                                      if args.suite == 'all' or suite == 'core']
    owner = OwnedRun(args.output, create=True)
    report = {'schema_version': SCHEMA, 'run_id': owner.record['run_id'], 'source': source(),
              'utc_started': datetime.now(timezone.utc).isoformat(),
              'environment': environment(args.pg_bin),
              'scope': {'data': 'synthetic', 'production_access': False,
                        'evidence_kind': 'local-process-and-database-drill'},
              'status': 'NOT_RUN', 'scenarios': []}
    # Persist incomplete status before allocating any external resource.
    publish(owner.path / 'incomplete.json', json.dumps(report, indent=2))
    try:
        for name in selected:
            item = {'scenario_id': name, 'requirement': SCENARIOS[name][0],
                    'scenario_status': 'NOT_RUN', 'cleanup_status': 'NOT_RUN',
                    'fault': {'reached': False, 'observed': False}, 'checks': [],
                    'request_reconciliation': {'lost_expected': None, 'lost_unexpected': None,
                                               'duplicate_effects': None, 'conflicting_effects': None,
                                               'observed_committed_without_ack': None},
                    'timings_seconds': {}, 'limitations': [], 'artifacts': []}
            report['scenarios'].append(item)
            child_path = owner.path / name
            owner.record.setdefault('children', []).append(str(child_path))
            owner.save()
            child = OwnedRun(child_path, create=True)
            lab = None
            parent_environment = None
            started = time.monotonic()
            try:
                if not all(prerequisites(args.pg_bin).values()):
                    raise FileNotFoundError('Owned local PostgreSQL executables unavailable.')
                if name not in FUNCTIONS:
                    raise FileNotFoundError('Scenario implementation is not available yet.')
                lab = Lab(child, args.pg_bin, args.pg_library, args.tools_bin)
                # A controlled peer also imports reviewed contract helpers in
                # the coordinator. Give those imports the same owned storage
                # before loading them, rather than retaining the caller's home.
                parent_environment = dict(os.environ)
                os.environ.clear()
                os.environ.update(lab.env)
                os.environ.update(DATABASE_URL=lab.settings['database_url'],
                                  SECRET_KEY=lab.settings['secret'])
                lab.initialize()
                item['timings_seconds']['provisioning'] = time.monotonic() - started
                item['environment'] = lab.query('SELECT version() AS server, '
                    "current_setting('fsync') AS fsync,current_setting('synchronous_commit') AS synchronous_commit")
                item['schema_revision'] = lab.query('SELECT version_num FROM alembic_version')
                FUNCTIONS[name](lab, item)
                item['runtime_identity'] = [
                    {key: value for key, value in json.loads(path.read_text()).items()
                     if key in ('application_source', 'testing', 'csrf')}
                    for path in sorted(lab.private.glob('worker-*.json'))]
                # These are derived from successful scenario assertions; an
                # unexecuted/failed scenario retains unknown counters.
                if name in ('restore-fresh-postgres', 'score-precommit-crash', 'score-postcommit-response-loss'):
                    for key, value in {'lost_expected': [], 'lost_unexpected': 0,
                                       'duplicate_effects': 0, 'conflicting_effects': 0,
                                       'observed_committed_without_ack': 0}.items():
                        if item['request_reconciliation'][key] is None:
                            item['request_reconciliation'][key] = value
                item['scenario_status'] = 'PASS'
            except FileNotFoundError as error:
                item['scenario_status'] = 'BLOCKED'
                item['limitations'].append(str(error))
            except TimeoutError as error:
                item['scenario_status'] = 'INVALID_RUN'
                item['limitations'].append(str(error))
            except Exception as error:
                item['scenario_status'] = 'FAIL'
                # Do not put HTTP bodies, local URLs, cookies or tracebacks in public evidence.
                item['limitations'].append(type(error).__name__ + ': ' + str(error))
                if lab:
                    import traceback
                    (lab.private / 'failure.txt').write_text(traceback.format_exc())
            finally:
                try:
                    if lab:
                        if item['scenario_status'] != 'PASS':
                            diagnostics = []
                            for path in sorted(lab.private.glob('*.log')) + list(lab.private.glob('failure.txt')):
                                text = path.read_text(errors='replace')[-12000:]
                                text = text.replace(lab.settings['secret'], '[secret]')
                                text = text.replace(str(lab.private), '[private]')
                                text = text.replace(lab.settings['database_url'], '[owned database]')
                                diagnostics.append(path.name + '\n' + text)
                            publish(child.path / 'diagnostics.txt', '\n'.join(diagnostics))
                        lab.cleanup()
                    else:
                        child.stop_all()
                    item['cleanup_status'] = 'PASS'
                except Exception as error:
                    item['cleanup_status'] = 'FAIL'
                    item['limitations'].append('Cleanup refused/failed: ' + type(error).__name__)
                if parent_environment is not None:
                    os.environ.clear()
                    os.environ.update(parent_environment)
                child.close()
                item['timings_seconds']['total_including_cleanup'] = time.monotonic() - started
                private_values = [(str(child.path / 'private'), '[private]')]
                if lab:
                    private_values.extend((lab.settings[key], '[ephemeral-secret]') for key in
                                          ('secret', 'peer_token', 'peer_key') if key in lab.settings)
                report = sanitized(report, private_values)
                publish(owner.path / 'incomplete.json', json.dumps(report, indent=2), replace=True)
                print(f"{name}: {item['scenario_status']}; cleanup {item['cleanup_status']}", flush=True)
        report['status'] = 'PASS' if all(item['scenario_status'] == 'PASS'
                                       and item['cleanup_status'] == 'PASS' for item in report['scenarios']) else 'FAIL'
        validate_report(report)
        owner.record['cleanup'] = 'PASS' if all(item['cleanup_status'] == 'PASS' for item in report['scenarios']) else 'FAIL'
        owner.save()
        publish(owner.path / 'report.json', json.dumps(report, indent=2, default=str))
        publish(owner.path / 'report.md', markdown(report))
        owner.record['report_sha256'] = hashlib.sha256((owner.path / 'report.json').read_bytes()).hexdigest()
        owner.save()
        (owner.path / 'incomplete.json').unlink()
        return 0 if report['status'] == 'PASS' else 1
    finally:
        owner.close()


def main(argv=None):
    from recovery_drills.scenarios import SCENARIOS
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    doctor = sub.add_parser('doctor')
    doctor.add_argument('--pg-bin', default=os.environ.get('PROAM_LAB_PG_BIN', '/usr/bin'))
    execute = sub.add_parser('run')
    execute.add_argument('--output', required=True)
    selection = execute.add_mutually_exclusive_group()
    selection.add_argument('--suite', choices=['core', 'all'], default='core')
    selection.add_argument('--scenario', choices=list(SCENARIOS))
    execute.add_argument('--pg-bin', default=os.environ.get('PROAM_LAB_PG_BIN', '/usr/bin'))
    execute.add_argument('--pg-library', default=None)
    execute.add_argument('--tools-bin', default=None)
    for name in ('report', 'cleanup'):
        command = sub.add_parser(name)
        command.add_argument('--run', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'list':
            print(json.dumps(SCENARIOS, indent=2))
            return 0
        if args.command == 'doctor':
            print(json.dumps({'prerequisites': prerequisites(args.pg_bin),
                              'linux_pidfd': hasattr(os, 'pidfd_open'),
                              'isolation': 'new private local cluster; no external database target accepted',
                              'docker': 'not used; inherited Docker endpoints cannot redirect this backend',
                              'age': shutil.which('age'), 'browser': shutil.which('chromium')}, indent=2))
            return 0 if all(prerequisites(args.pg_bin).values()) else 2
        if args.command == 'run':
            from recovery_drills.network import isolated
            if not isolated():
                # No routes out of this namespace. Dependencies are installed
                # separately before running; the drill itself is offline.
                command = ['unshare', '--user', '--map-current-user', '--net', '--keep-caps',
                           sys.executable, '-m', 'recovery_drills.network', *(argv or sys.argv[1:])]
                child_env = {key: os.environ[key] for key in ('PATH', 'LD_LIBRARY_PATH') if key in os.environ}
                child_env['PYTHONDONTWRITEBYTECODE'] = '1'
                return subprocess.run(command, cwd=Path(__file__).resolve().parent.parent,
                                      env=child_env, timeout=1200).returncode
            return run(args)
        owner = OwnedRun(args.run)
        try:
            if args.command == 'report':
                report_path = no_alias(owner.path / 'report.json')
                expected = owner.record.get('report_sha256')
                if expected and hashlib.sha256(report_path.read_bytes()).hexdigest() != expected:
                    raise Refused('Report bytes no longer match the run publication record.')
                report = validate_report(json.loads(report_path.read_text()))
                print(markdown(report))
                return 0 if report['status'] == 'PASS' else 1
            for path in owner.record.get('children', []):
                if Path(path).parent != owner.path:
                    raise Refused('Child run escaped its recorded parent.')
                child = OwnedRun(path)
                try:
                    child.stop_all()
                    child.cleanup_scratch()
                    private = child.path / 'private'
                    if private.exists():
                        no_alias(private)
                        for item in private.rglob('*'):
                            no_alias(item)
                        shutil.rmtree(private)
                    child.record['cleanup'] = 'PASS'
                    child.save()
                finally:
                    child.close()
            owner.record['cleanup'] = 'PASS'
            owner.save()
            print('Exact-run cleanup complete; retained reports were not removed.')
            return 0
        finally:
            owner.close()
    except (Refused, OSError, ValueError) as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
