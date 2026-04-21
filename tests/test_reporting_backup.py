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
