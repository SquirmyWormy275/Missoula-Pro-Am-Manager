"""
Integration tests for saw-block recompute hooks wired into mutation routes.

Covers:
  - generate_heats (single event) triggers block assignment
  - build_flights triggers block assignment post-flight
  - reorder_flight_heats triggers recompute reflecting new flight order
  - reorder_friday_events triggers recompute reflecting new event order
  - Hook failure does not break the primary mutation

Run:  pytest tests/test_saw_block_integration.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import pytest
from sqlalchemy.orm.exc import StaleDataError

from database import db

os.environ.setdefault("SECRET_KEY", "test-saw-block-integration")
os.environ.setdefault("WTF_CSRF_ENABLED", "False")


@pytest.fixture(scope="module")
def app():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()

    old_url = os.environ.get("DATABASE_URL")
    old_create_all = os.environ.get("TEST_USE_CREATE_ALL")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["TEST_USE_CREATE_ALL"] = "1"

    try:
        from app import create_app

        _app = create_app()
        _app.config.update(
            {
                "TESTING": True,
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
                "WTF_CSRF_ENABLED": False,
                "WTF_CSRF_CHECK_DEFAULT": False,
            }
        )

        from database import db as _db

        with _app.app_context():
            _db.create_all()
            yield _app
            _db.session.remove()
    finally:
        if old_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_url
        if old_create_all is None:
            os.environ.pop("TEST_USE_CREATE_ALL", None)
        else:
            os.environ["TEST_USE_CREATE_ALL"] = old_create_all
        try:
            os.unlink(db_path)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def clean_db(app):
    from database import db as _db

    with app.app_context():
        yield
        _db.session.remove()
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()
        # Clear heat_generator's module-level tournament-events cache to prevent
        # detached-instance leaks across test modules (pre-existing cache bug
        # in services/heat_generator.py: `_get_tournament_events._cache`).
        try:
            from services.heat_generator import _get_tournament_events
            if hasattr(_get_tournament_events, '_cache'):
                _get_tournament_events._cache.clear()
        except Exception:
            pass


@pytest.fixture()
def auth_client(app):
    """Return a test client authenticated as an admin user."""
    from database import db as _db
    from models.user import User

    with app.app_context():
        u = User(username="sawblock_admin", role="admin")
        u.set_password("pass")
        _db.session.add(u)
        _db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return c


# ---------------------------------------------------------------------------
# Seed helpers — create realistic saw-event tournaments
# ---------------------------------------------------------------------------


def _seed_tournament(db):
    from models import Tournament

    t = Tournament(name="SawBlock Integration Test 2026", year=2026, status="setup")
    db.session.add(t)
    db.session.flush()
    return t


def _seed_team(db, tournament):
    from models import Team

    team = Team(
        tournament_id=tournament.id,
        team_code="UM-A",
        school_name="University of Montana",
        school_abbreviation="UM",
    )
    db.session.add(team)
    db.session.flush()
    return team


def _seed_college_competitors(
    db, tournament, team, count=12, gender="M", event_name="Single Buck"
):
    from models.competitor import CollegeCompetitor

    comps = []
    for i in range(count):
        c = CollegeCompetitor(
            tournament_id=tournament.id,
            team_id=team.id,
            name=f"Competitor {i + 1}",
            gender=gender,
            events_entered=json.dumps([event_name]),
            status="active",
        )
        db.session.add(c)
        comps.append(c)
    db.session.flush()
    return comps


def _seed_saw_event(
    db,
    tournament,
    name="Single Buck",
    event_type="college",
    gender="M",
    is_partnered=False,
):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender=gender,
        scoring_type="time",
        stand_type="saw_hand",
        is_partnered=is_partnered,
    )
    db.session.add(e)
    db.session.flush()
    return e


def _seed_stand_event(db, tournament, name, stand_type, event_type="pro"):
    from models import Event

    event = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender="M",
        scoring_type="time",
        stand_type=stand_type,
    )
    db.session.add(event)
    db.session.flush()
    return event


def _seed_event_results(db, event, competitors, comp_type="college"):
    from models.event import EventResult

    for c in competitors:
        r = EventResult(
            event_id=event.id,
            competitor_id=c.id,
            competitor_type=comp_type,
            competitor_name=c.name,
            status="pending",
        )
        db.session.add(r)
    db.session.flush()


def _seed_heat(
    db,
    event,
    heat_number,
    competitors,
    stand_assignments,
    run_number=1,
    flight=None,
    flight_position=None,
):
    # Materialise the competitors this heat names. These fixtures invent ids,
    # which was harmless until s8a0b2c3d4e5 gave heat_assignments a NOT NULL uid
    # with a foreign key onto the identity spine. `set_roster` raises
    # BadHeatAssignment for an id with no competitor row behind it, so the
    # heat has to name somebody who exists.
    from models.tournament import Tournament
    from tests.conftest import ensure_competitors

    ensure_competitors(
        db.session, db.session.get(Tournament, event.tournament_id),
        competitors, event.event_type,
    )

    from models import Heat

    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
    )
    if flight is not None:
        h.flight_id = flight.id
        h.flight_position = flight_position
    db.session.add(h)
    db.session.flush()
    h.set_roster(event.event_type, competitors, stand_assignments)
    db.session.flush()
    return h


def _seed_flight(db, tournament, flight_number):
    from models.heat import Flight

    f = Flight(tournament_id=tournament.id, flight_number=flight_number)
    db.session.add(f)
    db.session.flush()
    return f


def _used_stands(heat):
    return sorted({int(v) for v in heat.get_stand_assignments().values()})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_generate_heats_triggers_block_assignment(app, auth_client):
    """POST generate-heats should trigger saw-block recompute."""
    from database import db as _db
    from services.saw_block_assignment import BLOCK_A, BLOCK_B

    with app.app_context():
        t = _seed_tournament(_db)
        team = _seed_team(_db, t)
        comps = _seed_college_competitors(
            _db, t, team, count=12, gender="M", event_name="Single Buck"
        )
        sb = _seed_saw_event(_db, t, name="Single Buck", event_type="college")
        _seed_event_results(_db, sb, comps, comp_type="college")

        # Tie competitors to the event by ID — generate_event_heats uses
        # competitor_entered_event which tries both name and ID.
        for c in comps:
            c.events_entered = json.dumps([str(sb.id)])
        _db.session.commit()

        tid = t.id
        eid = sb.id

    resp = auth_client.post(
        f"/scheduling/{tid}/event/{eid}/generate-heats",
        data={},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        from models import Heat

        heats = (
            Heat.query.filter_by(event_id=eid, run_number=1)
            .order_by(Heat.heat_number)
            .all()
        )
        assert len(heats) >= 2
        # First heat on Block A, second on Block B
        assert _used_stands(heats[0]) == BLOCK_A
        assert _used_stands(heats[1]) == BLOCK_B


def test_build_flights_triggers_block_assignment(app, auth_client):
    """After build_flights reshuffles heats, blocks reflect new flight order."""
    from database import db as _db
    from services.saw_block_assignment import BLOCK_A, BLOCK_B

    with app.app_context():
        t = _seed_tournament(_db)
        sb = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        # Seed 2 pro saw heats with stand_assignments already on Block A
        h1 = _seed_heat(
            _db,
            sb,
            1,
            competitors=[1, 2, 3, 4],
            stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
        )
        h2 = _seed_heat(
            _db,
            sb,
            2,
            competitors=[5, 6, 7, 8],
            stand_assignments={"5": 1, "6": 2, "7": 3, "8": 4},
        )
        _db.session.commit()
        tid = t.id
        h1_id = h1.id
        h2_id = h2.id

    resp = auth_client.post(
        f"/scheduling/{tid}/flights/build",
        data={"num_flights": "1"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        from models import Heat

        h1 = db.session.get(Heat, h1_id)
        h2 = db.session.get(Heat, h2_id)
        # After build_flights, both heats are in flight 1.
        # The first heat in flight_position order gets Block A, the second Block B.
        ordered_by_flight = sorted(
            [h1, h2], key=lambda h: (h.flight_position or 999, h.id)
        )
        assert _used_stands(ordered_by_flight[0]) == BLOCK_A
        assert _used_stands(ordered_by_flight[1]) == BLOCK_B


def test_build_flights_refuses_to_rewrite_completed_heat_history(app, auth_client):
    """The build route preserves published flight placements after scoring."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        sb = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        h1 = _seed_heat(
            _db, sb, 1, competitors=[1, 2],
            stand_assignments={"1": 1, "2": 2},
        )
        h2 = _seed_heat(
            _db, sb, 2, competitors=[3, 4],
            stand_assignments={"3": 1, "4": 2},
        )
        _db.session.commit()
        tid, h1_id, h2_id = t.id, h1.id, h2.id

    first = auth_client.post(
        f"/scheduling/{tid}/flights/build",
        data={"num_flights": "1"},
        follow_redirects=False,
    )
    assert first.status_code in (302, 303)

    with app.app_context():
        from models import Heat

        _db.session.expire_all()
        completed = _db.session.get(Heat, h1_id)
        completed.status = "completed"
        _db.session.commit()
        before = [
            (heat.id, heat.flight_id, heat.flight_position)
            for heat in (_db.session.get(Heat, h1_id), _db.session.get(Heat, h2_id))
        ]

    second = auth_client.post(
        f"/scheduling/{tid}/flights/build",
        data={"num_flights": "1"},
        follow_redirects=True,
    )
    assert second.status_code == 200
    assert b"cannot be rebuilt after scoring begins" in second.data.lower()

    with app.app_context():
        from models import Heat

        after = [
            (heat.id, heat.flight_id, heat.flight_position)
            for heat in (_db.session.get(Heat, h1_id), _db.session.get(Heat, h2_id))
        ]
    assert after == before


