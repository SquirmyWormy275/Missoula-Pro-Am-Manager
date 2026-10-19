"""The synthetic harness must fail closed before touching any unowned target."""
import copy
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from recovery_drills.cli import main, validate_report
from recovery_drills.lab import Client, Lab
from recovery_drills.ownership import (
    OwnedRun,
    Refused,
    child_environment,
    no_alias,
    process_identity,
    publish,
    stop_process,
    wait_until,
)

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux process lab uses flock/pidfd; native Windows recovery is a separate lane')


def _started_child_identity(process, command):
    # /proc may expose an empty cmdline during exec; match the same readiness
    # boundary as Lab.launch before recording an immutable process identity.
    def ready():
        current = process_identity(process.pid)
        return current if current and current['command'] == command else None
    return wait_until(ready, 3, 'allocated executable and argv')


def test_preexisting_output_and_live_exact_run_are_refused(tmp_path):
    path = tmp_path / 'run'
    owner = OwnedRun(path, create=True)
    try:
        with pytest.raises(FileExistsError):
            OwnedRun(path, create=True)
        with pytest.raises(Refused, match='active'):
            OwnedRun(path)
    finally:
        owner.close()


def test_alias_and_traversal_refusals_preserve_original(tmp_path):
    protected = tmp_path / 'protected.db'
    protected.write_bytes(b'retained')
    link = tmp_path / 'alias'
    link.symlink_to(protected)
    hard = tmp_path / 'hard'
    hard.hardlink_to(protected)
    for path in (link, hard, tmp_path / 'nested/../run'):
        with pytest.raises(Refused):
            no_alias(path)
    assert protected.read_bytes() == b'retained'


def test_operational_configuration_never_enters_child(tmp_path, monkeypatch):
    for key in ('DATABASE_URL', 'PGSERVICE', 'PGHOST', 'DOCKER_HOST', 'HTTPS_PROXY',
                'AWS_SECRET_ACCESS_KEY', 'STRATHMARK_SUPABASE_KEY', 'RAILWAY_TOKEN',
                'PROAM_CRASH_AT', 'PYTHONPATH'):
        monkeypatch.setenv(key, 'operational-sentinel')
    environment = child_environment(tmp_path, '/usr/bin')
    assert 'operational-sentinel' not in environment.values()
    assert not (set(environment) & {'DATABASE_URL', 'PGHOST', 'PGSERVICE', 'DOCKER_HOST',
                                   'AWS_SECRET_ACCESS_KEY', 'RAILWAY_TOKEN', 'HTTPS_PROXY'})
    assert environment['HOME'] == str(tmp_path)


def test_arbitrary_database_and_docker_targets_have_no_cli(tmp_path):
    for option in ('--database-url', '--docker-host', '--container', '--pid'):
        with pytest.raises(SystemExit) as result:
            main(['run', '--output', str(tmp_path / 'new'), option, 'operational'])
        assert result.value.code == 2
    assert not (tmp_path / 'new').exists()


def test_pid_reuse_identity_is_rejected_without_signalling():
    identity = process_identity(os.getpid())
    wrong = {**identity, 'start': 'not-this-process'}
    with pytest.raises(Refused, match='identity changed'):
        stop_process(wrong, signal.SIGTERM)
    assert process_identity(os.getpid()) == identity


def test_unallocated_process_and_operational_storage_are_refused(tmp_path):
    owner = OwnedRun(tmp_path / 'run', create=True)
    try:
        with pytest.raises(Refused, match='not allocated'):
            owner.signal(process_identity(os.getpid()))
    finally:
        owner.close()
    operational = tmp_path / 'instance'
    operational.mkdir()
    with pytest.raises(Refused, match='Operational storage'):
        OwnedRun(operational / 'new', create=True)
    assert list(operational.iterdir()) == []


def test_browser_scratch_stays_short_with_a_private_long_tmpdir(tmp_path, monkeypatch):
    owner = OwnedRun(tmp_path / 'run', create=True)
    monkeypatch.setenv('TMPDIR', str(tmp_path / ('long-' * 30)))
    try:
        scratch = owner.scratch()
        assert scratch.parent == Path('/tmp')
        assert len(str(scratch / 'org.chromium.Chromium.abcdef/SingletonSocket')) < 108
        assert json.loads((scratch / 'owner.json').read_text())['run_id'] == owner.record['run_id']
    finally:
        owner.cleanup_scratch()
        owner.close()


def test_cleanup_of_one_run_preserves_a_second_live_run(tmp_path):
    owners = [OwnedRun(tmp_path / name, create=True) for name in ('one', 'two')]
    processes = []
    try:
        for owner in owners:
            command = [sys.executable, '-c', 'import time; time.sleep(25)']
            owner.intent('sleeper', command)
            process = subprocess.Popen(command, env={'PROAM_LAB_RUN_ID': owner.record['run_id']})
            processes.append(process)
            identity = _started_child_identity(process, command)
            owner.remember(identity, 'sleeper')
        owners[0].stop_all()
        processes[0].wait(timeout=5)
        assert processes[1].poll() is None
        assert owners[1].record['path'] == str(tmp_path / 'two')
    finally:
        for owner in owners:
            owner.stop_all()
            owner.close()
        for process in processes:
            process.wait(timeout=5)


