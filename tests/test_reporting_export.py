import hashlib
import os
import time


def test_build_results_export_returns_file_metadata(app, monkeypatch):
    from database import db
    from models import Tournament
    from services import reporting_export

    with app.app_context():
        tournament = Tournament(name='Export Smoke', year=2026, status='setup')
        db.session.add(tournament)
        db.session.commit()

        out = os.path.join(app.instance_path, 'reporting-export-results.xlsx')

        def _fake_export(export_tournament, path):
            assert export_tournament.id == tournament.id
            with open(path, 'wb') as fh:
                fh.write(b'xlsx')

        monkeypatch.setattr(reporting_export, '_reserve_export_path', lambda *_a, **_k: out)
        monkeypatch.setattr(reporting_export, 'export_results_to_excel', _fake_export)

        result = reporting_export.build_results_export(tournament)

    assert result == {
        'path': out,
        'download_name': 'Export_Smoke_2026_results.xlsx',
        'format': 'xlsx',
        'kind': 'all_results',
        'sha256': hashlib.sha256(b'xlsx').hexdigest(),
    }
    assert os.path.exists(out)
    os.remove(out)


def test_build_chopping_json_payload_uses_shared_rows(app, monkeypatch):
    from database import db
    from models import Tournament
    from services import reporting_export

    with app.app_context():
        tournament = Tournament(name='Chop Tooling', year=2026, status='setup')
        db.session.add(tournament)
        db.session.commit()
        tournament_id = tournament.id

        monkeypatch.setattr(
            reporting_export,
            'build_chopping_rows',
            lambda export_tournament: [{'tournament_id': export_tournament.id}],
        )

        payload = reporting_export.build_chopping_json_payload(tournament)

    assert payload == {
        'tournament': {'id': tournament_id, 'name': 'Chop Tooling', 'year': 2026},
        'rows': [{'tournament_id': tournament_id}],
    }


def test_submit_results_export_job_is_tournament_bound(monkeypatch):
    from services import reporting_export

    captured = {}

    def _fake_submit(label, fn, *args, metadata=None, **kwargs):
        captured.update({
            'label': label,
            'fn': fn,
            'args': args,
            'metadata': metadata,
            'kwargs': kwargs,
        })
        return 'job-123'

    monkeypatch.setattr(reporting_export, 'submit_job', _fake_submit)

    job_id = reporting_export.submit_results_export_job(42)

    assert job_id == 'job-123'
    assert captured['label'] == 'export_results_42'
    assert captured['fn'] is reporting_export.build_results_export_for_job
    assert captured['args'] == (42,)
    assert captured['metadata'] == {'tournament_id': 42, 'kind': 'export_results'}
    assert captured['kwargs'] == {}


def test_resolve_completed_export_path_rejects_cross_tournament_jobs(tmp_path):
    from services.reporting_export import resolve_completed_export_path

    artifact = tmp_path / 'out.xlsx'
    artifact.write_bytes(b'xlsx')

    def _get_job(_job_id):
        return {
            'metadata': {'tournament_id': 7, 'kind': 'export_results'},
            'status': 'completed',
            'result': {
                'path': str(artifact),
                'sha256': hashlib.sha256(b'xlsx').hexdigest(),
            },
        }

    assert resolve_completed_export_path(8, 'job-1', _get_job) is None
    resolved = resolve_completed_export_path(7, 'job-1', _get_job)
    assert resolved['status'] == 'completed'
    assert resolved['result']['path'] == str(artifact)


def test_resolve_completed_export_path_expires_missing_artifact_after_restart(tmp_path):
    from services.reporting_export import resolve_completed_export_path

    missing = tmp_path / 'missing.xlsx'
    job = {
        'metadata': {'tournament_id': 7, 'kind': 'export_results'},
        'status': 'completed',
        'result': {
            'path': str(missing),
            'sha256': hashlib.sha256(b'xlsx').hexdigest(),
        },
    }

    resolved = resolve_completed_export_path(7, 'job-1', lambda _job_id: job)

    assert resolved['status'] == 'expired'
    assert 'no longer exists' in resolved['error']


def test_export_artifact_cleanup_expires_old_files_and_bounds_count(tmp_path):
    from services.reporting_export import prune_export_artifacts

    now = time.time()
    old = tmp_path / 'proam_export_1_old.xlsx'
    old.write_bytes(b'old')
    os.utime(old, (now - 7200, now - 7200))
    fresh = []
    for index in range(3):
        path = tmp_path / f'proam_export_1_fresh_{index}.xlsx'
        path.write_bytes(str(index).encode())
        os.utime(path, (now + index, now + index))
        fresh.append(path)
    unrelated = tmp_path / 'another-project.xlsx'
    unrelated.write_bytes(b'keep')

    removed = prune_export_artifacts(
        directory=str(tmp_path),
        now=now,
        max_age_seconds=3600,
        max_files=2,
    )

    assert removed == 2
    assert not old.exists()
    assert not fresh[0].exists()
    assert fresh[1].exists()
    assert fresh[2].exists()
    assert unrelated.exists()