def test_reorder_flight_heats_triggers_recompute(app, auth_client):
    """Reordering heats within a flight recomputes blocks per new run order."""
    from database import db as _db
    from services.saw_block_assignment import BLOCK_A, BLOCK_B

    with app.app_context():
        t = _seed_tournament(_db)
        sb = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        dbuck = _seed_saw_event(_db, t, name="Double Buck", event_type="pro")
        flight = _seed_flight(_db, t, flight_number=1)
        h_a = _seed_heat(
            _db,
            sb,
            1,
            competitors=[1, 2, 3, 4],
            stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
            flight=flight,
            flight_position=1,
        )
        h_b = _seed_heat(
            _db,
            dbuck,
            1,
            competitors=[5, 6, 7, 8],
            stand_assignments={"5": 5, "6": 6, "7": 7, "8": 8},
            flight=flight,
            flight_position=2,
        )
        _db.session.commit()
        tid = t.id
        fid = flight.id
        h_a_id = h_a.id
        h_b_id = h_b.id

    # Reverse two different events. Event-local heat sequencing stays valid.
    resp = auth_client.post(
        f"/scheduling/{tid}/flights/{fid}/reorder",
        json={"heat_ids": [h_b_id, h_a_id]},
    )
    assert resp.status_code == 200

    with app.app_context():
        from models import Heat

        h_a = db.session.get(Heat, h_a_id)
        h_b = db.session.get(Heat, h_b_id)
        # h_b is now first in flight -> Block A; h_a is second -> Block B
        assert _used_stands(h_b) == BLOCK_A
        assert _used_stands(h_a) == BLOCK_B


