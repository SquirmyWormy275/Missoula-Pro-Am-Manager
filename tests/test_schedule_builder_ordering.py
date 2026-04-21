"""
Tests for services.schedule_builder.get_friday_ordered_heats and
services.schedule_builder.get_saturday_ordered_heats.

Verifies that the ordered-heats helpers expose the same authoritative run
order that build_day_schedule() is built on, with no side effects.

Run:
    pytest tests/test_schedule_builder_ordering.py -v
"""

import os

import pytest

from database import db as _db


@pytest.fixture(scope="module")
def app():
    """Test Flask app with temp-file SQLite built via flask db upgrade."""
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
    """Wrap each test in a transaction and roll back afterward."""
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def tournament(db_session):
    from models import Tournament

    t = Tournament(name="Ordering Test 2026", year=2026, status="setup")
    db_session.add(t)
    db_session.flush()
    return t


def _make_event(
    db_session,
    tournament,
    name,
    event_type="college",
    gender=None,
    stand_type="saw_hand",
    requires_dual_runs=False,
    is_open=False,
):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender=gender,
        scoring_type="time",
        stand_type=stand_type,
        requires_dual_runs=requires_dual_runs,
        is_open=is_open,
    )
    db_session.add(e)
    db_session.flush()
    return e


def _make_flight(db_session, tournament, flight_number):
    from models.heat import Flight

    f = Flight(tournament_id=tournament.id, flight_number=flight_number)
    db_session.add(f)
    db_session.flush()
    return f


def _make_heat(
    db_session, event, heat_number, run_number=1, flight=None, flight_position=None
):
    from models import Heat

    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
    )
    if flight is not None:
        h.flight_id = flight.id
        h.flight_position = flight_position
    db_session.add(h)
    db_session.flush()
    return h


# ---------------------------------------------------------------------------
# Friday ordering
# ---------------------------------------------------------------------------


def test_friday_default_order_uses_college_sort_key(db_session, tournament):
    """Absent friday_event_order, events order by _college_friday_sort_key.

    Axe Throw (open) should come before Single Buck (closed), which is
    before Birling (always last).
    """
    from services.schedule_builder import get_friday_ordered_heats

    axe = _make_event(
        db_session,
        tournament,
        "Axe Throw",
        "college",
        "M",
        stand_type="axe_throw",
        is_open=True,
    )
    sb = _make_event(
        db_session, tournament, "Single Buck", "college", "M", stand_type="saw_hand"
    )
    birling = _make_event(
        db_session, tournament, "Birling", "college", "M", stand_type="birling"
    )

    _make_heat(db_session, axe, 1)
    _make_heat(db_session, sb, 1)
    _make_heat(db_session, birling, 1)

    ordered = get_friday_ordered_heats(tournament)
    event_ids = [h.event_id for h in ordered]
    assert event_ids == [axe.id, sb.id, birling.id]


def test_friday_custom_order_overrides_default(db_session, tournament):
    """friday_event_order in schedule_config dictates event order."""
    from services.schedule_builder import get_friday_ordered_heats

    axe = _make_event(
        db_session,
        tournament,
        "Axe Throw",
        "college",
        "M",
        stand_type="axe_throw",
        is_open=True,
    )
    sb = _make_event(
        db_session, tournament, "Single Buck", "college", "M", stand_type="saw_hand"
    )
    db = _make_event(
        db_session, tournament, "Double Buck", "college", "M", stand_type="saw_hand"
    )

    _make_heat(db_session, axe, 1)
    _make_heat(db_session, sb, 1)
    _make_heat(db_session, db, 1)

    # Force Double Buck first, Axe Throw second, Single Buck third
    tournament.set_schedule_config({"friday_event_order": [db.id, axe.id, sb.id]})
    db_session.flush()

    ordered = get_friday_ordered_heats(tournament)
    event_ids = [h.event_id for h in ordered]
    assert event_ids == [db.id, axe.id, sb.id]


def test_friday_multiple_heats_ordered_by_heat_number(db_session, tournament):
    """Within an event, heats come out in heat_number ascending order."""
    from services.schedule_builder import get_friday_ordered_heats

    sb = _make_event(
        db_session, tournament, "Single Buck", "college", "M", stand_type="saw_hand"
    )
    _make_heat(db_session, sb, 3)
    _make_heat(db_session, sb, 1)
    _make_heat(db_session, sb, 2)

    ordered = get_friday_ordered_heats(tournament)
    assert [h.heat_number for h in ordered] == [1, 2, 3]


def test_friday_excludes_saturday_spillover_events(db_session, tournament):
    """Events listed in saturday_college_event_ids don't appear on Friday."""
    from services.schedule_builder import get_friday_ordered_heats

    sb = _make_event(
        db_session, tournament, "Single Buck", "college", "M", stand_type="saw_hand"
    )
    spillover = _make_event(
        db_session,
        tournament,
        "Standing Block Speed",
        "college",
        "M",
        stand_type="standing_block",
    )

    _make_heat(db_session, sb, 1)
    _make_heat(db_session, spillover, 1)

    tournament.set_schedule_config(
        {
            "saturday_college_event_ids": [spillover.id],
        }
    )
    db_session.flush()

    ordered = get_friday_ordered_heats(tournament)
    event_ids = {h.event_id for h in ordered}
    assert sb.id in event_ids
    assert spillover.id not in event_ids


