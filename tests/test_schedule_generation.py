"""Tests for schedule-generation application services."""
import os

import pytest

from database import db as _db


@pytest.fixture(scope='module')
def app():
    from tests.db_test_utils import create_test_app

    _app, db_path = create_test_app()
    with _app.app_context():
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


def _make_tournament(db_session):
    from models import Tournament

    tournament = Tournament(name='Schedule Generation Test', year=2026, status='setup')
    db_session.add(tournament)
    db_session.flush()
    return tournament


def _make_event(db_session, tournament, name, event_type='pro'):
    from models import Event

    event = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        scoring_type='time',
        scoring_order='lowest_wins',
        status='pending',
    )
    db_session.add(event)
    db_session.flush()
    return event


def _make_heat(db_session, event, competitor_ids=None):
    from models import Heat

    heat = Heat(event_id=event.id, heat_number=1, run_number=1)
    if competitor_ids is not None:
        heat.set_competitors(competitor_ids)
    db_session.add(heat)
    db_session.flush()
    return heat


def test_run_preflight_autofix_syncs_heat_assignments(db_session, monkeypatch):
    from models.heat import HeatAssignment
    from services.schedule_generation import run_preflight_autofix

    tournament = _make_tournament(db_session)
    event = _make_event(db_session, tournament, 'Underhand')
    heat = _make_heat(db_session, event, competitor_ids=[101, 202])
    heat.set_stand_assignment(101, 1)
    heat.set_stand_assignment(202, 2)
    db_session.flush()

    monkeypatch.setattr(
        'services.gear_sharing.parse_all_gear_details',
        lambda _tournament: {'parsed': 2},
    )
    monkeypatch.setattr(
        'services.gear_sharing.complete_one_sided_pairs',
        lambda _tournament: {'completed': 1},
    )
    monkeypatch.setattr(
        'services.partner_matching.auto_assign_pro_partners',
        lambda _tournament: {'assigned_pairs': 3},
    )
    monkeypatch.setattr(
        'services.flight_builder.integrate_college_spillover_into_flights',
        lambda _tournament, _ids: {'integrated_heats': 4},
    )

    result = run_preflight_autofix(tournament, saturday_ids=[999])
    db_session.flush()

    rows = HeatAssignment.query.filter_by(heat_id=heat.id).order_by(HeatAssignment.competitor_id).all()
    assert [row.competitor_id for row in rows] == [101, 202]
    assert [row.stand_number for row in rows] == [1, 2]
    assert result['heats_fixed'] == 1
    assert result['gear_parsed']['parsed'] == 2
    assert result['gear_pairs_completed'] == 1
    assert result['partner_summary']['assigned_pairs'] == 3
    assert result['spillover']['integrated_heats'] == 4


def test_generate_tournament_schedule_artifacts_returns_error_for_missing_tournament():
    from services.schedule_generation import generate_tournament_schedule_artifacts

    result = generate_tournament_schedule_artifacts(999999)

    assert result['ok'] is False
    assert 'not found' in result['error']


def test_generate_tournament_schedule_artifacts_orchestrates_heat_and_flight_generation(
    db_session,
    monkeypatch,
):
    from models import Heat
    from services.schedule_generation import generate_tournament_schedule_artifacts

    tournament = _make_tournament(db_session)
    success_event = _make_event(db_session, tournament, 'Success Event', event_type='pro')
    skipped_event = _make_event(db_session, tournament, 'Skip Event', event_type='pro')
    error_event = _make_event(db_session, tournament, 'Error Event', event_type='college')

    def _fake_generate(event):
        if event.id == skipped_event.id:
            raise RuntimeError('No competitors entered for this event')
        if event.id == error_event.id:
            raise RuntimeError('kaboom')
        heat = Heat(event_id=event.id, heat_number=1, run_number=1)
        _db.session.add(heat)
        _db.session.flush()

    monkeypatch.setattr('services.heat_generator.generate_event_heats', _fake_generate)
    monkeypatch.setattr('services.flight_builder.build_pro_flights', lambda _tournament: 2)

    result = generate_tournament_schedule_artifacts(tournament.id)

    assert result['ok'] is True
    assert result['generated'] == 1
    assert result['skipped'] == 1
    assert result['errors'] == ['kaboom']
    assert result['flights'] == 2
