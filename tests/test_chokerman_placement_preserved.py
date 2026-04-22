"""
Phase 2 regression guard: Chokerman's Race Run 2 must stay at the end of the
last flight regardless of placement_mode — it is the documented show closer
(FlightLogic.md §4.1).

Run:  pytest tests/test_chokerman_placement_preserved.py -v
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

    t = Tournament(name="Chokerman Closer Test", year=2026, status="pro_active")
    session.add(t)
    session.flush()
    return t


def _make_college_event(
    session, tournament, name, stand_type, gender=None, requires_dual_runs=False
):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="college",
        gender=gender,
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type=stand_type,
        requires_dual_runs=requires_dual_runs,
    )
    session.add(e)
    session.flush()
    return e


def _make_pro_event(session, tournament, name, stand_type, gender=None, max_stands=4):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="pro",
        gender=gender,
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type=stand_type,
        max_stands=max_stands,
    )
    session.add(e)
    session.flush()
    return e


def _make_heat(session, event, heat_number, competitor_ids=None, run_number=1):
    from models import Heat

    h = Heat(event_id=event.id, heat_number=heat_number, run_number=run_number)
    h.set_competitors(competitor_ids or [])
    session.add(h)
    session.flush()
    return h


def _make_pro(session, tournament, name, gender="M"):
    from models import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id, name=name, gender=gender, status="active"
    )
    session.add(c)
    session.flush()
    return c


def _seed_with_chokerman_and_speed_climb(session):
    """3 flights + Speed Climb Run 2 (4 heats) + Chokerman Run 2 (4 heats)."""
    from services.flight_builder import build_pro_flights

    t = _make_tournament(session)
    pros = [_make_pro(session, t, f"Pro {i}") for i in range(1, 9)]
    ev_sb = _make_pro_event(session, t, "Springboard", "springboard")
    ev_uh = _make_pro_event(
        session, t, "Underhand", "underhand", gender="M", max_stands=5
    )
    ev_op = _make_pro_event(session, t, "Obstacle Pole", "obstacle_pole")
    for n in range(1, 4):
        _make_heat(session, ev_sb, n, [pros[n - 1].id])
        _make_heat(session, ev_uh, n, [pros[n].id, pros[n + 1].id])
        _make_heat(session, ev_op, n, [pros[n + 2].id])

    ev_chokerman_m = _make_college_event(
        session,
        t,
        "Chokerman's Race",
        "chokerman",
        gender="M",
        requires_dual_runs=True,
    )
    ev_speed_m = _make_college_event(
        session,
        t,
        "Speed Climb",
        "speed_climb",
        gender="M",
        requires_dual_runs=True,
    )
    for ev in (ev_chokerman_m, ev_speed_m):
        for n in range(1, 3):
            _make_heat(session, ev, n, [], run_number=1)
            _make_heat(session, ev, n, [], run_number=2)

    build_pro_flights(t, num_flights=3, commit=False)
    session.flush()
    return {"t": t, "chokerman_m": ev_chokerman_m, "speed_m": ev_speed_m}


class TestChokermanPlacement:
    def test_chokerman_run2_lands_in_last_flight_roundrobin(self, db_session):
        from models import Flight, Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed_with_chokerman_and_speed_climb(db_session)
        integrate_college_spillover_into_flights(
            data["t"],
            college_event_ids=[],
            placement_mode="roundrobin",
        )

        flights = (
            Flight.query.filter_by(tournament_id=data["t"].id)
            .order_by(Flight.flight_number)
            .all()
        )
        last_flight_id = flights[-1].id

        chokerman_run2 = Heat.query.filter_by(
            event_id=data["chokerman_m"].id,
            run_number=2,
        ).all()
        for h in chokerman_run2:
            assert h.flight_id == last_flight_id, (
                f"Chokerman Run 2 heat {h.heat_number} landed in flight "
                f"{h.flight_id}, expected last flight {last_flight_id}. "
                "FlightLogic.md §4.1 show-climax rule must hold."
            )

    def test_chokerman_run2_lands_in_last_flight_cluster(self, db_session):
        """Cluster mode must not pull Chokerman off its final-flight seat."""
        from models import Flight, Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed_with_chokerman_and_speed_climb(db_session)
        integrate_college_spillover_into_flights(
            data["t"],
            college_event_ids=[],
            placement_mode="cluster",
        )

        flights = (
            Flight.query.filter_by(tournament_id=data["t"].id)
            .order_by(Flight.flight_number)
            .all()
        )
        last_flight_id = flights[-1].id

        chokerman_run2 = Heat.query.filter_by(
            event_id=data["chokerman_m"].id,
            run_number=2,
        ).all()
        for h in chokerman_run2:
            assert h.flight_id == last_flight_id

    def test_chokerman_is_last_heat_in_last_flight(self, db_session):
        """Within the last flight, Chokerman's flight_position must be the largest."""
        from models import Flight, Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed_with_chokerman_and_speed_climb(db_session)
        integrate_college_spillover_into_flights(
            data["t"],
            college_event_ids=[],
            placement_mode="roundrobin",
        )

        flights = (
            Flight.query.filter_by(tournament_id=data["t"].id)
            .order_by(Flight.flight_number)
            .all()
        )
        last_flight = flights[-1]

        last_flight_heats = Heat.query.filter_by(flight_id=last_flight.id).all()
        chokerman_heats = [
            h for h in last_flight_heats if h.event_id == data["chokerman_m"].id
        ]
        non_chokerman_heats = [
            h for h in last_flight_heats if h.event_id != data["chokerman_m"].id
        ]
        if not chokerman_heats or not non_chokerman_heats:
            pytest.skip("fixture did not produce a mixed last flight")

        max_chokerman_pos = max(h.flight_position or 0 for h in chokerman_heats)
        max_non_chokerman_pos = max(h.flight_position or 0 for h in non_chokerman_heats)

        assert max_chokerman_pos > max_non_chokerman_pos, (
            f"Chokerman max flight_position ({max_chokerman_pos}) should be strictly "
            f"after all other last-flight heats ({max_non_chokerman_pos}). "
            "Show-closer rule broken."
        )
