"""
Phase 2: Speed Climb Run 2 placement respects the saturday_college_placement_mode
toggle.

- 'roundrobin' (default) — distribute across flights, respecting MIN_HEAT_SPACING.
- 'cluster' — greedy-fill flights from flight 1 forward until each hits the
  per-flight cap, then spill to last flight.

Run:  pytest tests/test_speed_climb_greedy_fill.py -v
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

    t = Tournament(name="Placement Mode Test", year=2026, status="pro_active")
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


def _seed(session):
    """3 flights × 2 pro heats each, plus Speed Climb M+F Run 2 (4 heats total)."""
    from services.flight_builder import build_pro_flights

    t = _make_tournament(session)
    pros = [_make_pro(session, t, f"Pro {i}") for i in range(1, 7)]
    ev_sb = _make_pro_event(session, t, "Springboard", "springboard")
    ev_uh = _make_pro_event(
        session, t, "Underhand", "underhand", gender="M", max_stands=5
    )
    ev_op_pro = _make_pro_event(session, t, "Obstacle Pole", "obstacle_pole")
    for n in range(1, 3):
        _make_heat(session, ev_sb, n, [pros[n - 1].id])
        _make_heat(session, ev_uh, n, [pros[n].id, pros[n + 1].id])
        _make_heat(session, ev_op_pro, n, [pros[n + 2].id])

    ev_speed_m = _make_college_event(
        session, t, "Speed Climb", "speed_climb", gender="M", requires_dual_runs=True
    )
    ev_speed_f = _make_college_event(
        session, t, "Speed Climb", "speed_climb", gender="F", requires_dual_runs=True
    )
    for ev in (ev_speed_m, ev_speed_f):
        for n in range(1, 3):
            _make_heat(session, ev, n, [], run_number=1)
            _make_heat(session, ev, n, [], run_number=2)

    build_pro_flights(t, num_flights=3, commit=False)
    session.flush()
    return {"t": t, "speed_m": ev_speed_m, "speed_f": ev_speed_f}


class TestRoundrobinDefault:
    def test_speed_climb_spreads_across_flights_roundrobin(self, db_session):
        """Default 'roundrobin' mode puts Speed Climb heats in multiple flights."""
        from models import Flight, Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed(db_session)
        integrate_college_spillover_into_flights(
            data["t"],
            college_event_ids=[],
            placement_mode="roundrobin",
        )

        speed_run2 = Heat.query.filter(
            Heat.event_id.in_([data["speed_m"].id, data["speed_f"].id]),
            Heat.run_number == 2,
        ).all()
        placed = [h for h in speed_run2 if h.flight_id is not None]
        assert len(placed) == len(speed_run2), "all Speed Climb Run 2 heats must land"

        # Round-robin: at least 2 distinct flights receive Speed Climb heats.
        flight_ids = {h.flight_id for h in placed}
        all_flights = Flight.query.filter_by(tournament_id=data["t"].id).all()
        assert len(all_flights) >= 2
        assert len(flight_ids) >= 2, (
            "roundrobin mode should distribute Speed Climb across flights, "
            f"got them all clustered in flights {flight_ids}"
        )


class TestClusterMode:
    def test_speed_climb_clusters_in_earliest_flights(self, db_session):
        """'cluster' mode fills flight 1 first, then flight 2, etc."""
        from models import Flight, Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed(db_session)
        integrate_college_spillover_into_flights(
            data["t"],
            college_event_ids=[],
            placement_mode="cluster",
        )

        speed_run2 = Heat.query.filter(
            Heat.event_id.in_([data["speed_m"].id, data["speed_f"].id]),
            Heat.run_number == 2,
        ).all()
        flights = (
            Flight.query.filter_by(tournament_id=data["t"].id)
            .order_by(
                Flight.flight_number,
            )
            .all()
        )
        assert len(flights) >= 2, "test needs at least 2 flights"

        first_flight_id = flights[0].id
        last_flight_id = flights[-1].id

        # Cluster should place Speed Climb heats starting in flight 1, not
        # the last flight. Some may spill if flight 1 is at cap but the FIRST
        # Speed Climb heat must land in an earlier flight than the last.
        placed_flight_ids = {h.flight_id for h in speed_run2}
        assert first_flight_id in placed_flight_ids or len(placed_flight_ids) == 1, (
            f"cluster mode should fill earlier flights first, got flights {placed_flight_ids}, "
            f"expected flight {first_flight_id} to be among them"
        )
        assert placed_flight_ids != {
            last_flight_id
        }, "cluster mode must not dump all Speed Climb heats into the last flight"


class TestPlacementModeFromConfig:
    def test_reads_mode_from_schedule_config_when_not_passed(self, db_session):
        """When placement_mode is not explicitly passed, read from schedule_config."""
        from models import Flight, Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed(db_session)

        # Persist 'cluster' in schedule_config — existing callers that don't
        # pass placement_mode should pick it up automatically.
        cfg = data["t"].get_schedule_config() or {}
        cfg["saturday_college_placement_mode"] = "cluster"
        data["t"].set_schedule_config(cfg)
        db_session.flush()

        integrate_college_spillover_into_flights(data["t"], college_event_ids=[])

        speed_run2 = Heat.query.filter(
            Heat.event_id.in_([data["speed_m"].id, data["speed_f"].id]),
            Heat.run_number == 2,
        ).all()
        flights = (
            Flight.query.filter_by(tournament_id=data["t"].id)
            .order_by(
                Flight.flight_number,
            )
            .all()
        )
        last_flight_id = flights[-1].id
        placed_flight_ids = {h.flight_id for h in speed_run2}
        assert placed_flight_ids != {last_flight_id}, (
            "cluster mode from schedule_config should not stack all Speed Climb "
            "heats in the last flight"
        )

    def test_invalid_mode_falls_back_to_roundrobin(self, db_session):
        """Unknown mode in schedule_config degrades gracefully."""
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed(db_session)
        cfg = data["t"].get_schedule_config() or {}
        cfg["saturday_college_placement_mode"] = "garbage-value"
        data["t"].set_schedule_config(cfg)
        db_session.flush()

        result = integrate_college_spillover_into_flights(
            data["t"], college_event_ids=[]
        )
        assert (
            result["integrated_heats"] > 0
        ), "unknown placement_mode should fall back to roundrobin, not abort"