def test_reorder_flight_heats_rejects_out_of_order_event(app, auth_client):
    """Manual ordering must preserve Heat 1, Heat 2, ... within each event."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        event = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        flight = _seed_flight(_db, t, flight_number=1)
        first = _seed_heat(
            _db, event, 1, competitors=[1], stand_assignments={"1": 1},
            flight=flight, flight_position=1,
        )
        second = _seed_heat(
            _db, event, 2, competitors=[2], stand_assignments={"2": 1},
            flight=flight, flight_position=2,
        )
        _db.session.commit()
        tid, fid, first_id, second_id = t.id, flight.id, first.id, second.id

    resp = auth_client.post(
        f"/scheduling/{tid}/flights/{fid}/reorder",
        json={"heat_ids": [second_id, first_id]},
    )

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "event_sequence"
    with app.app_context():
        from models import Heat

        assert _db.session.get(Heat, first_id).flight_position == 1
        assert _db.session.get(Heat, second_id).flight_position == 2


def test_reorder_flight_heats_rejects_completed_heat_move(app, auth_client):
    """A scored heat cannot be shifted within its published flight."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        first_event = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        second_event = _seed_saw_event(_db, t, name="Double Buck", event_type="pro")
        flight = _seed_flight(_db, t, flight_number=1)
        completed = _seed_heat(
            _db, first_event, 1, competitors=[1], stand_assignments={"1": 1},
            flight=flight, flight_position=1,
        )
        pending = _seed_heat(
            _db, second_event, 1, competitors=[2], stand_assignments={"2": 1},
            flight=flight, flight_position=2,
        )
        completed.status = "completed"
        _db.session.commit()
        tid, fid, completed_id, pending_id = t.id, flight.id, completed.id, pending.id

    resp = auth_client.post(
        f"/scheduling/{tid}/flights/{fid}/reorder",
        json={"heat_ids": [pending_id, completed_id]},
    )

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "completed_heat"
    with app.app_context():
        from models import Heat

        assert _db.session.get(Heat, completed_id).flight_position == 1
        assert _db.session.get(Heat, pending_id).flight_position == 2


def test_reorder_flight_heats_rejects_duplicate_heat_id(app, auth_client):
    """A duplicate heat ID cannot create a gap in flight positions."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        first_event = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        second_event = _seed_saw_event(_db, t, name="Double Buck", event_type="pro")
        flight = _seed_flight(_db, t, flight_number=1)
        first = _seed_heat(
            _db, first_event, 1, competitors=[1], stand_assignments={"1": 1},
            flight=flight, flight_position=1,
        )
        second = _seed_heat(
            _db, second_event, 1, competitors=[2], stand_assignments={"2": 1},
            flight=flight, flight_position=2,
        )
        _db.session.commit()
        tid, fid, first_id, second_id = t.id, flight.id, first.id, second.id

    resp = auth_client.post(
        f"/scheduling/{tid}/flights/{fid}/reorder",
        json={"heat_ids": [first_id, second_id, second_id]},
    )

    assert resp.status_code == 400
    with app.app_context():
        from models import Heat

        assert _db.session.get(Heat, first_id).flight_position == 1
        assert _db.session.get(Heat, second_id).flight_position == 2


def test_bulk_reorder_moves_heat_between_flights(app, auth_client):
    """Bulk reorder endpoint moves a heat from one flight to another,
    updates flight_id and flight_position correctly for every heat in the
    payload."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        sb = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        f1 = _seed_flight(_db, t, flight_number=1)
        f2 = _seed_flight(_db, t, flight_number=2)
        h_a = _seed_heat(_db, sb, 1, competitors=[1, 2, 3, 4],
                        stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
                        flight=f1, flight_position=1)
        h_b = _seed_heat(_db, sb, 2, competitors=[5, 6, 7, 8],
                        stand_assignments={"5": 5, "6": 6, "7": 7, "8": 8},
                        flight=f1, flight_position=2)
        h_c = _seed_heat(_db, sb, 3, competitors=[9, 10, 11, 12],
                        stand_assignments={"9": 1, "10": 2, "11": 3, "12": 4},
                        flight=f2, flight_position=1)
        _db.session.commit()
        tid, f1_id, f2_id = t.id, f1.id, f2.id
        h_a_id, h_b_id, h_c_id = h_a.id, h_b.id, h_c.id

    # Move h_b from flight 1 to flight 2, keep h_a alone in flight 1,
    # put h_b at position 1 of flight 2 (before h_c).
    resp = auth_client.post(
        f"/scheduling/{tid}/flights/bulk-reorder",
        json={
            "flights": [
                {"flight_id": f1_id, "heat_ids": [h_a_id]},
                {"flight_id": f2_id, "heat_ids": [h_b_id, h_c_id]},
            ]
        },
    )
    assert resp.status_code == 200, resp.data
    assert resp.get_json().get("ok") is True

    with app.app_context():
        from models import Heat
        h_a = db.session.get(Heat, h_a_id)
        h_b = db.session.get(Heat, h_b_id)
        h_c = db.session.get(Heat, h_c_id)
        assert h_a.flight_id == f1_id and h_a.flight_position == 1
        assert h_b.flight_id == f2_id and h_b.flight_position == 1
        assert h_c.flight_id == f2_id and h_c.flight_position == 2