def test_friday_excludes_run_2_heats(db_session, tournament):
    """Dual-run run_number=2 heats do not appear on Friday."""
    from services.schedule_builder import get_friday_ordered_heats

    sb = _make_event(
        db_session,
        tournament,
        "Single Buck",
        "college",
        "M",
        stand_type="saw_hand",
        requires_dual_runs=True,
    )
    _make_heat(db_session, sb, 1, run_number=1)
    _make_heat(db_session, sb, 1, run_number=2)

    ordered = get_friday_ordered_heats(tournament)
    assert all(h.run_number == 1 for h in ordered)


# ---------------------------------------------------------------------------
# Saturday ordering
# ---------------------------------------------------------------------------


def test_saturday_uses_flight_order_when_flights_exist(db_session, tournament):
    """With flights: Flight.flight_number + flight.get_heats_ordered() rules."""
    from services.schedule_builder import get_saturday_ordered_heats

    sb = _make_event(
        db_session, tournament, "Single Buck", "pro", "M", stand_type="saw_hand"
    )
    dbuck = _make_event(
        db_session, tournament, "Double Buck", "pro", "M", stand_type="saw_hand"
    )

    f1 = _make_flight(db_session, tournament, 1)
    f2 = _make_flight(db_session, tournament, 2)

    h_sb1 = _make_heat(db_session, sb, 1, flight=f1, flight_position=1)
    h_db1 = _make_heat(db_session, dbuck, 1, flight=f1, flight_position=2)
    h_sb2 = _make_heat(db_session, sb, 2, flight=f2, flight_position=1)

    ordered = get_saturday_ordered_heats(tournament)
    heat_ids = [h.id for h in ordered]
    assert heat_ids == [h_sb1.id, h_db1.id, h_sb2.id]


def test_saturday_fallback_to_event_order_when_no_flights(db_session, tournament):
    """No flights: pro heats ordered by event_id + heat_number ascending."""
    from services.schedule_builder import get_saturday_ordered_heats

    sb = _make_event(
        db_session, tournament, "Single Buck", "pro", "M", stand_type="saw_hand"
    )
    dbuck = _make_event(
        db_session, tournament, "Double Buck", "pro", "M", stand_type="saw_hand"
    )

    h_sb1 = _make_heat(db_session, sb, 1)
    h_sb2 = _make_heat(db_session, sb, 2)
    h_db1 = _make_heat(db_session, dbuck, 1)

    ordered = get_saturday_ordered_heats(tournament)
    # event_id ascending: sb (smaller id) first, then dbuck
    assert ordered[0].id == h_sb1.id
    assert ordered[1].id == h_sb2.id
    assert ordered[2].id == h_db1.id


def test_saturday_fallback_respects_custom_order(db_session, tournament):
    """No flights + saturday_event_order -> custom pro event order is used."""
    from services.schedule_builder import get_saturday_ordered_heats

    sb = _make_event(
        db_session, tournament, "Single Buck", "pro", "M", stand_type="saw_hand"
    )
    dbuck = _make_event(
        db_session, tournament, "Double Buck", "pro", "M", stand_type="saw_hand"
    )

    _make_heat(db_session, sb, 1)
    _make_heat(db_session, dbuck, 1)

    tournament.set_schedule_config({"saturday_event_order": [dbuck.id, sb.id]})
    db_session.flush()

    ordered = get_saturday_ordered_heats(tournament)
    assert [h.event_id for h in ordered] == [dbuck.id, sb.id]


def test_saturday_includes_dual_run_run_2_heats(db_session, tournament):
    """Day-split Run 2 heats route to Saturday even without flight attachment."""
    from services.schedule_builder import get_saturday_ordered_heats

    chokerman = _make_event(
        db_session,
        tournament,
        "Chokerman's Race",
        "college",
        "M",
        stand_type="chokerman",
        requires_dual_runs=True,
    )
    # Run 2 heats only — no flights, no pro heats
    h_run2 = _make_heat(db_session, chokerman, 1, run_number=2)

    ordered = get_saturday_ordered_heats(tournament)
    assert h_run2.id in [h.id for h in ordered]


def test_saturday_and_friday_helpers_are_pure_reads(db_session, tournament):
    """Calling the helpers does not mutate schedule_config or any heat."""
    from services.schedule_builder import (
        get_friday_ordered_heats,
        get_saturday_ordered_heats,
    )

    sb = _make_event(
        db_session, tournament, "Single Buck", "college", "M", stand_type="saw_hand"
    )
    h = _make_heat(db_session, sb, 1)
    # capture pre-state
    stands_before = h.stand_assignments
    config_before = tournament.schedule_config

    get_friday_ordered_heats(tournament)
    get_saturday_ordered_heats(tournament)

    assert h.stand_assignments == stands_before
    assert tournament.schedule_config == config_before
