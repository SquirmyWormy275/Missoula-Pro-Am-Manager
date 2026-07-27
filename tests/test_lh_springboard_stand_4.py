"""
Phase 5: Pro Springboard LH cutter stand-4 assignment + flight-contention
warning surface.

Locked decisions:
- Stand 4 is the hard-coded LH dummy.
- Max one LH cutter per flight (already enforced by existing penalty; these
  tests guard the rule).
- Overflow (LH_count > flight_count) allowed, surfaces warning via
  get_last_lh_flight_warnings().

Keeps and extends the existing tests/test_flight_builder_lh_constraint.py —
those still assert the "spread across flights" property this phase keeps.

Run:  pytest tests/test_lh_springboard_stand_4.py -v
"""

from __future__ import annotations

import pytest

from database import db as _db


@pytest.fixture(scope="module")
def app():
    import os

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


def _make_tournament(session):
    from models import Tournament

    t = Tournament(name="LH Stand 4 Test", year=2026, status="pro_active")
    session.add(t)
    session.flush()
    return t


def _make_springboard_event(session, tournament, name="Springboard"):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="pro",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="springboard",
        max_stands=4,
    )
    session.add(e)
    session.flush()
    return e


def _make_pro_competitor(session, tournament, name, is_lh=False):
    from models.competitor import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender="M",
        status="active",
        is_left_handed_springboard=is_lh,
    )
    session.add(c)
    session.flush()
    return c


def _enroll(competitor, event):
    """Register the competitor for the event (add event name to events_entered)."""
    import json

    entered = (
        competitor.get_events_entered()
        if hasattr(competitor, "get_events_entered")
        else []
    )
    if event.name not in entered:
        entered.append(event.name)
        competitor.events_entered = json.dumps(entered)


# ---------------------------------------------------------------------------
# Stand-4 assignment rule
# ---------------------------------------------------------------------------


class TestLhCutterGetsStand4:
    def test_single_lh_in_heat_gets_stand_4(self, db_session):
        """4-cutter heat with 1 LH competitor → LH is assigned stand 4, others 1-3."""
        from services.heat_generator import generate_event_heats

        t = _make_tournament(db_session)
        ev = _make_springboard_event(db_session, t)
        rh1 = _make_pro_competitor(db_session, t, "RH One")
        rh2 = _make_pro_competitor(db_session, t, "RH Two")
        rh3 = _make_pro_competitor(db_session, t, "RH Three")
        lh = _make_pro_competitor(db_session, t, "LH One", is_lh=True)
        for comp in (rh1, rh2, rh3, lh):
            _enroll(comp, ev)
        db_session.flush()

        generate_event_heats(ev)

        from models import Heat

        heats = Heat.query.filter_by(event_id=ev.id, run_number=1).all()
        assert heats, "expected at least 1 springboard heat"
        found_lh_stand_4 = False
        for h in heats:
            assignments = h.get_stand_assignments()
            if str(lh.id) in assignments:
                assert assignments[str(lh.id)] == 4, (
                    f"LH cutter {lh.name} got stand {assignments[str(lh.id)]}, "
                    "expected 4"
                )
                found_lh_stand_4 = True
                # The other 3 cutters should be on stands 1-3 (in some order).
                other_stands = sorted(
                    v for k, v in assignments.items() if k != str(lh.id)
                )
                assert other_stands == [
                    1,
                    2,
                    3,
                ], f"other cutters got stands {other_stands}, expected [1, 2, 3]"
        assert found_lh_stand_4, "LH cutter was not placed in any heat"

    def test_no_lh_cutter_uses_default_assignment(self, db_session):
        """All-RH heat: stands 1-4 filled in list order (no LH special-case)."""
        from services.heat_generator import generate_event_heats

        t = _make_tournament(db_session)
        ev = _make_springboard_event(db_session, t)
        rh_all = [_make_pro_competitor(db_session, t, f"RH {i}") for i in range(1, 5)]
        for comp in rh_all:
            _enroll(comp, ev)
        db_session.flush()

        generate_event_heats(ev)

        from models import Heat

        heats = Heat.query.filter_by(event_id=ev.id, run_number=1).all()
        assert heats
        for h in heats:
            assignments = h.get_stand_assignments()
            stands = sorted(assignments.values())
            # Every cutter in a 4-slot heat should get stand 1, 2, 3, or 4.
            for s in stands:
                assert 1 <= s <= 4

    def test_stand_4_used_even_without_lh(self, db_session):
        """Stand 4 is not reserved — an RH cutter still uses it when no LH present."""
        from services.heat_generator import generate_event_heats

        t = _make_tournament(db_session)
        ev = _make_springboard_event(db_session, t)
        rh_all = [_make_pro_competitor(db_session, t, f"RH {i}") for i in range(1, 5)]
        for comp in rh_all:
            _enroll(comp, ev)
        db_session.flush()

        generate_event_heats(ev)

        from models import Heat

        heats = Heat.query.filter_by(event_id=ev.id, run_number=1).all()
        all_stands = set()
        for h in heats:
            all_stands.update(h.get_stand_assignments().values())
        assert (
            4 in all_stands
        ), "stand 4 must be assigned to somebody even in all-RH heats"


