import io
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace


def test_sqlite_online_snapshot_is_consistent_during_active_writes(app):
    from services.backup import create_sqlite_snapshot

    db_path = os.path.join(app.instance_path, f'active-backup-{uuid.uuid4().hex}.db')
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE summary (row_count INTEGER NOT NULL)')
    conn.execute('CREATE TABLE ledger (id INTEGER PRIMARY KEY)')
    conn.execute('INSERT INTO summary VALUES (0)')
    conn.commit()
    conn.close()
    stop = threading.Event()

    def _writer():
        writer = sqlite3.connect(db_path, timeout=5)
        try:
            while not stop.is_set():
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('INSERT INTO ledger DEFAULT VALUES')
                writer.execute('UPDATE summary SET row_count = row_count + 1')
                writer.commit()
        finally:
            writer.close()

    thread = threading.Thread(target=_writer)
    thread.start()
    snapshot = None
    try:
        deadline = time.time() + 2
        while time.time() < deadline:
            observer = sqlite3.connect(db_path)
            count = observer.execute('SELECT COUNT(*) FROM ledger').fetchone()[0]
            observer.close()
            if count:
                break
            time.sleep(0.01)
        snapshot = create_sqlite_snapshot(db_path)
    finally:
        stop.set()
        thread.join(timeout=2)

    try:
        restored = sqlite3.connect(snapshot['path'])
        try:
            summary_count = restored.execute('SELECT row_count FROM summary').fetchone()[0]
            ledger_count = restored.execute('SELECT COUNT(*) FROM ledger').fetchone()[0]
            assert summary_count == ledger_count
            assert restored.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        finally:
            restored.close()
    finally:
        if snapshot:
            os.remove(snapshot['path'])
        for suffix in ('', '-wal', '-shm'):
            try:
                os.remove(f'{db_path}{suffix}')
            except OSError:
                pass


def test_sqlite_backup_download_plan_creates_validated_snapshot(app):
    from services.reporting_backup import sqlite_backup_download_plan

    db_path = os.path.join(app.instance_path, 'backup-plan.db')
    conn = sqlite3.connect(db_path)
    conn.execute('CREATE TABLE values_seen (value INTEGER NOT NULL)')
    conn.execute('INSERT INTO values_seen VALUES (17)')
    conn.commit()
    conn.close()

    try:
        result = sqlite_backup_download_plan('sqlite:///backup-plan.db', app.instance_path)
        assert result['ok'] is True
        assert result['path'] != db_path
        assert result['cleanup'] is True
        assert len(result['sha256']) == 64
        snapshot = sqlite3.connect(result['path'])
        try:
            assert snapshot.execute('SELECT value FROM values_seen').fetchone() == (17,)
            assert snapshot.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        finally:
            snapshot.close()
    finally:
        if 'result' in locals() and result.get('path'):
            try:
                os.remove(result['path'])
            except OSError:
                pass
        os.remove(db_path)


def test_sqlite_backup_download_plan_rejects_non_sqlite(app):
    from services.reporting_backup import sqlite_backup_download_plan

    result = sqlite_backup_download_plan('postgresql://example/db', app.instance_path)

    assert result['ok'] is False
    assert result['reason'] == 'unsupported'
    assert 'SQLite' in result['message']


def test_sqlite_backup_download_plan_rejects_missing_file(app):
    from services.reporting_backup import sqlite_backup_download_plan

    result = sqlite_backup_download_plan('sqlite:///missing-backup-plan.db', app.instance_path)

    assert result == {
        'ok': False,
        'reason': 'missing',
        'message': 'Database file not found.',
    }


def test_local_backup_uses_durable_directory_publish(tmp_path, monkeypatch):
    from services import backup

    source = tmp_path / 'source.db'
    connection = sqlite3.connect(source)
    connection.execute('CREATE TABLE evidence (value INTEGER NOT NULL)')
    connection.execute('INSERT INTO evidence VALUES (17)')
    connection.commit()
    connection.close()
    calls = []
    real_replace = backup._replace_file_durably

    def recording_replace(source_path, destination_path):
        calls.append((source_path, destination_path))
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(backup, '_replace_file_durably', recording_replace)

    result = backup.backup_to_local(
        str(source),
        str(tmp_path / 'backups'),
        tournament_id=7,
    )

    assert result['ok'] is True
    assert len(calls) == 1
    assert calls[0][1] == result['dest']