def test_interrupted_allocation_is_reconciled_by_exact_intent(tmp_path):
    owner = OwnedRun(tmp_path / 'run', create=True)
    command = [sys.executable, '-c', 'import time; time.sleep(25)']
    owner.intent('sleeper', command)
    process = subprocess.Popen(command, env={'PROAM_LAB_RUN_ID': owner.record['run_id']})
    try:
        _started_child_identity(process, command)
        owner.stop_all()  # no remember() call; simulates runner death after spawn
        process.wait(timeout=5)
        assert process.returncode != 0
        assert any(item['identity']['pid'] == process.pid for item in owner.record['processes'])
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        owner.close()


def test_missing_barrier_never_becomes_success():
    with pytest.raises(TimeoutError, match='barrier'):
        wait_until(lambda: False, .01, 'fault barrier')


def test_report_atomic_failure_and_existing_evidence_are_preserved(tmp_path, monkeypatch):
    path = tmp_path / 'report.json'
    publish(path, '{"status":"FAIL"}')
    with pytest.raises(Refused):
        publish(path, '{"status":"PASS"}')
    def fail(*args, **kwargs):
        raise OSError('injected publication failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError, match='publication'):
        publish(path, '{"status":"PASS"}', replace=True)
    assert json.loads(path.read_text())['status'] == 'FAIL'
    assert not list(tmp_path.glob('.publish-*'))


@pytest.mark.parametrize('scenario,cleanup,checks', [
    ('NOT_RUN', 'PASS', []), ('BLOCKED', 'PASS', []),
    ('INVALID_RUN', 'PASS', []), ('PASS', 'FAIL', [{'status': 'PASS'}]),
    ('PASS', 'PASS', []), ('PASS', 'PASS', [{'status': 'FAIL'}]),
])
def test_aggregate_cannot_pass_missing_work_failed_checks_or_cleanup(scenario, cleanup, checks):
    report = {'schema_version': 'proam-recovery-drill-v1', 'status': 'PASS',
              'scenarios': [{'scenario_status': scenario, 'cleanup_status': cleanup, 'checks': checks}]}
    with pytest.raises(Refused):
        validate_report(report)


def test_corrupt_report_and_normal_runtime_instrumentation_boundary():
    with pytest.raises(Refused):
        validate_report({'status': 'PASS'})
    root = Path(__file__).resolve().parent.parent
    # Ordinary deployment has no import path into the lab adapter. A client
    # header or environment flag cannot install the Session.commit wrapper.
    import ast
    for folder in ('services', 'routes'):
        for path in (root / folder).rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or '').startswith('recovery_drills')
                if isinstance(node, ast.Import):
                    assert all(not name.name.startswith('recovery_drills') for name in node.names)
    assert 'recovery_drills' not in (root / 'app.py').read_text()


def test_report_refuses_corrupt_bytes_and_interrupted_publication(tmp_path):
    path = tmp_path / 'run'
    owner = OwnedRun(path, create=True)
    owner.close()
    (path / 'incomplete.json').write_text('{"status":"NOT_RUN"}')
    assert main(['report', '--run', str(path)]) == 2
    (path / 'report.json').write_text('{broken')
    assert main(['report', '--run', str(path)]) == 2
    assert (path / 'incomplete.json').exists()


def test_unreached_fault_and_empty_fixture_cannot_pass():
    report = {'schema_version': 'proam-recovery-drill-v1', 'status': 'PASS', 'scenarios': [{
        'scenario_id': 'score-precommit-crash', 'scenario_status': 'PASS',
        'cleanup_status': 'PASS', 'checks': [{'status': 'PASS'}],
        'fault': {'reached': False, 'observed': False}}]}
    with pytest.raises(Refused, match='observed fault'):
        validate_report(report)
    with pytest.raises(AssertionError, match='populated'):
        Lab.snapshot(SimpleNamespace(query=lambda sql: [], expected_tournaments=2))


def test_partial_body_and_login_page_are_not_successful_score_responses():
    class Response:
        headers = {'Content-Length': '100'}
        code = 200
        def read(self):
            return b'partial'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    client = Client(SimpleNamespace(url='http://127.0.0.1:12345'))
    client.opener = SimpleNamespace(open=lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match='Incomplete response'):
        client.request('/scoring/1/heat/1/enter')
    from recovery_drills.scenarios import accepted
    result = {'checks': []}
    with pytest.raises(AssertionError, match='JSON acceptance'):
        accepted(result, {'status': 200, 'json': None, 'body': '<form>Login</form>'})
    assert result['checks'][0]['status'] == 'FAIL'