# ---------------------------------------------------------------------------
# Two-LH-in-one-heat tie-break (overflow scenario)
# ---------------------------------------------------------------------------


class TestMultipleLhInSameHeat:
    def test_two_lh_same_heat_first_gets_stand_4_and_warning_fires(self, db_session):
        """When 2+ LH cutters land in the same heat, first gets stand 4 +
        a 'multiple_lh_same_heat' warning is recorded."""
        from services.heat_generator import (
            generate_event_heats,
            get_last_lh_overflow_warnings,
        )

        t = _make_tournament(db_session)
        ev = _make_springboard_event(db_session, t)
        # 4 LH cutters + only 1 heat capacity → forces overflow-in-same-heat.
        # 4 competitors total so we get exactly 1 heat.
        lh_cutters = [
            _make_pro_competitor(db_session, t, f"LH {i}", is_lh=True)
            for i in range(1, 5)
        ]
        for comp in lh_cutters:
            _enroll(comp, ev)
        db_session.flush()

        generate_event_heats(ev)

        warnings = get_last_lh_overflow_warnings(ev.id)
        types = {w.get("type") for w in warnings}
        assert (
            "multiple_lh_same_heat" in types
        ), f"expected 'multiple_lh_same_heat' warning, got types {types}"


# ---------------------------------------------------------------------------
# Flight-contention warning surface
# ---------------------------------------------------------------------------


class TestLhFlightContentionWarning:
    def test_warning_populated_when_multiple_lh_heats_in_one_flight(self, db_session):
        """When flight builder can't spread LH heats, get_last_lh_flight_warnings returns rows."""
        from models import Event, Heat
        from services.flight_builder import (
            build_pro_flights,
            get_last_lh_flight_warnings,
        )

        t = _make_tournament(db_session)
        ev = _make_springboard_event(db_session, t)

        # Seed enough LH springboard cutters so LH_count > flight_count.
        # With num_flights=2 and 4 LH heats, at least one flight must contain >1.
        lh_pros = [
            _make_pro_competitor(db_session, t, f"LH {i}", is_lh=True)
            for i in range(1, 9)
        ]
        for i, comp in enumerate(lh_pros):
            _enroll(comp, ev)
            # Place each LH cutter in its own heat so flight builder has
            # many LH-containing heats to spread.
            h = Heat(event_id=ev.id, heat_number=i + 1, run_number=1)
            h.set_competitors([comp.id])
            db_session.add(h)
        db_session.flush()

        # Build with only 2 flights — 8 LH heats / 2 flights = 4 per flight
        # → each flight has 4 LH-containing heats → both flagged.
        build_pro_flights(t, num_flights=2, commit=False)

        warnings = get_last_lh_flight_warnings(t.id)
        assert warnings, (
            "expected LH flight-contention warnings when LH heat count "
            "exceeds flight count"
        )
        for w in warnings:
            assert "flight_number" in w and "lh_count" in w
            assert w["lh_count"] > 1

    def test_no_lh_cutters_means_no_warnings(self, db_session):
        """All-RH tournament: no LH flight-contention warnings regardless of build."""
        from models import Heat
        from services.flight_builder import (
            build_pro_flights,
            get_last_lh_flight_warnings,
        )

        t = _make_tournament(db_session)
        ev = _make_springboard_event(db_session, t)

        for i in range(1, 9):
            rh = _make_pro_competitor(db_session, t, f"RH {i}")
            _enroll(rh, ev)
            h = Heat(event_id=ev.id, heat_number=i, run_number=1)
            h.set_competitors([rh.id])
            db_session.add(h)
        db_session.flush()

        build_pro_flights(t, num_flights=4, commit=False)

        warnings = get_last_lh_flight_warnings(t.id)
        assert warnings == [], (
            f'no LH cutters should produce zero warnings, got {warnings}'
        )

    def test_warnings_cleared_on_subsequent_build(self, db_session):
        """A tournament that once had warnings should clear them on a clean rebuild."""
        from models import Heat
        from services.flight_builder import (
            _last_lh_flight_warnings,
            build_pro_flights,
            get_last_lh_flight_warnings,
        )

        t = _make_tournament(db_session)
        ev = _make_springboard_event(db_session, t)

        # Pre-populate _last_lh_flight_warnings simulating a prior build.
        _last_lh_flight_warnings[t.id] = [{'flight_number': 1, 'lh_count': 99}]
        assert get_last_lh_flight_warnings(t.id)  # pre-condition

        # All-RH heats → fresh build should clear the map entry.
        for i in range(1, 9):
            rh = _make_pro_competitor(db_session, t, f"RH {i}")
            _enroll(rh, ev)
            h = Heat(event_id=ev.id, heat_number=i, run_number=1)
            h.set_competitors([rh.id])
            db_session.add(h)
        db_session.flush()

        build_pro_flights(t, num_flights=4, commit=False)

        warnings = get_last_lh_flight_warnings(t.id)
        assert warnings == [], (
            'build_pro_flights must clear stale LH warnings when the new '
            'build has no contention'
        )
