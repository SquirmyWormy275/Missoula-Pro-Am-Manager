from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

import services.restore_workflow as restore_workflow
from services.restore_workflow import (
    MaintenanceFenceBusy,
    RestoreValidationError,
    apply_staged_sqlite_restore,
    configure_sqlite_process_fence,
    database_activity_fence,
    prepare_sqlite_restore,
    sha256_file,
    sqlite_db_path_from_uri,
    stage_sqlite_restore,
    validate_sqlite_restore_file,
)


@pytest.fixture
def workspace_tmpdir() -> Path:
    path = Path.cwd() / 'instance' / f'restore-workflow-{uuid.uuid4().hex}'
    path.mkdir(parents=True, exist_ok=False)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _make_sqlite_db(
    path,
    *,
    revision='rev1',
    tables=None,
    tournament=(1, 'Missoula Pro-Am', 2027),
    tournaments=None,
    marker='original',
    invalid_foreign_key=False,
):
    tables = tables or {'tournaments', 'events', 'event_results', 'heats', 'users'}
    tournament_rows = list(tournaments) if tournaments is not None else [tournament]
    conn = sqlite3.connect(path)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
        conn.execute('INSERT INTO alembic_version (version_num) VALUES (?)', (revision,))
        if 'tournaments' in tables:
            conn.execute(
                'CREATE TABLE tournaments ('
                'id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, '
                'year INTEGER NOT NULL, status VARCHAR(50) NOT NULL DEFAULT "setup", '
                'providing_shirts BOOLEAN NOT NULL DEFAULT 0, '
                'schedule_config TEXT)'
            )
            conn.executemany(
                'INSERT INTO tournaments (id, name, year) VALUES (?, ?, ?)',
                tournament_rows,
            )
        if 'users' in tables:
            conn.execute(
                'CREATE TABLE users ('
                'id INTEGER PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE, '
                'password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, '
                'tournament_id INTEGER REFERENCES tournaments(id) ON DELETE SET NULL, '
                'display_name VARCHAR(200), is_active_user BOOLEAN NOT NULL DEFAULT 1)'
            )
            conn.execute('CREATE INDEX ix_users_tournament_id ON users(tournament_id)')
        if 'events' in tables:
            conn.execute(
                'CREATE TABLE events ('
                'id INTEGER PRIMARY KEY, tournament_id INTEGER NOT NULL '
                'REFERENCES tournaments(id) ON DELETE CASCADE, '
                'name VARCHAR(200) NOT NULL, event_type VARCHAR(20) NOT NULL, '
                'scoring_type VARCHAR(20) NOT NULL, '
                'scoring_order VARCHAR(20) NOT NULL DEFAULT "lowest_wins", '
                'status VARCHAR(20) NOT NULL DEFAULT "pending", '
                'is_finalized BOOLEAN NOT NULL DEFAULT 0)'
            )
            conn.execute(
                'CREATE INDEX ix_events_tournament_type_status '
                'ON events(tournament_id, event_type, status)'
            )
        if 'heats' in tables:
            conn.execute(
                'CREATE TABLE heats ('
                'id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL '
                'REFERENCES events(id) ON DELETE CASCADE, '
                'heat_number INTEGER NOT NULL, run_number INTEGER NOT NULL DEFAULT 1, '
                'status VARCHAR(20) NOT NULL DEFAULT "pending", '
                'version_id INTEGER NOT NULL DEFAULT 1, '
                'locked_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, '
                'UNIQUE(event_id, heat_number, run_number))'
            )
            conn.execute('CREATE INDEX ix_heats_event_status ON heats(event_id, status)')
        if 'event_results' in tables:
            conn.execute(
                'CREATE TABLE event_results ('
                'id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL '
                'REFERENCES events(id) ON DELETE CASCADE, '
                'heat_id INTEGER REFERENCES heats(id) ON DELETE SET NULL, '
                'competitor_id INTEGER NOT NULL, competitor_type VARCHAR(20) NOT NULL, '
                'result_value REAL, final_position INTEGER, '
                'points_awarded NUMERIC(6, 2) NOT NULL DEFAULT 0, '
                'status VARCHAR(20) NOT NULL DEFAULT "pending", '
                'version_id INTEGER NOT NULL DEFAULT 1, '
                'UNIQUE(event_id, competitor_id, competitor_type))'
            )
            conn.execute(
                'CREATE INDEX ix_event_results_event_status '
                'ON event_results(event_id, status)'
            )
        conn.execute('CREATE TABLE restore_marker (value TEXT NOT NULL)')
        conn.execute('INSERT INTO restore_marker VALUES (?)', (marker,))
        conn.execute(
            'CREATE TABLE audit_logs ('
            'id INTEGER PRIMARY KEY, action TEXT NOT NULL, details_json TEXT)'
        )
        conn.execute(
            'INSERT INTO audit_logs (action, details_json) VALUES (?, ?)',
            ('fixture_created', '{}'),
        )
        conn.execute(
            'CREATE TABLE parent_rows (id INTEGER PRIMARY KEY)'
        )
        conn.execute(
            'CREATE TABLE child_rows ('
            'id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL '
            'REFERENCES parent_rows(id))'
        )
        if invalid_foreign_key:
            conn.commit()
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute('INSERT INTO child_rows VALUES (1, 999)')
        conn.commit()
    finally:
        conn.close()