def test_resolve_completed_export_path_expires_tampered_artifact(tmp_path):
    from services.reporting_export import resolve_completed_export_path

    artifact = tmp_path / 'tampered.xlsx'
    artifact.write_bytes(b'original')
    job = {
        'metadata': {'tournament_id': 7, 'kind': 'video_judge_sheets'},
        'status': 'completed',
        'result': {
            'path': str(artifact),
            'sha256': hashlib.sha256(b'original').hexdigest(),
        },
    }
    artifact.write_bytes(b'changed')

    resolved = resolve_completed_export_path(7, 'job-1', lambda _job_id: job)

    assert resolved['status'] == 'expired'
    assert 'checksum' in resolved['error'].lower()


def test_resolve_completed_export_path_expires_legacy_result_without_checksum(tmp_path):
    from services.reporting_export import resolve_completed_export_path

    artifact = tmp_path / 'legacy.xlsx'
    artifact.write_bytes(b'xlsx')
    job = {
        'metadata': {'tournament_id': 7, 'kind': 'export_results'},
        'status': 'completed',
        'result': str(artifact),
    }

    resolved = resolve_completed_export_path(7, 'job-1', lambda _job_id: job)

    assert resolved['status'] == 'expired'
    assert 'checksum metadata' in resolved['error'].lower()


def test_export_job_route_downloads_checksum_verified_artifact(
    auth_client, db_session, monkeypatch, tmp_path,
):
    from models import Tournament
    from routes import reporting

    artifact = tmp_path / 'verified.xlsx'
    artifact.write_bytes(b'verified-xlsx')
    tournament = Tournament(name='Verified Export', year=2027, status='setup')
    db_session.add(tournament)
    db_session.flush()
    tournament_id = tournament.id
    job = {
        'metadata': {'tournament_id': tournament_id, 'kind': 'export_results'},
        'status': 'completed',
        'result': {
            'path': str(artifact),
            'sha256': hashlib.sha256(b'verified-xlsx').hexdigest(),
        },
    }
    monkeypatch.setattr(reporting, 'get_job', lambda _job_id: job)

    response = auth_client.get(f'/reporting/{tournament_id}/jobs/export-job')

    assert response.status_code == 200
    assert response.data == b'verified-xlsx'
    assert 'results.xlsx' in response.headers['Content-Disposition']
    assert artifact.exists()

    retry = auth_client.get(f'/reporting/{tournament_id}/jobs/export-job')
    assert retry.status_code == 200
    assert retry.data == b'verified-xlsx'


def test_export_job_route_rechecks_same_handle_after_status_resolution(
    auth_client, db_session, monkeypatch, tmp_path,
):
    from models import Tournament
    from routes import reporting

    artifact = tmp_path / 'replaced-after-status.xlsx'
    artifact.write_bytes(b'original')
    tournament = Tournament(name='Raced Export', year=2027, status='setup')
    db_session.add(tournament)
    db_session.flush()
    tournament_id = tournament.id
    job = {
        'metadata': {'tournament_id': tournament_id, 'kind': 'export_results'},
        'status': 'completed',
        'result': {
            'path': str(artifact),
            'sha256': hashlib.sha256(b'original').hexdigest(),
        },
    }
    real_resolve = reporting.resolve_completed_export_path

    def resolve_then_replace(*args, **kwargs):
        resolved = real_resolve(*args, **kwargs)
        artifact.write_bytes(b'replacement')
        return resolved

    monkeypatch.setattr(reporting, 'get_job', lambda _job_id: job)
    monkeypatch.setattr(
        reporting,
        'resolve_completed_export_path',
        resolve_then_replace,
    )

    response = auth_client.get(
        f'/reporting/{tournament_id}/jobs/export-job',
        follow_redirects=False,
    )

    assert response.status_code == 302
    with auth_client.session_transaction() as session:
        assert any(
            category == 'error' and 'checksum verification failed' in message
            for category, message in session.get('_flashes', [])
        )


def test_export_job_route_redirects_tampered_artifact(
    auth_client, db_session, monkeypatch, tmp_path,
):
    from models import Tournament
    from routes import reporting

    artifact = tmp_path / 'tampered-route.xlsx'
    artifact.write_bytes(b'changed')
    tournament = Tournament(name='Tampered Export', year=2027, status='setup')
    db_session.add(tournament)
    db_session.flush()
    tournament_id = tournament.id
    job = {
        'metadata': {'tournament_id': tournament_id, 'kind': 'export_results'},
        'status': 'completed',
        'result': {
            'path': str(artifact),
            'sha256': hashlib.sha256(b'original').hexdigest(),
        },
    }
    monkeypatch.setattr(reporting, 'get_job', lambda _job_id: job)

    response = auth_client.get(
        f'/reporting/{tournament_id}/jobs/export-job',
        follow_redirects=False,
    )

    assert response.status_code == 302
    with auth_client.session_transaction() as session:
        assert any(
            category == 'error' and 'checksum verification failed' in message
            for category, message in session.get('_flashes', [])
        )