def test_postgres_local_backup_publishes_only_complete_archive(tmp_path, monkeypatch):
    from services import backup

    def fake_pg_dump(args, **_kwargs):
        output = next(value.split('=', 1)[1] for value in args if value.startswith('--file='))
        Path(output).write_bytes(b'PGDMP\x01synthetic-archive')
        return SimpleNamespace(returncode=0, stderr='')

    monkeypatch.setattr(backup.subprocess, 'run', fake_pg_dump)
    result = backup.backup_pg_to_local(
        'postgresql://proam:proam@localhost/proam',
        str(tmp_path),
        tournament_id=17,
    )

    assert result['ok'] is True
    assert Path(result['dest']).read_bytes() == b'PGDMP\x01synthetic-archive'
    assert result['sha256'] == backup.sha256_file(result['dest'])
    assert not list(tmp_path.glob('.proam_pg_backup_*.tmp'))


def test_failed_postgres_dump_leaves_no_final_or_partial_archive(tmp_path, monkeypatch):
    from services import backup

    def failed_pg_dump(args, **_kwargs):
        output = next(value.split('=', 1)[1] for value in args if value.startswith('--file='))
        Path(output).write_bytes(b'partial')
        return SimpleNamespace(returncode=1, stderr='synthetic failure')

    monkeypatch.setattr(backup.subprocess, 'run', failed_pg_dump)
    result = backup.backup_pg_to_local(
        'postgresql://proam:proam@localhost/proam',
        str(tmp_path),
        tournament_id=17,
    )

    assert result['ok'] is False
    assert list(tmp_path.iterdir()) == []


def test_postgres_s3_failure_falls_back_to_local_backup(tmp_path, monkeypatch):
    from services import backup

    local_calls = []
    monkeypatch.setenv('LOCAL_BACKUP_DIR', str(tmp_path))
    monkeypatch.setattr(backup, 'is_s3_configured', lambda: True)
    monkeypatch.setattr(
        backup,
        'backup_pg_to_s3',
        lambda *_args: {'ok': False, 'error': 'synthetic S3 outage'},
    )

    def local_backup(db_uri, dest_dir, tournament_id):
        local_calls.append((db_uri, dest_dir, tournament_id))
        return {'ok': True, 'dest': str(tmp_path / 'fallback.dump')}

    monkeypatch.setattr(backup, 'backup_pg_to_local', local_backup)
    result = backup.backup_database(
        'postgresql://proam:proam@localhost/proam',
        tournament_id=17,
    )

    assert result['ok'] is True
    assert result['fallback_from'] == 's3'
    assert result['primary_error'] == 'synthetic S3 outage'
    assert local_calls == [
        (
            'postgresql://proam:proam@localhost/proam',
            str(tmp_path),
            17,
        )
    ]


def test_sqlite_s3_failure_falls_back_to_local_backup(tmp_path, monkeypatch):
    from services import backup

    source = tmp_path / 'source.db'
    source.write_bytes(b'SQLite format 3\x00synthetic')
    local_calls = []
    monkeypatch.setenv('LOCAL_BACKUP_DIR', str(tmp_path / 'backups'))
    monkeypatch.setattr(backup, 'is_s3_configured', lambda: True)
    monkeypatch.setattr(
        backup,
        'backup_to_s3',
        lambda *_args: {'ok': False, 'error': 'synthetic S3 outage'},
    )

    def local_backup(db_path, dest_dir, tournament_id):
        local_calls.append((db_path, dest_dir, tournament_id))
        return {'ok': True, 'dest': str(tmp_path / 'fallback.db')}

    monkeypatch.setattr(backup, 'backup_to_local', local_backup)
    result = backup.backup_database(
        f'sqlite:///{source}',
        tournament_id=9,
    )

    assert result['ok'] is True
    assert result['fallback_from'] == 's3'
    assert result['primary_error'] == 'synthetic S3 outage'
    assert local_calls == [
        (str(source), str(tmp_path / 'backups'), 9)
    ]


def test_same_second_local_backups_use_distinct_artifact_names(tmp_path, monkeypatch):
    from services import backup

    source = tmp_path / 'source.db'
    connection = sqlite3.connect(source)
    connection.execute('CREATE TABLE evidence (value INTEGER NOT NULL)')
    connection.commit()
    connection.close()
    destination = tmp_path / 'backups'
    monkeypatch.setattr(backup, '_timestamp', lambda: '20270818_120000')

    first = backup.backup_to_local(str(source), str(destination), tournament_id=9)
    second = backup.backup_to_local(str(source), str(destination), tournament_id=9)

    assert first['ok'] is True
    assert second['ok'] is True
    assert first['dest'] != second['dest']
    assert Path(first['dest']).is_file()
    assert Path(second['dest']).is_file()