def test_bulk_reorder_rejects_out_of_order_event(app, auth_client):
    """Cross-flight drag cannot put Heat 2 in front of Heat 1."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        event = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        first_flight = _seed_flight(_db, t, flight_number=1)
        second_flight = _seed_flight(_db, t, flight_number=2)
        first = _seed_heat(
            _db, event, 1, competitors=[1], stand_assignments={"1": 1},
            flight=first_flight, flight_position=1,
        )
        second = _seed_heat(
            _db, event, 2, competitors=[2], stand_assignments={"2": 1},
            flight=second_flight, flight_position=1,
        )
        _db.session.commit()
        tid = t.id
        first_flight_id, second_flight_id = first_flight.id, second_flight.id
        first_id, second_id = first.id, second.id

    resp = auth_client.post(
        f"/scheduling/{tid}/flights/bulk-reorder",
        json={
            "flights": [
                {"flight_id": first_flight_id, "heat_ids": [second_id]},
                {"flight_id": second_flight_id, "heat_ids": [first_id]},
            ]
        },
    )

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "event_sequence"
    with app.app_context():
        from models import Heat

        assert _db.session.get(Heat, first_id).flight_id == first_flight_id
        assert _db.session.get(Heat, second_id).flight_id == second_flight_id


def test_bulk_reorder_rejects_completed_heat_move(app, auth_client):
    """A full-flight snapshot cannot relocate a scored heat."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        first_event = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        second_event = _seed_saw_event(_db, t, name="Double Buck", event_type="pro")
        first_flight = _seed_flight(_db, t, flight_number=1)
        second_flight = _seed_flight(_db, t, flight_number=2)
        completed = _seed_heat(
            _db, first_event, 1, competitors=[1], stand_assignments={"1": 1},
            flight=first_flight, flight_position=1,
        )
        pending = _seed_heat(
            _db, second_event, 1, competitors=[2], stand_assignments={"2": 1},
            flight=second_flight, flight_position=1,
        )
        completed.status = "completed"
        _db.session.commit()
        tid = t.id
        first_flight_id, second_flight_id = first_flight.id, second_flight.id
        completed_id, pending_id = completed.id, pending.id

    resp = auth_client.post(
        f"/scheduling/{tid}/flights/bulk-reorder",
        json={
            "flights": [
                {"flight_id": first_flight_id, "heat_ids": [pending_id]},
                {"flight_id": second_flight_id, "heat_ids": [completed_id]},
            ]
        },
    )

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "completed_heat"
    with app.app_context():
        from models import Heat

        assert _db.session.get(Heat, completed_id).flight_id == first_flight_id
        assert _db.session.get(Heat, pending_id).flight_id == second_flight_id


