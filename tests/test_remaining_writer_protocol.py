"""Regression coverage for the final tournament-writer lock boundaries."""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from database import db
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_pro_competitor,
    make_team,
    make_tournament,
)


def test_scoring_aggregate_writers_take_tournament_lock(
    app, db_session, auth_client, monkeypatch,
):
    import routes.scoring as scoring_routes

    tournament = make_tournament(db_session, name='Scoring Writer Locks')
    event = make_event(db_session, tournament, 'Axe Throw', scoring_type='score')
    db_session.flush()

    calls = []
    original_lock = scoring_routes.lock_tournament_schedule

    def recording_lock(tournament_or_id):
        calls.append(int(getattr(tournament_or_id, 'id', tournament_or_id)))
        return original_lock(tournament_or_id)

    monkeypatch.setattr(scoring_routes, 'lock_tournament_schedule', recording_lock)

    assert auth_client.post(
        f'/scoring/{tournament.id}/heat/999999/undo',
        follow_redirects=False,
    ).status_code == 302
    assert auth_client.post(
        f'/scoring/{tournament.id}/event/{event.id}/throwoff',
        follow_redirects=False,
    ).status_code == 302
    assert auth_client.post(
        f'/scoring/{tournament.id}/event/{event.id}/import-results',
        data={},
        follow_redirects=False,
    ).status_code == 302
    assert auth_client.post(
        f'/scoring/admin/repair-points/{tournament.id}',
    ).status_code == 200
    assert auth_client.post(
        f'/scoring/{tournament.id}/event/{event.id}/finalize',
        follow_redirects=False,
    ).status_code == 302
    assert auth_client.post(
        f'/scoring/{tournament.id}/event/{event.id}/payouts',
        data={},
        follow_redirects=False,
    ).status_code == 302
    assert auth_client.post(
        f'/scoring/{tournament.id}/pro/payout-manager',
        data={'action': 'unknown'},
        follow_redirects=False,
    ).status_code == 302

    assert calls == [tournament.id] * 7


def test_partnered_axe_writer_takes_tournament_lock(
    app, db_session, monkeypatch,
):
    import routes.partnered_axe as partnered_axe_routes
    from models import User

    tournament = make_tournament(db_session, name='Partnered Axe Writer Lock')
    admin = User(username='partnered_axe_writer_admin', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.flush()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin.id)

    calls = []
    original_lock = partnered_axe_routes.lock_tournament_schedule

    def recording_lock(tournament_or_id):
        calls.append(int(getattr(tournament_or_id, 'id', tournament_or_id)))
        return original_lock(tournament_or_id)

    monkeypatch.setattr(
        partnered_axe_routes,
        'lock_tournament_schedule',
        recording_lock,
    )

    assert client.post(
        f'/tournament/{tournament.id}/partnered-axe/enable',
        follow_redirects=False,
    ).status_code == 302
    assert calls == [tournament.id]


def test_registration_generation_editors_lock_before_competitor_read(
    app, db_session, monkeypatch,
):
    import routes.registration as registration_routes
    from models import CollegeCompetitor, ProCompetitor, User

    tournament = make_tournament(db_session, name='Registration Editor Locks')
    team = make_team(db_session, tournament, code='EDITOR-A')
    college_competitor = make_college_competitor(
        db_session,
        tournament,
        team,
        'College Editor',
    )
    pro_competitor = make_pro_competitor(
        db_session,
        tournament,
        'Pro Editor',
    )
    admin = User(username='registration_editor_admin', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.flush()

    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin.id)

    calls = []
    original_lock = registration_routes.lock_tournament_schedule
    original_get_or_404 = registration_routes.db.get_or_404

    def recording_lock(tournament_or_id):
        calls.append(int(getattr(tournament_or_id, 'id', tournament_or_id)))
        return original_lock(tournament_or_id)

    def assert_child_read_follows_lock(model, object_id, *args, **kwargs):
        if model in {CollegeCompetitor, ProCompetitor}:
            assert calls and calls[-1] == tournament.id
        return original_get_or_404(model, object_id, *args, **kwargs)

    monkeypatch.setattr(
        registration_routes,
        'lock_tournament_schedule',
        recording_lock,
    )
    monkeypatch.setattr(
        registration_routes.db,
        'get_or_404',
        assert_child_read_follows_lock,
    )

    college_base = (
        f'/registration/{tournament.id}/college/competitor/'
        f'{college_competitor.id}'
    )
    for suffix in ('remove-event', 'add-event', 'set-partner'):
        assert client.post(
            f'{college_base}/{suffix}',
            data={},
            follow_redirects=False,
        ).status_code == 302
    assert client.post(
        f'/registration/{tournament.id}/pro/{pro_competitor.id}/update-events',
        data={},
        follow_redirects=False,
    ).status_code == 302

    assert calls == [tournament.id] * 4


