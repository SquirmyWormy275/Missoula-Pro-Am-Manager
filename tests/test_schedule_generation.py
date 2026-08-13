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


# `_make_heat` stood here, building a heat with competitor JSON and no
# assignment rows. Column-only on purpose: its one caller was the autofix
# test below, and what that test measured was `run_preflight_autofix` walking
# every heat, noticing the rows were missing, and building them. A helper
# that seated the rows itself would have left the call with nothing to fix.
#
# D12-C commit F2 deleted that sweep. `set_roster` writes the rows, every
# caller goes through it, and a heat whose rows are missing is now a heat
# with no roster rather than a heat awaiting repair, so a sweep that rebuilt
# rows from the JSON column would be reading a store nothing writes to.


def test_run_preflight_autofix_reports_its_summary_numbers(db_session, monkeypatch):
    """The autofix still reports what each of its steps did.

    This was `test_run_preflight_autofix_syncs_heat_assignments`, and its
    first claim was that the function rebuilt `heat_assignments` rows from
    the JSON column and reported the count as `heats_fixed`. D12-C commit F2
    deleted that sweep and both counters. The four steps that remain, gear
    parsing, one-sided pair completion, partner auto-assignment and spillover
    integration, are still summarised the same way, and that is what is
    asserted below.
    """
    from services.schedule_generation import run_preflight_autofix

    tournament = _make_tournament(db_session)
    _make_event(db_session, tournament, 'Underhand')
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
    # MOCK FIDELITY: real signature is integrate_college_spillover_into_flights(
    # tournament, college_event_ids=None, commit=False, placement_mode=None).
    # This 2-positional lambda only matches the current call site at
    # services/schedule_generation.py:_integrate_spillover. If a future caller
    # adds positional args (e.g., placement_mode=) the mock will silently keep
    # passing while production crashes. If you change the production signature,
    # also update this lambda. See V2.13.0/V2.14.0/V2.14.5 mock-shape trilogy
    # in docs/solutions/test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md.
    monkeypatch.setattr(
        'services.flight_builder.integrate_college_spillover_into_flights',
        lambda _tournament, _ids: {'integrated_heats': 4},
    )

    result = run_preflight_autofix(tournament, saturday_ids=[999])
    db_session.flush()

    assert 'heats_fixed' not in result
    assert 'heats_checked' not in result
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

    # MOCK FIDELITY: build_pro_flights real signature is
    # (tournament, num_flights=None, commit=True). Mock must match — the
    # prior 1-positional lambda broke silently when V2.14.2 threaded
    # num_flights= through the caller in services/schedule_generation.py.
    # See feedback_mock_signature_matches_bug.md in memory.
    monkeypatch.setattr('services.heat_generator.generate_event_heats', _fake_generate)
    monkeypatch.setattr(
        'services.flight_builder.build_pro_flights',
        lambda tournament, num_flights=None, commit=True: 2,
    )

    result = generate_tournament_schedule_artifacts(tournament.id)

    assert result['ok'] is True
    assert result['generated'] == 1
    assert result['skipped'] == 1
    assert result['errors'] == ['kaboom']
    assert result['flights'] == 2


def test_generate_tournament_schedule_rolls_back_partial_failed_event(
    db_session,
    monkeypatch,
):
    """A failed event must not leak flushed heat rows into the final commit."""
    from models import Heat
    from services.schedule_generation import generate_tournament_schedule_artifacts

    tournament = _make_tournament(db_session)
    failed_event = _make_event(db_session, tournament, 'Partial Failure', event_type='pro')

    def _partial_failure(event):
        _db.session.add(Heat(event_id=event.id, heat_number=1, run_number=1))
        _db.session.flush()
        raise RuntimeError('forced failure after flush')

    monkeypatch.setattr('services.heat_generator.generate_event_heats', _partial_failure)

    result = generate_tournament_schedule_artifacts(tournament.id)

    assert result['ok'] is True
    assert result['generated'] == 0
    assert result['errors'] == ['forced failure after flush']
    assert Heat.query.filter_by(event_id=failed_event.id).count() == 0


def test_generate_event_heats_refuses_to_delete_completed_heat_without_score_rows(
    db_session,
):
    """A fully scratched heat is still race-day history, not disposable layout."""
    from models import Heat
    from services.heat_generator import HeatGenerationSafetyError, generate_event_heats

    tournament = _make_tournament(db_session)
    event = _make_event(db_session, tournament, 'All Scratched')
    heat = Heat(event_id=event.id, heat_number=1, run_number=1, status='completed')
    _db.session.add(heat)
    _db.session.flush()

    with pytest.raises(HeatGenerationSafetyError, match='completed heat history'):
        generate_event_heats(event)

    assert _db.session.get(Heat, heat.id) is heat


def test_generate_event_heats_refuses_completed_score_rows_without_completed_heat(
    db_session,
):
    """A legacy stale heat status cannot make completed scores disposable."""
    from models import EventResult
    from services.heat_generator import HeatGenerationSafetyError, generate_event_heats

    tournament = _make_tournament(db_session)
    event = _make_event(db_session, tournament, 'Stale Heat Status')
    _db.session.add(EventResult(
        event_id=event.id,
        competitor_id=1,
        competitor_type='pro',
        competitor_name='Historical Result',
        status='completed',
        result_value=10.0,
    ))
    _db.session.flush()

    with pytest.raises(HeatGenerationSafetyError, match='scored results'):
        generate_event_heats(event)