def test_bulk_reorder_rejects_duplicate_heat_id(app, auth_client):
    """A heat may occur in one bulk-reorder destination exactly once."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        first_event = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        second_event = _seed_saw_event(_db, t, name="Double Buck", event_type="pro")
        first_flight = _seed_flight(_db, t, flight_number=1)
        second_flight = _seed_flight(_db, t, flight_number=2)
        first = _seed_heat(
            _db, first_event, 1, competitors=[1], stand_assignments={"1": 1},
            flight=first_flight, flight_position=1,
        )
        second = _seed_heat(
            _db, second_event, 1, competitors=[2], stand_assignments={"2": 1},
            flight=second_flight, flight_position=1,
        )
        _db.session.commit()
        tid = t.id
        first_flight_id, second_flight_id = first_flight.id, second_flight.id
        first_id, second_id = first.id, second.id

    resp = auth_client.post(
        f"/scheduling/{tid}/flights/bulk-reorder",
        json={
            "flights": [
                {"flight_id": first_flight_id, "heat_ids": [first_id, second_id]},
                {"flight_id": second_flight_id, "heat_ids": [second_id]},
            ]
        },
    )

    assert resp.status_code == 400
    with app.app_context():
        from models import Heat

        assert _db.session.get(Heat, first_id).flight_id == first_flight_id
        assert _db.session.get(Heat, second_id).flight_id == second_flight_id


def test_bulk_reorder_rejects_mismatched_heat_set(app, auth_client):
    """Bulk reorder must refuse a payload that drops or invents heats so a
    half-loaded DOM can't wipe state."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        sb = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        f1 = _seed_flight(_db, t, flight_number=1)
        h_a = _seed_heat(_db, sb, 1, competitors=[1, 2],
                        stand_assignments={"1": 1, "2": 2},
                        flight=f1, flight_position=1)
        h_b = _seed_heat(_db, sb, 2, competitors=[3, 4],
                        stand_assignments={"3": 1, "4": 2},
                        flight=f1, flight_position=2)
        _db.session.commit()
        tid, f1_id = t.id, f1.id
        h_a_id = h_a.id  # intentionally omit h_b from the payload

    resp = auth_client.post(
        f"/scheduling/{tid}/flights/bulk-reorder",
        json={"flights": [{"flight_id": f1_id, "heat_ids": [h_a_id]}]},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body and body.get("ok") is False


def test_reorder_rejects_new_shared_stand_conflict(app, auth_client):
    """A within-flight drag cannot break the builder's eight-heat field gap."""
    from database import db as _db

    with app.app_context():
        tournament = _seed_tournament(_db)
        flight = _seed_flight(_db, tournament, flight_number=1)
        cookie = _seed_stand_event(_db, tournament, "Cookie Stack", "cookie_stack")
        standing = _seed_stand_event(_db, tournament, "Standing Block", "standing_block")
        neutral = _seed_stand_event(_db, tournament, "Underhand", "underhand")
        cookie_heat = _seed_heat(
            _db, cookie, 1, [1], {"1": 1}, flight=flight, flight_position=1,
        )
        neutral_heats = [
            _seed_heat(
                _db, neutral, number, [number + 1], {str(number + 1): 1},
                flight=flight, flight_position=number + 1,
            )
            for number in range(1, 9)
        ]
        standing_heat = _seed_heat(
            _db, standing, 1, [10], {"10": 1}, flight=flight, flight_position=10,
        )
        _db.session.commit()
        tournament_id, flight_id = tournament.id, flight.id
        cookie_heat_id = cookie_heat.id
        neutral_heat_ids = [heat.id for heat in neutral_heats]
        standing_heat_id = standing_heat.id
        original_ids = [cookie_heat_id, *neutral_heat_ids, standing_heat_id]

    response = auth_client.post(
        f"/scheduling/{tournament_id}/flights/{flight_id}/reorder",
        json={"heat_ids": [cookie_heat_id, standing_heat_id, *neutral_heat_ids]},
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "stand_conflict"
    with app.app_context():
        from models import Heat

        actual_ids = [
            heat.id for heat in Heat.query.filter_by(flight_id=flight_id)
            .order_by(Heat.flight_position).all()
        ]
        assert actual_ids == original_ids


def test_bulk_reorder_rejects_new_shared_stand_conflict(app, auth_client):
    """A full flight-board snapshot gets the same shared-field protection."""
    from database import db as _db

    with app.app_context():
        tournament = _seed_tournament(_db)
        flight = _seed_flight(_db, tournament, flight_number=1)
        cookie = _seed_stand_event(_db, tournament, "Cookie Stack", "cookie_stack")
        standing = _seed_stand_event(_db, tournament, "Standing Block", "standing_block")
        neutral = _seed_stand_event(_db, tournament, "Underhand", "underhand")
        cookie_heat = _seed_heat(
            _db, cookie, 1, [1], {"1": 1}, flight=flight, flight_position=1,
        )
        neutral_heats = [
            _seed_heat(
                _db, neutral, number, [number + 1], {str(number + 1): 1},
                flight=flight, flight_position=number + 1,
            )
            for number in range(1, 9)
        ]
        standing_heat = _seed_heat(
            _db, standing, 1, [10], {"10": 1}, flight=flight, flight_position=10,
        )
        _db.session.commit()
        tournament_id, flight_id = tournament.id, flight.id
        cookie_heat_id = cookie_heat.id
        neutral_heat_ids = [heat.id for heat in neutral_heats]
        standing_heat_id = standing_heat.id
        original_ids = [cookie_heat_id, *neutral_heat_ids, standing_heat_id]

    response = auth_client.post(
        f"/scheduling/{tournament_id}/flights/bulk-reorder",
        json={
            "flights": [{
                "flight_id": flight_id,
                "heat_ids": [
                    cookie_heat_id,
                    standing_heat_id,
                    *neutral_heat_ids,
                ],
            }],
        },
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "stand_conflict"
    with app.app_context():
        from models import Heat

        actual_ids = [
            heat.id for heat in Heat.query.filter_by(flight_id=flight_id)
            .order_by(Heat.flight_position).all()
        ]
        assert actual_ids == original_ids


def test_reorder_keeps_preexisting_unavoidable_shared_stand_conflict(app, auth_client):
    """The guard must not reject the builder's no-alternative fallback case."""
    from database import db as _db

    with app.app_context():
        tournament = _seed_tournament(_db)
        flight = _seed_flight(_db, tournament, flight_number=1)
        cookie = _seed_stand_event(_db, tournament, "Cookie Stack", "cookie_stack")
        standing = _seed_stand_event(_db, tournament, "Standing Block", "standing_block")
        cookie_heat = _seed_heat(
            _db, cookie, 1, [1], {"1": 1}, flight=flight, flight_position=1,
        )
        standing_heat = _seed_heat(
            _db, standing, 1, [2], {"2": 1}, flight=flight, flight_position=2,
        )
        _db.session.commit()
        tournament_id, flight_id = tournament.id, flight.id
        cookie_heat_id, standing_heat_id = cookie_heat.id, standing_heat.id

    response = auth_client.post(
        f"/scheduling/{tournament_id}/flights/{flight_id}/reorder",
        json={"heat_ids": [standing_heat_id, cookie_heat_id]},
    )

    assert response.status_code == 200, response.data


def test_move_competitor_happy_path(app, auth_client):
    """Drag-drop a competitor from one heat to another heat of the same event.

    Uses a non-saw event so saw_block_assignment's post-move reshuffle doesn't
    mask the assignment assertion.
    """
    from database import db as _db
    from models import Event

    with app.app_context():
        t = _seed_tournament(_db)
        ev = Event(tournament_id=t.id, name="Underhand", event_type="pro",
                   gender="M", scoring_type="time", stand_type="underhand",
                   max_stands=5)
        _db.session.add(ev); _db.session.flush()
        h_a = _seed_heat(_db, ev, 1, competitors=[1, 2, 3, 4],
                        stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4})
        h_b = _seed_heat(_db, ev, 2, competitors=[5, 6],
                        stand_assignments={"5": 1, "6": 2})
        _db.session.commit()
        tid, h_a_id, h_b_id = t.id, h_a.id, h_b.id

    resp = auth_client.post(
        f"/scheduling/{tid}/heats/{h_a_id}/drag-move",
        json={"competitor_ids": [2], "target_heat_id": h_b_id},
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["ok"] is True

    with app.app_context():
        from models import Heat
        h_a = db.session.get(Heat, h_a_id)
        h_b = db.session.get(Heat, h_b_id)
        assert 2 not in h_a.get_competitors()
        assert 2 in h_b.get_competitors()
        # Target heat had stands 1 and 2 used; competitor 2 gets next free = 3
        assert h_b.get_stand_assignments().get("2") == 3


def test_move_competitor_rejects_completed_source_or_target(app, auth_client):
    """Flight-board drag cannot rewrite a completed heat roster."""
    from database import db as _db
    from models import Event

    with app.app_context():
        t = _seed_tournament(_db)
        ev = Event(tournament_id=t.id, name="Underhand", event_type="pro",
                   gender="M", scoring_type="time", stand_type="underhand",
                   max_stands=5)
        _db.session.add(ev); _db.session.flush()
        source = _seed_heat(_db, ev, 1, competitors=[1], stand_assignments={"1": 1})
        target = _seed_heat(_db, ev, 2, competitors=[], stand_assignments={})
        target.status = "completed"
        _db.session.commit()
        tid, source_id, target_id = t.id, source.id, target.id

    resp = auth_client.post(
        f"/scheduling/{tid}/heats/{source_id}/drag-move",
        json={"competitor_ids": [1], "target_heat_id": target_id},
    )

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "completed_heat"
    with app.app_context():
        from models import Heat
        assert 1 in _db.session.get(Heat, source_id).get_competitors()
        assert 1 not in _db.session.get(Heat, target_id).get_competitors()


def test_move_competitor_rejects_dual_run_event(app, auth_client):
    """A single-run drag request cannot desynchronize the matching second run."""
    from database import db as _db
    from models import Event

    with app.app_context():
        t = _seed_tournament(_db)
        ev = Event(tournament_id=t.id, name="Chokerman", event_type="pro",
                   gender="M", scoring_type="time", stand_type="chokerman",
                   max_stands=5, requires_dual_runs=True)
        _db.session.add(ev); _db.session.flush()
        source = _seed_heat(_db, ev, 1, competitors=[1], stand_assignments={"1": 1})
        target = _seed_heat(_db, ev, 2, competitors=[], stand_assignments={})
        _db.session.commit()
        tid, source_id, target_id = t.id, source.id, target.id

    resp = auth_client.post(
        f"/scheduling/{tid}/heats/{source_id}/drag-move",
        json={"competitor_ids": [1], "target_heat_id": target_id},
    )

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "dual_run_event"


def test_move_competitor_rejects_full_target(app, auth_client):
    """Moving into a full heat returns 409 with target_full code."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        ev = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        ev.max_stands = 4
        h_a = _seed_heat(_db, ev, 1, competitors=[1, 2],
                        stand_assignments={"1": 1, "2": 2})
        h_b = _seed_heat(_db, ev, 2, competitors=[5, 6, 7, 8],
                        stand_assignments={"5": 1, "6": 2, "7": 3, "8": 4})
        _db.session.commit()
        tid, h_a_id, h_b_id = t.id, h_a.id, h_b.id

    resp = auth_client.post(
        f"/scheduling/{tid}/heats/{h_a_id}/drag-move",
        json={"competitor_ids": [1], "target_heat_id": h_b_id},
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert body.get("code") == "target_full"

    # State unchanged
    with app.app_context():
        from models import Heat
        h_a = db.session.get(Heat, h_a_id)
        h_b = db.session.get(Heat, h_b_id)
        assert 1 in h_a.get_competitors()
        assert 1 not in h_b.get_competitors()


def test_move_competitor_rejects_shared_gear_target(app, auth_client):
    """Drag moves cannot create the same shared-gear conflict as manual edits."""
    from database import db as _db
    from models.competitor import ProCompetitor

    with app.app_context():
        t = _seed_tournament(_db)
        ev = _seed_saw_event(_db, t, name="Underhand", event_type="pro")
        h_a = _seed_heat(_db, ev, 1, competitors=[1], stand_assignments={"1": 1})
        h_b = _seed_heat(_db, ev, 2, competitors=[5], stand_assignments={"5": 1})
        mover = _db.session.get(ProCompetitor, 1)
        resident = _db.session.get(ProCompetitor, 5)
        mover.gear_sharing = json.dumps({str(ev.id): resident.name})
        resident.gear_sharing = json.dumps({str(ev.id): mover.name})
        resident_name = resident.name
        _db.session.commit()
        tid, h_a_id, h_b_id = t.id, h_a.id, h_b.id

    resp = auth_client.post(
        f"/scheduling/{tid}/heats/{h_a_id}/drag-move",
        json={"competitor_ids": [1], "target_heat_id": h_b_id},
    )

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert body["code"] == "gear_conflict"
    assert resident_name in body["error"]

    with app.app_context():
        from models import Heat
        assert 1 in _db.session.get(Heat, h_a_id).get_competitors()
        assert 1 not in _db.session.get(Heat, h_b_id).get_competitors()


def test_move_competitor_reports_stale_heat_conflict(app, auth_client):
    """A concurrent heat update is a retryable conflict, not a server error."""
    from database import db as _db
    from models import Event

    with app.app_context():
        t = _seed_tournament(_db)
        ev = Event(tournament_id=t.id, name="Underhand", event_type="pro",
                   gender="M", scoring_type="time", stand_type="underhand",
                   max_stands=5)
        _db.session.add(ev); _db.session.flush()
        source = _seed_heat(_db, ev, 1, competitors=[1], stand_assignments={"1": 1})
        target = _seed_heat(_db, ev, 2, competitors=[], stand_assignments={})
        _db.session.commit()
        tid, source_id, target_id = t.id, source.id, target.id

    with patch('routes.scheduling.flights.db.session.commit', side_effect=StaleDataError()):
        resp = auth_client.post(
            f"/scheduling/{tid}/heats/{source_id}/drag-move",
            json={"competitor_ids": [1], "target_heat_id": target_id},
        )

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert body["code"] == "stale_heat"
    assert "refresh" in body["error"].lower()


def test_move_competitor_rejects_cross_event(app, auth_client):
    """Moving a competitor into a heat of a DIFFERENT event is refused."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        ev1 = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        ev1.max_stands = 4
        ev2 = _seed_saw_event(_db, t, name="Double Buck", event_type="pro")
        ev2.max_stands = 4
        h_a = _seed_heat(_db, ev1, 1, competitors=[1, 2],
                        stand_assignments={"1": 1, "2": 2})
        h_b = _seed_heat(_db, ev2, 1, competitors=[5, 6],
                        stand_assignments={"5": 1, "6": 2})
        _db.session.commit()
        tid, h_a_id, h_b_id = t.id, h_a.id, h_b.id

    resp = auth_client.post(
        f"/scheduling/{tid}/heats/{h_a_id}/drag-move",
        json={"competitor_ids": [1], "target_heat_id": h_b_id},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "same event" in body["error"].lower()


def test_move_competitor_pair_moves_both(app, auth_client):
    """Partnered events: when two competitor_ids are supplied, both move atomically."""
    from database import db as _db

    with app.app_context():
        from models.competitor import ProCompetitor

        t = _seed_tournament(_db)
        ev = _seed_saw_event(_db, t, name="Jack & Jill", event_type="pro",
                             is_partnered=True)
        ev.max_stands = 4
        h_a = _seed_heat(_db, ev, 1, competitors=[10, 11, 12, 13],
                        stand_assignments={"10": 1, "11": 1, "12": 2, "13": 2})
        h_b = _seed_heat(_db, ev, 2, competitors=[],
                        stand_assignments={})
        first = _db.session.get(ProCompetitor, 10)
        second = _db.session.get(ProCompetitor, 11)
        first.partners = json.dumps({str(ev.id): second.name})
        second.partners = json.dumps({str(ev.id): first.name})
        _db.session.commit()
        tid, h_a_id, h_b_id = t.id, h_a.id, h_b.id

    # Move the pair [10, 11] as a unit
    resp = auth_client.post(
        f"/scheduling/{tid}/heats/{h_a_id}/drag-move",
        json={"competitor_ids": [10, 11], "target_heat_id": h_b_id},
    )
    assert resp.status_code == 200, resp.data

    with app.app_context():
        from models import Heat
        h_a = db.session.get(Heat, h_a_id)
        h_b = db.session.get(Heat, h_b_id)
        assert 10 not in h_a.get_competitors()
        assert 11 not in h_a.get_competitors()
        assert 10 in h_b.get_competitors()
        assert 11 in h_b.get_competitors()
        assignments = h_b.get_stand_assignments()
        assert assignments["10"] == assignments["11"]


def test_move_competitor_rejects_partial_partnered_pair(app, auth_client):
    """Partnered drag moves must include the complete reciprocal pair."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        ev = _seed_saw_event(_db, t, name="Double Buck", event_type="pro", is_partnered=True)
        h_a = _seed_heat(_db, ev, 1, competitors=[10, 11],
                         stand_assignments={"10": 1, "11": 1})
        h_b = _seed_heat(_db, ev, 2, competitors=[], stand_assignments={})
        _db.session.commit()
        tid, h_a_id, h_b_id = t.id, h_a.id, h_b.id

    resp = auth_client.post(
        f"/scheduling/{tid}/heats/{h_a_id}/drag-move",
        json={"competitor_ids": [10], "target_heat_id": h_b_id},
    )

    assert resp.status_code == 400
    assert "exactly one confirmed pair" in resp.get_json()["error"].lower()


def test_reorder_friday_events_triggers_recompute(app, auth_client):
    """Reordering Friday events reassigns blocks to match new event order."""
    from database import db as _db
    from services.saw_block_assignment import BLOCK_A, BLOCK_B

    with app.app_context():
        t = _seed_tournament(_db)
        sb = _seed_saw_event(
            _db, t, name="Single Buck", event_type="college", gender="M"
        )
        dbuck = _seed_saw_event(
            _db,
            t,
            name="Double Buck",
            event_type="college",
            gender="M",
            is_partnered=True,
        )
        # Each event has 1 heat
        h_sb = _seed_heat(
            _db,
            sb,
            1,
            competitors=[1, 2, 3, 4],
            stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
        )
        h_db = _seed_heat(
            _db,
            dbuck,
            1,
            competitors=[10, 11, 12, 13],
            stand_assignments={"10": 1, "11": 1, "12": 2, "13": 2},
        )
        _db.session.commit()
        tid = t.id
        sb_id = sb.id
        dbuck_id = dbuck.id
        h_sb_id = h_sb.id
        h_db_id = h_db.id

    # Force order: Double Buck first, Single Buck second
    resp = auth_client.post(
        f"/scheduling/{tid}/events/reorder-friday",
        json={"event_ids": [dbuck_id, sb_id]},
    )
    assert resp.status_code == 200

    with app.app_context():
        from models import Heat

        h_sb = db.session.get(Heat, h_sb_id)
        h_db = db.session.get(Heat, h_db_id)
        # DB runs first -> Block A (stands 1 and 2 for the pairs)
        assert set(h_db.get_stand_assignments().values()).issubset(set(BLOCK_A))
        # SB runs second -> Block B
        assert _used_stands(h_sb) == BLOCK_B


def test_generation_saw_failure_rolls_back_primary_mutation(
    app, auth_client, monkeypatch,
):
    """Mandatory saw assignment and flight generation are one transaction."""
    from database import db as _db
    from services import saw_block_assignment as sba

    def _boom(_t, **_kwargs):
        raise RuntimeError("synthetic failure for test")

    monkeypatch.setattr(sba, "assign_saw_blocks", _boom)

    with app.app_context():
        t = _seed_tournament(_db)
        sb = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
        _seed_heat(
            _db,
            sb,
            1,
            competitors=[1, 2, 3, 4],
            stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
        )
        _db.session.commit()
        tid = t.id

    resp = auth_client.post(
        f"/scheduling/{tid}/flights/build",
        data={"num_flights": "1"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        from models import Flight

        flights = Flight.query.filter_by(tournament_id=tid).all()
        assert flights == []

    with auth_client.session_transaction() as session:
        flashes = list(session.get("_flashes", []))
    assert not any(category == "success" for category, _message in flashes)
    assert any(category == "error" for category, _message in flashes)


def test_optional_hook_failure_does_not_break_event_reorder(
    app, auth_client, monkeypatch,
):
    """A post-commit saw hook failure preserves an event-order mutation."""
    from database import db as _db
    from services import saw_block_assignment as sba

    def _boom(_t, **_kwargs):
        raise RuntimeError("synthetic failure for test")

    monkeypatch.setattr(sba, "assign_saw_blocks", _boom)

    with app.app_context():
        t = _seed_tournament(_db)
        first = _seed_saw_event(_db, t, name="Single Buck", event_type="college")
        second = _seed_saw_event(_db, t, name="Double Buck", event_type="college")
        _db.session.commit()
        tid = t.id
        expected_order = [second.id, first.id]

    resp = auth_client.post(
        f"/scheduling/{tid}/events/reorder-friday",
        json={"event_ids": expected_order},
    )
    assert resp.status_code == 200

    with app.app_context():
        from models import Tournament

        tournament = _db.session.get(Tournament, tid)
        assert tournament.get_schedule_config()["friday_event_order"] == expected_order

    with auth_client.session_transaction() as session:
        flashes = list(session.get("_flashes", []))
    assert any(category == "warning" for category, _message in flashes)
