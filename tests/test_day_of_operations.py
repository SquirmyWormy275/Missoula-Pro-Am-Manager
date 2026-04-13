"""
Tests for day-of heat operations: scratch, capacity guards, add-to-heat, delete empty heat.

Self-contained module-scoped app fixture (same pattern as test_dual_timer_entry.py)
to avoid conftest's per-test admin user creation cascade.

Run:  pytest tests/test_day_of_operations.py -v
"""

import json
import os

import pytest

from database import db as _db

# ---------------------------------------------------------------------------
# Self-contained app fixture (module-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    """Test Flask app with temp-file SQLite built via flask db upgrade."""
    from tests.db_test_utils import create_test_app

    _app, db_path = create_test_app()

    with _app.app_context():
        _seed_admin(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_admin(app):
    from models.user import User

    if not User.query.filter_by(username="dayof_admin").first():
        u = User(username="dayof_admin", role="admin")
        u.set_password("dayof_pass")
        _db.session.add(u)
    # Second user for lock tests (a different judge)
    if not User.query.filter_by(username="dayof_other_judge").first():
        u2 = User(username="dayof_other_judge", role="judge")
        u2.set_password("other_pass")
        _db.session.add(u2)
    _db.session.commit()


@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def auth_client(app):
    c = app.test_client()
    with app.app_context():
        c.post(
            "/auth/login",
            data={
                "username": "dayof_admin",
                "password": "dayof_pass",
            },
            follow_redirects=True,
        )
    return c


# ---------------------------------------------------------------------------
# Local seed helpers
# ---------------------------------------------------------------------------


def _make_tournament(session, name="DayOf Test", year=2026):
    from models import Tournament

    t = Tournament(name=name, year=year, status="setup")
    session.add(t)
    session.flush()
    return t


def _make_team(session, tid, code="UM-A"):
    from models import Team

    t = Team(
        tournament_id=tid, team_code=code, school_name="UM", school_abbreviation="UM"
    )
    session.add(t)
    session.flush()
    return t


def _make_event(session, tid, name="Underhand", **kw):
    from models.event import Event

    defaults = dict(
        tournament_id=tid,
        name=name,
        event_type="pro",
        gender="M",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="underhand",
        max_stands=5,
        status="pending",
        payouts=json.dumps({}),
    )
    defaults.update(kw)
    e = Event(**defaults)
    session.add(e)
    session.flush()
    return e


def _make_pro(session, tid, name, gender="M", events=None):
    from models.competitor import ProCompetitor

    c = ProCompetitor(
        tournament_id=tid,
        name=name,
        gender=gender,
        events_entered=json.dumps(events or []),
        status="active",
    )
    session.add(c)
    session.flush()
    return c


def _make_college(session, tid, team_id, name, gender="M", events=None):
    from models.competitor import CollegeCompetitor

    c = CollegeCompetitor(
        tournament_id=tid,
        team_id=team_id,
        name=name,
        gender=gender,
        events_entered=json.dumps(events or []),
        status="active",
    )
    session.add(c)
    session.flush()
    return c


def _make_heat(
    session,
    event_id,
    heat_number=1,
    run_number=1,
    competitors=None,
    stand_assignments=None,
    status="pending",
):
    from models.heat import Heat

    h = Heat(
        event_id=event_id,
        heat_number=heat_number,
        run_number=run_number,
        competitors=json.dumps(competitors or []),
        stand_assignments=json.dumps(stand_assignments or {}),
        status=status,
    )
    session.add(h)
    session.flush()
    return h


def _make_result(
    session, event_id, comp, comp_type="pro", status="pending", result_value=None
):
    from models.event import EventResult

    r = EventResult(
        event_id=event_id,
        competitor_id=comp.id,
        competitor_type=comp_type,
        competitor_name=comp.name,
        status=status,
        result_value=result_value,
    )
    session.add(r)
    session.flush()
    return r


# ---------------------------------------------------------------------------
# Phase 1: Scratch from heat
# ---------------------------------------------------------------------------


class TestScratchCompetitor:
    """Tests for POST /scheduling/<tid>/event/<eid>/scratch-competitor."""

    def test_scratch_removes_from_heat_json(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id)
        c1 = _make_pro(db_session, t.id, "Alice_1", events=[e.id])
        c2 = _make_pro(db_session, t.id, "Bob_1", events=[e.id])
        _make_result(db_session, e.id, c1)
        _make_result(db_session, e.id, c2)
        h = _make_heat(
            db_session,
            e.id,
            competitors=[c1.id, c2.id],
            stand_assignments={str(c1.id): 1, str(c2.id): 2},
        )
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/scratch-competitor",
            data={"competitor_id": c1.id, "heat_id": h.id},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        from models.heat import Heat

        heat = _db.session.get(Heat, h.id)
        assert c1.id not in heat.get_competitors()
        assert c2.id in heat.get_competitors()

    def test_scratch_frees_stand(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Stand Test")
        c1 = _make_pro(db_session, t.id, "Alice_2", events=[e.id])
        _make_result(db_session, e.id, c1)
        h = _make_heat(
            db_session, e.id, competitors=[c1.id], stand_assignments={str(c1.id): 1}
        )
        db_session.commit()

        auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/scratch-competitor",
            data={"competitor_id": c1.id, "heat_id": h.id},
            follow_redirects=True,
        )

        from models.heat import Heat

        heat = _db.session.get(Heat, h.id)
        assert str(c1.id) not in heat.get_stand_assignments()

    def test_scratch_sets_event_result_status_scratched(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Status Test")
        c1 = _make_pro(db_session, t.id, "Alice_3", events=[e.id])
        r = _make_result(db_session, e.id, c1)
        h = _make_heat(
            db_session, e.id, competitors=[c1.id], stand_assignments={str(c1.id): 1}
        )
        db_session.commit()

        auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/scratch-competitor",
            data={"competitor_id": c1.id, "heat_id": h.id},
            follow_redirects=True,
        )

        from models.event import EventResult

        result = EventResult.query.filter_by(event_id=e.id, competitor_id=c1.id).first()
        assert result is not None  # Row preserved
        assert result.status == "scratched"

    def test_scratch_preserves_result_value(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Preserve Test")
        c1 = _make_pro(db_session, t.id, "Alice_4", events=[e.id])
        _make_result(db_session, e.id, c1, result_value=42.5)
        h = _make_heat(
            db_session, e.id, competitors=[c1.id], stand_assignments={str(c1.id): 1}
        )
        db_session.commit()

        auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/scratch-competitor",
            data={"competitor_id": c1.id, "heat_id": h.id},
            follow_redirects=True,
        )

        from models.event import EventResult

        result = EventResult.query.filter_by(event_id=e.id, competitor_id=c1.id).first()
        assert result.result_value == 42.5
        assert result.status == "scratched"

    def test_scratch_syncs_heat_assignments(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Sync Test")
        c1 = _make_pro(db_session, t.id, "Alice_5", events=[e.id])
        c2 = _make_pro(db_session, t.id, "Bob_5", events=[e.id])
        _make_result(db_session, e.id, c1)
        _make_result(db_session, e.id, c2)
        h = _make_heat(
            db_session,
            e.id,
            competitors=[c1.id, c2.id],
            stand_assignments={str(c1.id): 1, str(c2.id): 2},
        )
        h.sync_assignments("pro")
        db_session.commit()

        auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/scratch-competitor",
            data={"competitor_id": c1.id, "heat_id": h.id},
            follow_redirects=True,
        )

        from models.heat import HeatAssignment

        assignments = HeatAssignment.query.filter_by(heat_id=h.id).all()
        comp_ids = {a.competitor_id for a in assignments}
        assert c1.id not in comp_ids
        assert c2.id in comp_ids

    def test_scratch_competitor_not_in_heat_rejected(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Reject Test")
        c1 = _make_pro(db_session, t.id, "Alice_6", events=[e.id])
        c2 = _make_pro(db_session, t.id, "Bob_6", events=[e.id])
        _make_result(db_session, e.id, c1)
        _make_result(db_session, e.id, c2)
        h = _make_heat(
            db_session, e.id, competitors=[c2.id], stand_assignments={str(c2.id): 1}
        )
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/scratch-competitor",
            data={"competitor_id": c1.id, "heat_id": h.id},
            follow_redirects=True,
        )
        assert b"not in the selected heat" in resp.data


class TestScratchDualRun:
    """Test that scratch mirrors across both runs for dual-run events."""

    def test_scratch_mirrors_both_runs(self, db_session, auth_client):
        t = _make_tournament(db_session)
        team = _make_team(db_session, t.id, code="UM-DR")
        e = _make_event(
            db_session,
            t.id,
            name="Speed Climb DR",
            event_type="college",
            stand_type="speed_climb",
            max_stands=2,
            requires_dual_runs=True,
        )
        c = _make_college(db_session, t.id, team.id, "Charlie_DR", events=[e.id])
        _make_result(db_session, e.id, c, comp_type="college")
        h_r1 = _make_heat(
            db_session,
            e.id,
            heat_number=1,
            run_number=1,
            competitors=[c.id],
            stand_assignments={str(c.id): 1},
        )
        h_r2 = _make_heat(
            db_session,
            e.id,
            heat_number=1,
            run_number=2,
            competitors=[c.id],
            stand_assignments={str(c.id): 2},
        )
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/scratch-competitor",
            data={"competitor_id": c.id, "heat_id": h_r1.id},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        from models.heat import Heat

        r1 = _db.session.get(Heat, h_r1.id)
        r2 = _db.session.get(Heat, h_r2.id)
        assert c.id not in r1.get_competitors()
        assert c.id not in r2.get_competitors()


class TestScratchLockedHeat:
    """Test that scratch is rejected when heat is locked by another judge."""

    def test_scratch_locked_heat_rejected(self, db_session, auth_client):
        from datetime import datetime, timezone

        from models.user import User

        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Lock Test")
        c = _make_pro(db_session, t.id, "Dave_Lock", events=[e.id])
        _make_result(db_session, e.id, c)
        h = _make_heat(
            db_session, e.id, competitors=[c.id], stand_assignments={str(c.id): 1}
        )
        # Lock the heat by the other judge (seeded in _seed_admin)
        other_judge = User.query.filter_by(username="dayof_other_judge").first()
        h.locked_by_user_id = other_judge.id
        h.locked_at = datetime.now(timezone.utc)
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/scratch-competitor",
            data={"competitor_id": c.id, "heat_id": h.id},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"being scored by another judge" in resp.data

        # Competitor should still be in the heat
        from models.heat import Heat

        heat = _db.session.get(Heat, h.id)
        assert c.id in heat.get_competitors()


# ---------------------------------------------------------------------------
# Phase 2: Capacity and safety guards
# ---------------------------------------------------------------------------

class TestMoveCapacityGuard:
    """Test that move is rejected when destination heat is full."""

    def test_move_into_full_heat_rejected(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Cap Test", max_stands=2)
        c1 = _make_pro(db_session, t.id, "Cap_A", events=[e.id])
        c2 = _make_pro(db_session, t.id, "Cap_B", events=[e.id])
        c3 = _make_pro(db_session, t.id, "Cap_C", events=[e.id])
        _make_result(db_session, e.id, c1)
        _make_result(db_session, e.id, c2)
        _make_result(db_session, e.id, c3)
        h1 = _make_heat(db_session, e.id, heat_number=1,
                        competitors=[c1.id], stand_assignments={str(c1.id): 1})
        h2 = _make_heat(db_session, e.id, heat_number=2,
                        competitors=[c2.id, c3.id],
                        stand_assignments={str(c2.id): 1, str(c3.id): 2})
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/move-competitor",
            data={"competitor_id": c1.id, "from_heat_id": h1.id, "to_heat_id": h2.id},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"full" in resp.data.lower()

        # Competitor should still be in source heat
        from models.heat import Heat
        source = _db.session.get(Heat, h1.id)
        assert c1.id in source.get_competitors()


class TestFinalizationGuard:
    """Test that heat generation is blocked for finalized events."""

    def test_regen_finalized_event_blocked(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Final Test")
        e.is_finalized = True
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/generate-heats",
            data={"confirm": "true"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"finalized" in resp.data.lower()

    def test_regen_scored_event_without_confirm_warned(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Scored Test")
        c = _make_pro(db_session, t.id, "Scored_A", events=[e.id])
        _make_result(db_session, e.id, c, status="completed", result_value=10.0)
        db_session.commit()

        # POST without confirm — should warn
        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/generate-heats",
            data={},  # no confirm
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"scored results" in resp.data.lower()


# ---------------------------------------------------------------------------
# Phase 3: Add late entry to heat
# ---------------------------------------------------------------------------

class TestAddToHeat:
    """Tests for POST /scheduling/<tid>/event/<eid>/add-to-heat."""

    def test_add_competitor_to_heat(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Add Test", max_stands=5)
        c1 = _make_pro(db_session, t.id, "InHeat_A", events=[e.id])
        c2 = _make_pro(db_session, t.id, "NotInHeat_A", events=[e.id])
        _make_result(db_session, e.id, c1)
        _make_result(db_session, e.id, c2)
        h = _make_heat(db_session, e.id, competitors=[c1.id],
                       stand_assignments={str(c1.id): 1})
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/add-to-heat",
            data={"competitor_id": c2.id, "heat_id": h.id},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        from models.heat import Heat
        heat = _db.session.get(Heat, h.id)
        assert c2.id in heat.get_competitors()
        assert str(c2.id) in heat.get_stand_assignments()

    def test_add_to_full_heat_rejected(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Full Add Test", max_stands=1)
        c1 = _make_pro(db_session, t.id, "Full_A", events=[e.id])
        c2 = _make_pro(db_session, t.id, "Full_B", events=[e.id])
        _make_result(db_session, e.id, c1)
        _make_result(db_session, e.id, c2)
        h = _make_heat(db_session, e.id, competitors=[c1.id],
                       stand_assignments={str(c1.id): 1})
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/add-to-heat",
            data={"competitor_id": c2.id, "heat_id": h.id},
            follow_redirects=True,
        )
        assert b"full" in resp.data.lower()

    def test_add_creates_event_result_if_missing(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="NoResult Add", max_stands=5)
        c1 = _make_pro(db_session, t.id, "NoRes_A", events=[e.id])
        # No EventResult created for c1
        h = _make_heat(db_session, e.id, competitors=[],
                       stand_assignments={})
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/add-to-heat",
            data={"competitor_id": c1.id, "heat_id": h.id},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        from models.event import EventResult
        result = EventResult.query.filter_by(event_id=e.id, competitor_id=c1.id).first()
        assert result is not None
        assert result.status == "pending"

    def test_add_dual_run_mirrors(self, db_session, auth_client):
        t = _make_tournament(db_session)
        team = _make_team(db_session, t.id, code="UM-ADD")
        e = _make_event(db_session, t.id, name="Dual Add",
                        event_type="college", stand_type="speed_climb",
                        max_stands=2, requires_dual_runs=True)
        c = _make_college(db_session, t.id, team.id, "DualAdd_C", events=[e.id])
        _make_result(db_session, e.id, c, comp_type="college")
        h_r1 = _make_heat(db_session, e.id, heat_number=1, run_number=1,
                          competitors=[], stand_assignments={})
        h_r2 = _make_heat(db_session, e.id, heat_number=1, run_number=2,
                          competitors=[], stand_assignments={})
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/add-to-heat",
            data={"competitor_id": c.id, "heat_id": h_r1.id},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        from models.heat import Heat
        r1 = _db.session.get(Heat, h_r1.id)
        r2 = _db.session.get(Heat, h_r2.id)
        assert c.id in r1.get_competitors()
        assert c.id in r2.get_competitors()


# ---------------------------------------------------------------------------
# Phase 4: Delete empty heat
# ---------------------------------------------------------------------------

class TestDeleteEmptyHeat:
    """Tests for POST /scheduling/<tid>/event/<eid>/delete-heat/<hid>."""

    def test_delete_empty_heat(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="Del Test")
        h1 = _make_heat(db_session, e.id, heat_number=1, competitors=[])
        h2 = _make_heat(db_session, e.id, heat_number=2, competitors=[42])
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/delete-heat/{h1.id}",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"deleted" in resp.data.lower()

        from models.heat import Heat
        assert _db.session.get(Heat, h1.id) is None
        # h2 should be renumbered to heat_number=1
        remaining = _db.session.get(Heat, h2.id)
        assert remaining is not None
        assert remaining.heat_number == 1

    def test_delete_non_empty_heat_rejected(self, db_session, auth_client):
        t = _make_tournament(db_session)
        e = _make_event(db_session, t.id, name="NonEmpty Del")
        c = _make_pro(db_session, t.id, "Del_A", events=[e.id])
        h = _make_heat(db_session, e.id, competitors=[c.id],
                       stand_assignments={str(c.id): 1})
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/event/{e.id}/delete-heat/{h.id}",
            follow_redirects=True,
        )
        assert b"cannot delete" in resp.data.lower()

        from models.heat import Heat
        assert _db.session.get(Heat, h.id) is not None
