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


# ---------------------------------------------------------------------------
# Friday end-of-day lock (2026-04-23)
# ---------------------------------------------------------------------------
# Operator rule: the FINAL FOUR Friday college events must auto-generate as
# 1. Men's Chokerman's Race
# 2. Women's Chokerman's Race
# 3. Men's Birling
# 4. Women's Birling
# Override path: operator drag-drops on Run Show → friday_event_order set in
# schedule_config → custom order wins.


class TestFridayEndOfDayLock:
    """The four end-of-day events must always auto-sort in the locked order
    when no custom_order is set, regardless of name/gender enum drift."""

    def _seed_full_friday(self, db_session, tournament):
        """Seed a representative Friday — open events, closed events, and
        all four lock events (M/F Chokerman, M/F Birling) in scrambled
        creation order so the sort has work to do."""
        events = {}
        # Scramble: birling FIRST, then closed, then chokerman, then open.
        # If sort works, lock order should still come out at the END as
        # M-Chokerman, W-Chokerman, M-Birling, W-Birling.
        events['birling_f'] = _make_event(
            db_session, tournament, "Birling", "college", "F",
            stand_type="birling",
        )
        events['birling_m'] = _make_event(
            db_session, tournament, "Birling", "college", "M",
            stand_type="birling",
        )
        events['underhand_m'] = _make_event(
            db_session, tournament, "Underhand Speed", "college", "M",
            stand_type="underhand",
        )
        events['choker_f'] = _make_event(
            db_session, tournament, "Chokerman's Race", "college", "F",
            stand_type="chokerman", requires_dual_runs=True,
        )
        events['choker_m'] = _make_event(
            db_session, tournament, "Chokerman's Race", "college", "M",
            stand_type="chokerman", requires_dual_runs=True,
        )
        events['axe'] = _make_event(
            db_session, tournament, "Axe Throw", "college", None,
            stand_type="axe_throw", is_open=True,
        )
        # One heat each so they appear in the ordered output.
        for ev in events.values():
            _make_heat(db_session, ev, 1)
        return events

    def test_default_sort_locks_final_four_in_canonical_order(self, db_session, tournament):
        """No custom_order: the last four events MUST be M-Chokerman,
        W-Chokerman, M-Birling, W-Birling in that exact order."""
        from services.schedule_builder import build_day_schedule

        events = self._seed_full_friday(db_session, tournament)
        sched = build_day_schedule(tournament)

        friday = sched['friday_day']
        last_four = friday[-4:]
        last_four_ids = [entry['event_id'] for entry in last_four]

        expected = [
            events['choker_m'].id,
            events['choker_f'].id,
            events['birling_m'].id,
            events['birling_f'].id,
        ]
        assert last_four_ids == expected, (
            f"End-of-day lock broken. Expected {expected}, got {last_four_ids} "
            f"(labels: {[e['label'] for e in last_four]})"
        )

    def test_chokerman_lands_before_birling_when_only_some_locks_present(self, db_session, tournament):
        """Partial lock — only Chokerman M+F + Birling M (no F birling).
        The three present lock events must still come last in lock order."""
        from services.schedule_builder import build_day_schedule

        underhand_m = _make_event(
            db_session, tournament, "Underhand Speed", "college", "M",
            stand_type="underhand",
        )
        birling_m = _make_event(
            db_session, tournament, "Birling", "college", "M",
            stand_type="birling",
        )
        choker_m = _make_event(
            db_session, tournament, "Chokerman's Race", "college", "M",
            stand_type="chokerman", requires_dual_runs=True,
        )
        choker_f = _make_event(
            db_session, tournament, "Chokerman's Race", "college", "F",
            stand_type="chokerman", requires_dual_runs=True,
        )
        for ev in (underhand_m, birling_m, choker_m, choker_f):
            _make_heat(db_session, ev, 1)

        sched = build_day_schedule(tournament)
        friday = sched['friday_day']
        ordered_ids = [entry['event_id'] for entry in friday]

        # Underhand first, then chokerman M, F, then birling M.
        assert ordered_ids == [
            underhand_m.id, choker_m.id, choker_f.id, birling_m.id,
        ], f"Lock order broken on partial set: {ordered_ids}"

    def test_custom_friday_order_overrides_lock(self, db_session, tournament):
        """When operator sets schedule_config['friday_event_order'], the lock
        defers to their explicit choice — that's the documented override path."""
        from services.schedule_builder import build_day_schedule

        events = self._seed_full_friday(db_session, tournament)
        # Operator wants Birling FIRST (weird, but their call). Custom order
        # puts birling at position 0; lock should NOT re-insert chokerman after.
        custom = [
            events['birling_m'].id,
            events['birling_f'].id,
            events['choker_m'].id,
            events['choker_f'].id,
            events['underhand_m'].id,
            events['axe'].id,
        ]
        tournament.set_schedule_config({'friday_event_order': custom})
        db_session.flush()

        sched = build_day_schedule(tournament)
        friday = sched['friday_day']
        ordered_ids = [entry['event_id'] for entry in friday]

        assert ordered_ids == custom, (
            f"Custom order should win over lock; got {ordered_ids}"
        )

    def test_lock_helper_returns_position_for_lock_events_only(self):
        """Unit test the lookup helper directly — defends against name /
        gender enum drift."""
        from services.schedule_builder import _friday_end_of_day_lock_position

        class _StubEvent:
            def __init__(self, name, gender):
                self.name = name
                self.gender = gender

        # Lock events.
        assert _friday_end_of_day_lock_position(_StubEvent("Chokerman's Race", "M")) == 0
        assert _friday_end_of_day_lock_position(_StubEvent("Chokerman's Race", "F")) == 1
        assert _friday_end_of_day_lock_position(_StubEvent("Birling", "M")) == 2
        assert _friday_end_of_day_lock_position(_StubEvent("Birling", "F")) == 3

        # Apostrophe / casing variants must still match (name normalization).
        assert _friday_end_of_day_lock_position(_StubEvent("chokermans race", "M")) == 0
        assert _friday_end_of_day_lock_position(_StubEvent("CHOKERMAN'S RACE", "F")) == 1

        # Non-lock events.
        assert _friday_end_of_day_lock_position(_StubEvent("Underhand Speed", "M")) is None
        assert _friday_end_of_day_lock_position(_StubEvent("Birling", None)) is None
        assert _friday_end_of_day_lock_position(_StubEvent("", "M")) is None
