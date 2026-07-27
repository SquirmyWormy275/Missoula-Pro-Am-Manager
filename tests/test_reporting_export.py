import os


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


def test_resolve_completed_export_path_rejects_cross_tournament_jobs():
    from services.reporting_export import resolve_completed_export_path

    def _get_job(_job_id):
        return {'metadata': {'tournament_id': 7}, 'status': 'completed', 'result': 'out.xlsx'}

    assert resolve_completed_export_path(8, 'job-1', _get_job) is None
    assert resolve_completed_export_path(7, 'job-1', _get_job)['result'] == 'out.xlsx'
