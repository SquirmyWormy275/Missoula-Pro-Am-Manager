"""
Phase 2: day-split events (Chokerman's Race, Speed Climb) are auto-added to the
mandatory spillover set — operators must NOT need to tick them explicitly.

Fixes Recon Issue 1: Speed Climb Run 2 wasn't landing on Saturday because
integrate_college_spillover_into_flights hard-coded Chokerman as the only
auto-mandatory event. Speed Climb Run 2 orphaned with flight_id=NULL.

Run:  pytest tests/test_day_split_mandatory_routing.py -v
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


def _make_tournament(session, name="Day Split Test 2026"):
    from models import Tournament

    t = Tournament(name=name, year=2026, status="pro_active")
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


def _make_pro_comp(session, tournament, name, gender="M"):
    from models import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id, name=name, gender=gender, status="active"
    )
    session.add(c)
    session.flush()
    return c


def _seed_with_speed_climb_and_chokerman(session):
    """Tournament with pro flights + all day-split events + Obstacle Pole.

    Speed Climb M/F and Chokerman M/F are NOT in the caller's event id list —
    they must be auto-added.
    """
    from services.flight_builder import build_pro_flights

    t = _make_tournament(session)

    # Pro scaffolding — enough for 2 flights
    pros = [_make_pro_comp(session, t, f"Pro {i}") for i in range(1, 9)]
    ev_sb = _make_pro_event(session, t, "Springboard", "springboard")
    ev_uh = _make_pro_event(
        session, t, "Underhand", "underhand", gender="M", max_stands=5
    )
    for n in range(1, 3):
        _make_heat(session, ev_sb, n, [pros[(n - 1)].id])
        _make_heat(session, ev_uh, n, [pros[n + 1].id, pros[n + 2].id])

    # Day-split events (both must be auto-included)
    ev_chokerman_m = _make_college_event(
        session, t, "Chokerman's Race", "chokerman", gender="M", requires_dual_runs=True
    )
    ev_speed_m = _make_college_event(
        session, t, "Speed Climb", "speed_climb", gender="M", requires_dual_runs=True
    )
    ev_speed_f = _make_college_event(
        session, t, "Speed Climb", "speed_climb", gender="F", requires_dual_runs=True
    )
    for ev in (ev_chokerman_m, ev_speed_m, ev_speed_f):
        for n in range(1, 3):
            _make_heat(session, ev, n, [], run_number=1)
            _make_heat(session, ev, n, [], run_number=2)

    # Non-day-split spillover — opted in via the explicit list so we can
    # confirm mandatory auto-add does not clobber explicit selections.
    ev_op = _make_college_event(
        session, t, "Obstacle Pole", "obstacle_pole", gender="M"
    )
    for n in range(1, 3):
        _make_heat(session, ev_op, n, [])

    build_pro_flights(t, num_flights=2, commit=False)
    session.flush()

    return {
        "t": t,
        "chokerman_m": ev_chokerman_m,
        "speed_m": ev_speed_m,
        "speed_f": ev_speed_f,
        "obstacle_pole": ev_op,
    }


# ---------------------------------------------------------------------------


class TestMandatoryAutoAdd:
    def test_speed_climb_men_auto_added_without_explicit_selection(self, db_session):
        """Speed Climb M Run 2 lands in a flight even when not in the caller's id list."""
        from models import Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed_with_speed_climb_and_chokerman(db_session)

        # Pass ONLY Obstacle Pole — Speed Climb and Chokerman must still be routed.
        result = integrate_college_spillover_into_flights(
            data["t"],
            college_event_ids=[data["obstacle_pole"].id],
        )
        assert result["integrated_heats"] > 0

        speed_m_run2 = Heat.query.filter_by(
            event_id=data["speed_m"].id,
            run_number=2,
        ).all()
        assert speed_m_run2, "fixture should include Speed Climb M Run 2 heats"
        for h in speed_m_run2:
            assert h.flight_id is not None, (
                f"Speed Climb M Run 2 heat {h.heat_number} orphaned — "
                "DAY_SPLIT_EVENT_NAMES auto-add regression."
            )

    def test_speed_climb_women_auto_added_without_explicit_selection(self, db_session):
        """Speed Climb F Run 2 lands in a flight even when not in the caller's id list."""
        from models import Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed_with_speed_climb_and_chokerman(db_session)
        integrate_college_spillover_into_flights(
            data["t"],
            college_event_ids=[data["obstacle_pole"].id],
        )

        speed_f_run2 = Heat.query.filter_by(
            event_id=data["speed_f"].id,
            run_number=2,
        ).all()
        for h in speed_f_run2:
            assert h.flight_id is not None

    def test_chokerman_still_auto_added(self, db_session):
        """Phase 2 change must preserve Chokerman's pre-existing auto-add behaviour."""
        from models import Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed_with_speed_climb_and_chokerman(db_session)
        integrate_college_spillover_into_flights(data["t"], college_event_ids=[])

        chokerman_run2 = Heat.query.filter_by(
            event_id=data["chokerman_m"].id,
            run_number=2,
        ).all()
        for h in chokerman_run2:
            assert h.flight_id is not None


class TestDaySplitRunFilter:
    def test_speed_climb_run_1_stays_on_friday(self, db_session):
        """Speed Climb Run 1 heats are NOT pulled into Saturday flights."""
        from models import Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed_with_speed_climb_and_chokerman(db_session)
        integrate_college_spillover_into_flights(data["t"], college_event_ids=[])

        speed_m_run1 = Heat.query.filter_by(
            event_id=data["speed_m"].id,
            run_number=1,
        ).all()
        for h in speed_m_run1:
            assert h.flight_id is None, (
                f"Speed Climb M Run 1 heat {h.heat_number} was pulled to Saturday "
                "(flight_id set) — day-split run_number filter regression."
            )

    def test_chokerman_run_1_stays_on_friday(self, db_session):
        """Chokerman Run 1 heats stay on Friday too — regression guard."""
        from models import Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed_with_speed_climb_and_chokerman(db_session)
        integrate_college_spillover_into_flights(data["t"], college_event_ids=[])

        chokerman_run1 = Heat.query.filter_by(
            event_id=data["chokerman_m"].id,
            run_number=1,
        ).all()
        for h in chokerman_run1:
            assert h.flight_id is None

    def test_obstacle_pole_still_pulls_all_runs(self, db_session):
        """Non-day-split events (Obstacle Pole is single-run) behave unchanged."""
        from models import Heat
        from services.flight_builder import integrate_college_spillover_into_flights

        data = _seed_with_speed_climb_and_chokerman(db_session)
        integrate_college_spillover_into_flights(
            data["t"],
            college_event_ids=[data["obstacle_pole"].id],
        )

        op_heats = Heat.query.filter_by(event_id=data["obstacle_pole"].id).all()
        for h in op_heats:
            assert h.flight_id is not None
