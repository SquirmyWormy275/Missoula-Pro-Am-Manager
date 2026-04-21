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

import pytest

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
    from models import Heat

    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
        competitors=json.dumps(competitors),
        stand_assignments=json.dumps(stand_assignments),
    )
    if flight is not None:
        h.flight_id = flight.id
        h.flight_position = flight_position
    db.session.add(h)
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

        h1 = Heat.query.get(h1_id)
        h2 = Heat.query.get(h2_id)
        # After build_flights, both heats are in flight 1.
        # The first heat in flight_position order gets Block A, the second Block B.
        ordered_by_flight = sorted(
            [h1, h2], key=lambda h: (h.flight_position or 999, h.id)
        )
        assert _used_stands(ordered_by_flight[0]) == BLOCK_A
        assert _used_stands(ordered_by_flight[1]) == BLOCK_B


def test_reorder_flight_heats_triggers_recompute(app, auth_client):
    """Reordering heats within a flight recomputes blocks per new run order."""
    from database import db as _db
    from services.saw_block_assignment import BLOCK_A, BLOCK_B

    with app.app_context():
        t = _seed_tournament(_db)
        sb = _seed_saw_event(_db, t, name="Single Buck", event_type="pro")
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
            sb,
            2,
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

    # Reverse the flight order: h_b first, h_a second
    resp = auth_client.post(
        f"/scheduling/{tid}/flights/{fid}/reorder",
        json={"heat_ids": [h_b_id, h_a_id]},
    )
    assert resp.status_code == 200

    with app.app_context():
        from models import Heat

        h_a = Heat.query.get(h_a_id)
        h_b = Heat.query.get(h_b_id)
        # h_b is now first in flight -> Block A; h_a is second -> Block B
        assert _used_stands(h_b) == BLOCK_A
        assert _used_stands(h_a) == BLOCK_B


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
        h_a = Heat.query.get(h_a_id)
        h_b = Heat.query.get(h_b_id)
        h_c = Heat.query.get(h_c_id)
        assert h_a.flight_id == f1_id and h_a.flight_position == 1
        assert h_b.flight_id == f2_id and h_b.flight_position == 1
        assert h_c.flight_id == f2_id and h_c.flight_position == 2


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

        h_sb = Heat.query.get(h_sb_id)
        h_db = Heat.query.get(h_db_id)
        # DB runs first -> Block A (stands 1 and 2 for the pairs)
        assert set(h_db.get_stand_assignments().values()).issubset(set(BLOCK_A))
        # SB runs second -> Block B
        assert _used_stands(h_sb) == BLOCK_B


def test_hook_failure_does_not_break_primary_mutation(app, auth_client, monkeypatch):
    """An exception inside trigger_saw_block_recompute must not 500 the route."""
    from database import db as _db

    # Monkeypatch assign_saw_blocks to raise
    from services import saw_block_assignment as sba

    def _boom(_t):
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

    # Primary mutation (build_flights) must succeed despite hook failure
    resp = auth_client.post(
        f"/scheduling/{tid}/flights/build",
        data={"num_flights": "1"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        from models import Flight

        flights = Flight.query.filter_by(tournament_id=tid).all()
        assert len(flights) == 1  # primary mutation committed