def test_submit_database_backup_job_is_tournament_bound(monkeypatch):
    from services import reporting_backup

    captured = {}

    def _fake_submit(label, fn, *args, metadata=None, **kwargs):
        captured.update({
            'label': label,
            'fn': fn,
            'args': args,
            'metadata': metadata,
            'kwargs': kwargs,
        })
        return 'backup-job-123'

    monkeypatch.setattr(reporting_backup, 'submit_job', _fake_submit)

    job_id = reporting_backup.submit_database_backup_job('sqlite:///proam.db', 42, '/instance')

    assert job_id == 'backup-job-123'
    assert captured['label'] == 'backup:t42'
    assert captured['fn'] is reporting_backup.run_database_backup
    assert captured['args'] == ('sqlite:///proam.db', 42, '/instance')
    assert captured['metadata'] == {'tournament_id': 42, 'kind': 'backup'}
    assert captured['kwargs'] == {}


def test_restore_route_validates_and_stages_without_replacing_database(
    app, db_session, auth_client, admin_user, monkeypatch,
):
    from models.audit_log import AuditLog
    from models.tournament import Tournament
    from tests.conftest import make_tournament

    tournament = make_tournament(db_session)
    db_session.commit()
    captured = {}
    monkeypatch.setitem(
        app.config,
        'SQLALCHEMY_DATABASE_URI',
        'sqlite:///synthetic-stage.db',
    )

    def _stage(**kwargs):
        captured.update(kwargs)
        assert os.path.exists(kwargs['upload_path'])
        return {
            'ok': True,
            'stage_id': 'stage-123',
            'source_sha256': 'a' * 64,
        }

    monkeypatch.setattr('routes.reporting.stage_sqlite_restore', _stage)

    response = auth_client.post(
        f'/reporting/{tournament.id}/restore',
        data={
            'backup_file': (
                io.BytesIO(b'SQLite format 3\x00synthetic'),
                'recovery.db',
            )
        },
        content_type='multipart/form-data',
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert captured['tournament_id'] == tournament.id
    assert captured['source_filename'] == 'recovery.db'
    assert captured['actor'].startswith('user:')
    assert db_session.get(Tournament, tournament.id).name == tournament.name
    assert AuditLog.query.filter_by(
        action='database_restore_staged', entity_id=tournament.id,
    ).count() == 1
    assert AuditLog.query.filter_by(
        action='database_restored', entity_id=tournament.id,
    ).count() == 0
    AuditLog.query.filter_by(entity_id=tournament.id).delete()
    db_session.delete(tournament)
    db_session.delete(admin_user)
    db_session.commit()


def test_restore_refuses_on_non_sqlite_engines(app, db_session, auth_client, monkeypatch):
    """Pin the production gap: restore is a no-op on PostgreSQL.

    Railway production runs PostgreSQL. `restore_database` in
    routes/reporting.py checks the URI prefix and, on anything that is not
    sqlite:///, flashes a warning and redirects without reading the upload.
    The backup half is the same story one layer down
    (`sqlite_backup_download_plan` returns reason='unsupported').

    So the disaster-recovery path advertised in the admin UI does nothing in
    the environment that actually holds the real data, and the failure is
    silent: no exception, no 'database_restore_failed' audit row, just a
    warning flash on a redirect.

    This test does not assert that the behaviour is correct. It asserts that
    it is what the code does, so that fixing it is a visible, deliberate
    change rather than an accident. Filed as a production finding in
    PROAM_2026_C44; not fixed here because backup/restore against a managed
    PostgreSQL instance is a design decision, not a patch.
    """
    import io

    from models.audit_log import AuditLog
    from tests.conftest import make_tournament

    tournament = make_tournament(db_session)
    db_session.commit()

    monkeypatch.setitem(
        app.config, 'SQLALCHEMY_DATABASE_URI', 'postgresql://proam@localhost/whatever')

    response = auth_client.post(
        f'/reporting/{tournament.id}/restore',
        data={'backup_file': (io.BytesIO(b'SQLite format 3\x00payload'), 'x.db')},
        content_type='multipart/form-data',
        follow_redirects=False,
    )

    assert response.status_code == 302
    failures = (
        AuditLog.query
        .filter_by(action='database_restore_failed', entity_id=tournament.id)
        .count()
    )
    assert failures == 0, (
        'restore now reaches its failure path on PostgreSQL. If that is '
        'intentional, the gap filed in PROAM_2026_C44 is closed and this '
        'guard should be replaced with a real restore test.'
    )