def test_generation_input_routes_take_tournament_lock(
    app, db_session, monkeypatch,
):
    import routes.registration as registration_routes
    import routes.scheduling.ability_rankings as ranking_routes
    import routes.scheduling.events as event_routes
    import routes.scheduling.friday_feature as friday_routes
    from models import CollegeCompetitor, Event, Flight, Heat, User

    tournament = make_tournament(db_session, name='Generation Input Locks')
    admin = User(username='generation_input_admin', role='admin')
    admin.set_password('testpass')
    db_session.add(admin)
    db_session.flush()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin.id)

    ranking_calls = []
    event_calls = []
    friday_calls = []
    upload_calls = []
    ranking_lock = ranking_routes.lock_tournament_schedule
    event_lock = event_routes.lock_tournament_schedule
    friday_lock = friday_routes.lock_tournament_schedule
    upload_lock = registration_routes.lock_tournament_schedule

    def record_ranking_lock(tournament_or_id):
        ranking_calls.append(tournament.id)
        return ranking_lock(tournament_or_id)

    def record_event_lock(tournament_or_id):
        event_calls.append(tournament.id)
        return event_lock(tournament_or_id)

    def record_friday_lock(tournament_or_id):
        friday_calls.append(tournament.id)
        return friday_lock(tournament_or_id)

    def record_upload_lock(tournament_or_id):
        upload_calls.append(tournament.id)
        return upload_lock(tournament_or_id)

    monkeypatch.setattr(ranking_routes, 'lock_tournament_schedule', record_ranking_lock)
    monkeypatch.setattr(event_routes, 'lock_tournament_schedule', record_event_lock)
    monkeypatch.setattr(friday_routes, 'lock_tournament_schedule', record_friday_lock)
    monkeypatch.setattr(registration_routes, 'lock_tournament_schedule', record_upload_lock)

    assert client.post(
        f'/scheduling/{tournament.id}/pro/ability-rankings',
        data={},
        follow_redirects=False,
    ).status_code == 302
    assert client.post(
        f'/scheduling/{tournament.id}/events/setup',
        data={'action_scope': 'none'},
        follow_redirects=False,
    ).status_code == 302

    process_import = Mock(return_value={
        'teams': 1,
        'invalid_teams': 0,
        'competitors': 4,
    })
    monkeypatch.setattr(
        registration_routes,
        'validate_excel_upload',
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            error=None,
            safe_name='entry.xlsx',
        ),
    )
    monkeypatch.setattr(registration_routes, 'save_upload', lambda *_args, **_kwargs: 'entry.xlsx')
    monkeypatch.setattr(registration_routes, 'malware_scan', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registration_routes, 'log_action', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('services.excel_io.process_college_entry_form', process_import)

    assert client.post(
        f'/registration/{tournament.id}/college/upload',
        data={'file': (BytesIO(b'test workbook'), 'entry.xlsx')},
        content_type='multipart/form-data',
        follow_redirects=False,
    ).status_code == 302

    college_event = make_event(
        db_session,
        tournament,
        'Standing Block Speed',
        event_type='college',
    )
    pro_event = make_event(
        db_session,
        tournament,
        'Underhand',
        event_type='pro',
    )
    heat = Heat(
        event_id=college_event.id,
        heat_number=7,
        run_number=1,
        status='pending',
    )
    team = make_team(db_session, tournament, code='LOCK-A')
    competitor = make_college_competitor(
        db_session,
        tournament,
        team,
        'Lock Order Competitor',
        events=[college_event.id],
    )
    db_session.add_all([
        heat,
        Flight(
            tournament_id=tournament.id,
            flight_number=1,
            status='in_progress',
        ),
    ])
    tournament.set_schedule_config({
        'friday_pro_event_ids': [pro_event.id],
        'friday_feature_notes': 'published Friday selection',
    })
    db_session.commit()

    assert client.post(
        f'/scheduling/{tournament.id}/college/saturday-priority',
        data={},
        follow_redirects=False,
    ).status_code == 302
    assert client.post(
        f'/scheduling/{tournament.id}/events/setup',
        data={'action_scope': 'pro'},
        follow_redirects=False,
    ).status_code == 302
    assert client.post(
        f'/scheduling/{tournament.id}/friday-night',
        data={'action': 'save', 'event_ids': [], 'notes': 'must not save'},
        follow_redirects=False,
    ).status_code == 302
    db.session.expire_all()
    assert db.session.get(Heat, heat.id).heat_number == 7
    assert db.session.get(Event, pro_event.id) is not None
    persisted_config = db.session.get(type(tournament), tournament.id).get_schedule_config()
    assert persisted_config['friday_pro_event_ids'] == [pro_event.id]
    assert persisted_config['friday_feature_notes'] == 'published Friday selection'

    upload_calls.clear()
    original_get_or_404 = registration_routes.db.get_or_404

    def assert_child_read_follows_lock(model, object_id, *args, **kwargs):
        if model is CollegeCompetitor:
            assert upload_calls == [tournament.id]
        return original_get_or_404(model, object_id, *args, **kwargs)

    monkeypatch.setattr(
        registration_routes.db,
        'get_or_404',
        assert_child_read_follows_lock,
    )
    assert client.post(
        f'/registration/{tournament.id}/college/competitor/{competitor.id}/delete',
        data={},
        follow_redirects=False,
    ).status_code == 302

    assert ranking_calls == [tournament.id]
    assert event_calls == [tournament.id, tournament.id, tournament.id]
    assert friday_calls == [tournament.id]
    assert upload_calls == [tournament.id]
    assert process_import.call_args.kwargs['commit'] is False


def test_direct_heat_generation_refreshes_event_after_parent_lock(
    db_session, monkeypatch,
):
    from services.heat_generator import generate_event_heats

    tournament = make_tournament(db_session, name='Fresh Event Generation')
    event = make_event(db_session, tournament, 'Underhand')
    db_session.flush()

    refreshed = []
    original_refresh = db.session.refresh

    def recording_refresh(instance, *args, **kwargs):
        refreshed.append(instance.id)
        return original_refresh(instance, *args, **kwargs)

    monkeypatch.setattr(db.session, 'refresh', recording_refresh)

    with pytest.raises(ValueError, match='No competitors entered'):
        generate_event_heats(event)
    assert refreshed == [event.id]


def test_legacy_friday_config_rechecks_database_after_lock(monkeypatch):
    from routes.scheduling import friday_feature as friday_routes

    current = {
        'friday_pro_event_ids': [99],
        'friday_feature_notes': 'current database value',
    }

    class TournamentStub:
        id = 7

        def __init__(self):
            self.read_count = 0
            self.saved = []

        def get_schedule_config(self):
            self.read_count += 1
            return {} if self.read_count == 1 else dict(current)

        def set_schedule_config(self, value):
            self.saved.append(value)

    tournament = TournamentStub()
    monkeypatch.setattr(
        friday_routes,
        '_load_legacy_fnf_config',
        lambda _tournament_id: {
            'event_ids': [1],
            'notes': 'stale legacy value',
        },
    )
    monkeypatch.setattr(
        friday_routes,
        'lock_tournament_schedule',
        lambda locked_tournament: locked_tournament,
    )

    loaded = friday_routes._load_fnf_config(tournament)

    assert loaded == {
        'event_ids': [99],
        'notes': 'current database value',
    }
    assert tournament.saved == []


def test_explicit_empty_friday_config_does_not_revive_legacy_file(monkeypatch):
    from routes.scheduling import friday_feature as friday_routes

    class TournamentStub:
        id = 8

        def __init__(self):
            self.saved = []

        def get_schedule_config(self):
            return {
                'friday_pro_event_ids': [],
                'friday_feature_notes': '',
            }

        def set_schedule_config(self, value):
            self.saved.append(value)

    legacy_loader = Mock(return_value={
        'event_ids': [1],
        'notes': 'stale legacy value',
    })
    monkeypatch.setattr(
        friday_routes,
        '_load_legacy_fnf_config',
        legacy_loader,
    )
    monkeypatch.setattr(
        friday_routes,
        'lock_tournament_schedule',
        Mock(side_effect=AssertionError('explicit config must not lock or migrate')),
    )

    tournament = TournamentStub()
    assert friday_routes._load_fnf_config(tournament) == {
        'event_ids': [],
        'notes': '',
    }
    legacy_loader.assert_not_called()
    assert tournament.saved == []