def _marker(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute('SELECT value FROM restore_marker').fetchone()[0]
    finally:
        conn.close()


def test_validate_sqlite_restore_file_accepts_matching_schema(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head')

    result = validate_sqlite_restore_file(
        str(upload), str(current), tournament_id=1,
    )

    assert result['target_path'] == str(current)
    assert result['current_revision'] == 'head'
    assert result['uploaded_revision'] == 'head'
    assert result['integrity_check'] == 'ok'
    assert result['foreign_key_violations'] == 0
    assert result['tournament_identity_matches'] is True
    assert result['database_identity_matches'] is True
    assert result['database_identity_tournament_count'] == 1
    assert result['required_schema_matches'] is True
    assert len(result['database_identity_sha256']) == 64
    assert len(result['required_schema_sha256']) == 64


def test_validate_sqlite_restore_file_rejects_missing_tables(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head', tables={'tournaments'})

    with pytest.raises(RuntimeError, match='missing required tables'):
        validate_sqlite_restore_file(str(upload), str(current), tournament_id=1)


def test_validate_sqlite_restore_file_rejects_revision_mismatch(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='old')

    with pytest.raises(RuntimeError, match='schema revision does not match'):
        validate_sqlite_restore_file(str(upload), str(current), tournament_id=1)


def test_validate_sqlite_restore_file_rejects_foreign_key_damage(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head', invalid_foreign_key=True)

    with pytest.raises(RestoreValidationError, match='foreign key'):
        validate_sqlite_restore_file(str(upload), str(current), tournament_id=1)


def test_validate_sqlite_restore_file_rejects_wrong_tournament(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(
        upload,
        revision='head',
        tournament=(1, 'Different Tournament', 2027),
    )

    with pytest.raises(RestoreValidationError, match='tournament identity'):
        validate_sqlite_restore_file(str(upload), str(current), tournament_id=1)


def test_validate_sqlite_restore_file_rejects_required_table_structure_drift(
    workspace_tmpdir,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head')
    conn = sqlite3.connect(upload)
    try:
        conn.execute('DROP INDEX ix_events_tournament_type_status')
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RestoreValidationError, match='required-table structure'):
        validate_sqlite_restore_file(str(upload), str(current), tournament_id=1)


def test_validate_sqlite_restore_file_rejects_missing_application_table(
    workspace_tmpdir,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head')
    conn = sqlite3.connect(current)
    try:
        conn.execute(
            'CREATE TABLE score_submission_receipts ('
            'request_id VARCHAR(36) PRIMARY KEY, '
            'payload_sha256 VARCHAR(64) NOT NULL)'
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RestoreValidationError, match='application tables'):
        validate_sqlite_restore_file(str(upload), str(current), tournament_id=1)


def test_validate_sqlite_restore_file_rejects_noncore_schema_drift(
    workspace_tmpdir,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head')
    conn = sqlite3.connect(upload)
    try:
        conn.execute('CREATE INDEX ix_audit_logs_action ON audit_logs(action)')
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RestoreValidationError, match='application-table structure'):
        validate_sqlite_restore_file(str(upload), str(current), tournament_id=1)


def test_validate_sqlite_restore_file_rejects_full_tournament_set_mismatch(
    workspace_tmpdir,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(
        current,
        revision='head',
        tournaments=[
            (1, 'Missoula Pro-Am', 2027),
            (2, 'Missoula Pro-Am', 2026),
        ],
    )
    _make_sqlite_db(
        upload,
        revision='head',
        tournaments=[
            (1, 'Missoula Pro-Am', 2027),
            (2, 'Unrelated Tournament', 2026),
        ],
    )

    with pytest.raises(RestoreValidationError, match='database identity'):
        validate_sqlite_restore_file(str(upload), str(current), tournament_id=1)


def test_sqlite_db_path_from_uri_rejects_non_sqlite(workspace_tmpdir):
    with pytest.raises(RuntimeError, match='only available for SQLite'):
        sqlite_db_path_from_uri('postgresql://example/db', str(workspace_tmpdir))


def test_prepare_sqlite_restore_runs_scan_and_returns_plan(workspace_tmpdir, monkeypatch):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head')
    calls = []

    def _scan(path, *, enabled, command_template):
        calls.append((path, enabled, command_template))

    monkeypatch.setattr('services.restore_workflow.malware_scan', _scan)

    result = prepare_sqlite_restore(
        upload_path=str(upload),
        db_uri='sqlite:///current.db',
        instance_path=str(workspace_tmpdir),
        tournament_id=1,
        malware_scan_enabled=True,
        malware_scan_command='scan {path}',
    )

    assert result['target_path'] == str(current)
    assert calls == [(str(upload), True, 'scan {path}')]


def test_stage_restore_writes_fsynced_package_manifest(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head', marker='replacement')

    result = stage_sqlite_restore(
        upload_path=str(upload),
        db_uri='sqlite:///current.db',
        instance_path=str(workspace_tmpdir),
        tournament_id=1,
        actor='admin:7',
        source_filename='race-day.db',
    )

    package_dir = Path(result['package_dir'])
    manifest = json.loads((package_dir / 'manifest.json').read_text(encoding='utf-8'))
    staged_db = package_dir / 'restore.db'
    assert package_dir.parent == workspace_tmpdir / '.restore-staging'
    assert manifest['status'] == 'staged'
    assert manifest['tournament_id'] == 1
    assert manifest['source_sha256'] == sha256_file(staged_db)
    assert manifest['target_sha256_at_stage'] == sha256_file(current)
    assert manifest['actor'] == 'admin:7'
    assert manifest['source_filename'] == 'race-day.db'
    assert len(manifest['required_schema_sha256']) == 64
    assert len(manifest['database_identity_sha256']) == 64
    assert manifest['database_identity_tournament_count'] == 1


def test_stage_restore_restricts_package_database_and_journal_modes(
    workspace_tmpdir,
    monkeypatch,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head', marker='replacement')
    restricted = []

    monkeypatch.setattr(
        restore_workflow,
        '_chmod_if_supported',
        lambda path, mode: restricted.append((Path(path), mode)),
    )

    result = stage_sqlite_restore(
        upload_path=str(upload),
        db_uri='sqlite:///current.db',
        instance_path=str(workspace_tmpdir),
        tournament_id=1,
        actor='admin:7',
        source_filename='race-day.db',
    )

    package_dir = Path(result['package_dir'])
    assert (package_dir.parent, 0o700) in restricted
    assert (package_dir, 0o700) in restricted
    assert (package_dir / 'restore.db', 0o600) in restricted
    assert (package_dir / 'restore-journal.jsonl', 0o600) in restricted
    assert (package_dir / 'manifest.json', 0o600) in restricted


def test_failed_stage_removes_incomplete_plaintext_package(
    workspace_tmpdir,
    monkeypatch,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head', marker='replacement')
    actual_copy = restore_workflow.shutil.copyfile

    def copy_then_fail(source, destination):
        actual_copy(source, destination)
        raise RuntimeError('injected staging failure')

    monkeypatch.setattr(restore_workflow.shutil, 'copyfile', copy_then_fail)

    with pytest.raises(RuntimeError, match='injected staging failure'):
        stage_sqlite_restore(
            upload_path=str(upload),
            db_uri='sqlite:///current.db',
            instance_path=str(workspace_tmpdir),
            tournament_id=1,
            actor='admin:7',
            source_filename='race-day.db',
        )

    staging_root = workspace_tmpdir / '.restore-staging'
    assert not list(staging_root.glob('*/restore.db'))
    assert not [path for path in staging_root.iterdir() if path.is_dir()]


def test_windows_restore_acl_is_explicit_and_verified(tmp_path, monkeypatch):
    package_dir = tmp_path / 'restore-package'
    package_dir.mkdir()
    calls = []

    def successful_acl(command, **kwargs):
        calls.append((command, kwargs))
        return type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()

    monkeypatch.setattr(restore_workflow.subprocess, 'run', successful_acl)

    restore_workflow._restrict_windows_acl(package_dir)

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert 'SetAccessRuleProtection($true, $false)' in command[-1]
    assert 'SetAccessControl($acl)' in command[-1]
    assert 'GetAccessRules(' in command[-1]
    assert kwargs['env']['PROAM_RESTORE_ACL_TARGET'] == str(package_dir)
    assert kwargs['check'] is False
    assert kwargs['timeout'] == 30


@pytest.mark.parametrize(
    'fail_at',
    [
        'before_safety_snapshot',
        'after_safety_snapshot',
        'before_replace',
        'after_replace',
        'before_reopen',
        'after_reopen',
        'before_post_validation',
        'after_post_validation',
    ],
)
def test_offline_restore_failure_leaves_old_valid_database(
    workspace_tmpdir, fail_at,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head', marker='original')
    _make_sqlite_db(upload, revision='head', marker='replacement')
    staged = stage_sqlite_restore(
        upload_path=str(upload),
        db_uri='sqlite:///current.db',
        instance_path=str(workspace_tmpdir),
        tournament_id=1,
        actor='admin:7',
        source_filename='race-day.db',
    )

    with pytest.raises(RuntimeError, match='Injected restore failure'):
        apply_staged_sqlite_restore(
            staged['package_dir'],
            actor='maintenance:test',
            fail_at=fail_at,
        )

    assert _marker(current) == 'original'
    validation = validate_sqlite_restore_file(
        str(current), str(current), tournament_id=1,
    )
    assert validation['integrity_check'] == 'ok'
    journal = (Path(staged['package_dir']) / 'restore-journal.jsonl').read_text(
        encoding='utf-8'
    )
    assert 'rollback' in journal or fail_at.startswith('before_')


@pytest.mark.parametrize(
    'fail_at',
    [
        'after_quarantine_wal',
        'after_quarantine_shm',
        'after_sidecar_quarantine',
    ],
)
def test_sidecar_quarantine_failure_rolls_back_without_reattaching_stale_pages(
    workspace_tmpdir,
    monkeypatch,
    fail_at,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head', marker='original')
    _make_sqlite_db(upload, revision='head', marker='replacement')
    staged = stage_sqlite_restore(
        upload_path=str(upload),
        db_uri='sqlite:///current.db',
        instance_path=str(workspace_tmpdir),
        tournament_id=1,
        actor='admin:7',
        source_filename='race-day.db',
    )
    original_snapshot = restore_workflow.create_sqlite_snapshot

    def _snapshot_then_create_stale_sidecars(source_path, destination_path):
        result = original_snapshot(source_path, destination_path)
        Path(f'{source_path}-wal').write_bytes(b'stale-wal-pages')
        Path(f'{source_path}-shm').write_bytes(b'stale-shared-memory')
        return result

    monkeypatch.setattr(
        restore_workflow,
        'create_sqlite_snapshot',
        _snapshot_then_create_stale_sidecars,
    )

    with pytest.raises(RuntimeError, match='Injected restore failure'):
        apply_staged_sqlite_restore(
            staged['package_dir'],
            actor='maintenance:test',
            fail_at=fail_at,
        )

    assert _marker(current) == 'original'
    assert not Path(f'{current}-wal').exists()
    assert not Path(f'{current}-shm').exists()
    quarantine_dir = Path(staged['package_dir']) / 'sidecar-quarantine'
    assert list(quarantine_dir.glob('*-wal'))
    assert list(quarantine_dir.glob('*-shm'))
    journal = (Path(staged['package_dir']) / 'restore-journal.jsonl').read_text(
        encoding='utf-8'
    )
    assert 'sidecar_quarantined' in journal
    assert 'rollback_completed' in journal


def test_next_boot_recovers_after_abrupt_exit_during_sidecar_quarantine(
    workspace_tmpdir,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head', marker='original')
    _make_sqlite_db(upload, revision='head', marker='replacement')
    staged = stage_sqlite_restore(
        upload_path=str(upload),
        db_uri='sqlite:///current.db',
        instance_path=str(workspace_tmpdir),
        tournament_id=1,
        actor='admin:7',
        source_filename='race-day.db',
    )
    crash_script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path

        import services.restore_workflow as restore_workflow

        original_snapshot = restore_workflow.create_sqlite_snapshot
        original_inject = restore_workflow._inject_failure

        def snapshot_then_create_stale_sidecars(source_path, destination_path):
            result = original_snapshot(source_path, destination_path)
            Path(f'{source_path}-wal').write_bytes(b'stale-wal-pages')
            Path(f'{source_path}-shm').write_bytes(b'stale-shared-memory')
            return result

        def terminate_after_quarantine(fail_at, checkpoint):
            if checkpoint == 'after_sidecar_quarantine':
                os._exit(87)
            original_inject(fail_at, checkpoint)

        restore_workflow.create_sqlite_snapshot = snapshot_then_create_stale_sidecars
        restore_workflow._inject_failure = terminate_after_quarantine
        restore_workflow.apply_staged_sqlite_restore(
            sys.argv[1], actor='maintenance:crash-test', fail_at='crash'
        )
        """
    )

    crashed = subprocess.run(
        [sys.executable, '-c', crash_script, staged['package_dir']],
        cwd=Path.cwd(),
        env=os.environ.copy(),
        check=False,
    )

    assert crashed.returncode == 87
    manifest_path = Path(staged['package_dir']) / 'manifest.json'
    interrupted_manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert interrupted_manifest['status'] == 'applying'
    assert interrupted_manifest['phase'] == 'replace_sidecars_quarantined'
    assert not Path(f'{current}-wal').exists()
    assert not Path(f'{current}-shm').exists()

    recovered = apply_staged_sqlite_restore(
        staged['package_dir'], actor='maintenance:next-boot',
    )

    assert recovered['ok'] is True
    assert _marker(current) == 'replacement'
    assert not Path(f'{current}-wal').exists()
    assert not Path(f'{current}-shm').exists()
    journal = (Path(staged['package_dir']) / 'restore-journal.jsonl').read_text(
        encoding='utf-8'
    )
    assert 'interrupted_apply' in journal
    assert 'rollback_completed' in journal


def test_next_boot_reconciles_sidecar_moved_before_manifest_update(
    workspace_tmpdir,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head', marker='original')
    _make_sqlite_db(upload, revision='head', marker='replacement')
    staged = stage_sqlite_restore(
        upload_path=str(upload),
        db_uri='sqlite:///current.db',
        instance_path=str(workspace_tmpdir),
        tournament_id=1,
        actor='admin:7',
        source_filename='race-day.db',
    )
    crash_script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path

        import services.restore_workflow as restore_workflow

        original_snapshot = restore_workflow.create_sqlite_snapshot
        original_inject = restore_workflow._inject_failure

        def snapshot_then_create_stale_sidecars(source_path, destination_path):
            result = original_snapshot(source_path, destination_path)
            Path(f'{source_path}-wal').write_bytes(b'stale-wal-pages')
            Path(f'{source_path}-shm').write_bytes(b'stale-shared-memory')
            return result

        def terminate_after_move(fail_at, checkpoint):
            if checkpoint == 'after_sidecar_move_before_manifest_wal':
                os._exit(88)
            original_inject(fail_at, checkpoint)

        restore_workflow.create_sqlite_snapshot = snapshot_then_create_stale_sidecars
        restore_workflow._inject_failure = terminate_after_move
        restore_workflow.apply_staged_sqlite_restore(
            sys.argv[1], actor='maintenance:move-crash-test', fail_at='crash'
        )
        """
    )

    crashed = subprocess.run(
        [sys.executable, '-c', crash_script, staged['package_dir']],
        cwd=Path.cwd(),
        env=os.environ.copy(),
        check=False,
    )

    assert crashed.returncode == 88
    package_dir = Path(staged['package_dir'])
    interrupted = json.loads(
        (package_dir / 'manifest.json').read_text(encoding='utf-8')
    )
    record = interrupted['sidecar_quarantines'][-1]
    wal_entry = next(entry for entry in record['entries'] if entry['suffix'] == '-wal')
    assert wal_entry['status'] == 'move_pending'
    assert len(wal_entry['source_sha256']) == 64
    assert not Path(f'{current}-wal').exists()
    assert (package_dir / 'sidecar-quarantine' / wal_entry['quarantine_name']).is_file()

    recovered = apply_staged_sqlite_restore(
        staged['package_dir'], actor='maintenance:next-boot',
    )

    assert recovered['ok'] is True
    completed = json.loads(
        (package_dir / 'manifest.json').read_text(encoding='utf-8')
    )
    original_record = next(
        item for item in completed['sidecar_quarantines']
        if item['attempt_id'] == record['attempt_id']
    )
    assert original_record['status'] == 'completed'
    assert all(
        entry['status'] in {'absent', 'quarantined'}
        for entry in original_record['entries']
    )
    journal = (package_dir / 'restore-journal.jsonl').read_text(encoding='utf-8')
    assert 'sidecar_quarantine_reconciled' in journal


def test_offline_restore_applies_validated_database_and_journals_hashes(
    workspace_tmpdir,
):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head', marker='original')
    _make_sqlite_db(upload, revision='head', marker='replacement')
    staged = stage_sqlite_restore(
        upload_path=str(upload),
        db_uri='sqlite:///current.db',
        instance_path=str(workspace_tmpdir),
        tournament_id=1,
        actor='admin:7',
        source_filename='race-day.db',
    )

    result = apply_staged_sqlite_restore(
        staged['package_dir'], actor='maintenance:test',
    )

    assert result['ok'] is True
    assert result['status'] == 'completed'
    assert _marker(current) == 'replacement'
    entries = [
        json.loads(line)
        for line in (Path(staged['package_dir']) / 'restore-journal.jsonl')
        .read_text(encoding='utf-8')
        .splitlines()
    ]
    assert entries[-1]['event'] == 'restore_completed'
    assert entries[-1]['source_sha256'] == sha256_file(current)
    assert entries[-1]['safety_sha256']
    assert entries[-1]['audit_chain_head']
    assert all('details_json' not in entry for entry in entries)


def test_offline_restore_refuses_while_web_process_fence_is_held(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head', marker='original')
    _make_sqlite_db(upload, revision='head', marker='replacement')
    staged = stage_sqlite_restore(
        upload_path=str(upload),
        db_uri='sqlite:///current.db',
        instance_path=str(workspace_tmpdir),
        tournament_id=1,
        actor='admin:7',
        source_filename='race-day.db',
    )

    with database_activity_fence(str(current), exclusive=False, timeout=0):
        with pytest.raises(RuntimeError, match='still running'):
            apply_staged_sqlite_restore(
                staged['package_dir'], actor='maintenance:test',
            )

    assert _marker(current) == 'original'


def test_same_process_apps_share_one_sqlite_lifetime_fence(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    _make_sqlite_db(current, revision='head')

    class TestApp:
        def __init__(self):
            self.config = {'SQLALCHEMY_DATABASE_URI': f'sqlite:///{current}'}
            self.instance_path = str(workspace_tmpdir)
            self.extensions = {}

    first = TestApp()
    second = TestApp()
    configure_sqlite_process_fence(first)
    configure_sqlite_process_fence(second)

    assert first.extensions['sqlite_process_fence'] is second.extensions[
        'sqlite_process_fence'
    ]
    with pytest.raises(MaintenanceFenceBusy):
        with database_activity_fence(str(current), exclusive=True, timeout=0):
            pass

    first.extensions['sqlite_process_fence_finalizer']()
    with pytest.raises(MaintenanceFenceBusy):
        with database_activity_fence(str(current), exclusive=True, timeout=0):
            pass

    second.extensions['sqlite_process_fence_finalizer']()
    with database_activity_fence(str(current), exclusive=True, timeout=0):
        pass
