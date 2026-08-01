import os


def test_sqlite_backup_download_plan_accepts_existing_relative_db(app):
    from services.reporting_backup import sqlite_backup_download_plan

    db_path = os.path.join(app.instance_path, 'backup-plan.db')
    with open(db_path, 'wb') as fh:
        fh.write(b'SQLite format 3\x00')

    try:
        result = sqlite_backup_download_plan('sqlite:///backup-plan.db', app.instance_path)
    finally:
        os.remove(db_path)

    assert result == {'ok': True, 'path': db_path}


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
