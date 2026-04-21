"""
Admin safety valve + status page tests for hand-saw block alternation.

Covers:
  - POST /scheduling/<tid>/heats/recompute-saw-blocks succeeds, flashes,
    redirects, and triggers assign_saw_blocks
  - GET  /scheduling/<tid>/saw-blocks-status renders 200 and shows
    the expected run-order rows per day
  - Status page empty state: tournament with no saw events renders
    an empty-state message for both days
  - Sidebar link is hidden when the tournament has no saw events, and
    present when it does

Run:  pytest tests/test_saw_blocks_admin.py -v
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test-saw-blocks-admin")
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
        try:
            from services.heat_generator import _get_tournament_events

            if hasattr(_get_tournament_events, "_cache"):
                _get_tournament_events._cache.clear()
        except Exception:
            pass


@pytest.fixture()
def auth_client(app):
    from database import db as _db
    from models.user import User

    with app.app_context():
        u = User(username="sba_admin", role="admin")
        u.set_password("pass")
        _db.session.add(u)
        _db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_tournament(db, name="SawAdmin 2026"):
    from models import Tournament

    t = Tournament(name=name, year=2026, status="setup")
    db.session.add(t)
    db.session.flush()
    return t


def _seed_saw_event(
    db, tournament, name="Single Buck", event_type="college", gender="M"
):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender=gender,
        scoring_type="time",
        stand_type="saw_hand",
    )
    db.session.add(e)
    db.session.flush()
    return e


def _seed_non_saw_event(db, tournament):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name="Underhand Speed",
        event_type="college",
        gender="M",
        scoring_type="time",
        stand_type="underhand",
    )
    db.session.add(e)
    db.session.flush()
    return e


def _seed_heat(db, event, heat_number, competitors, stand_assignments):
    from models import Heat

    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=1,
        competitors=json.dumps(competitors),
        stand_assignments=json.dumps(stand_assignments),
    )
    db.session.add(h)
    db.session.flush()
    return h


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_recompute_route_succeeds(app, auth_client):
    """POST recompute-saw-blocks triggers assign_saw_blocks, flashes, redirects."""
    from database import db as _db
    from services.saw_block_assignment import BLOCK_A, BLOCK_B

    with app.app_context():
        t = _seed_tournament(_db)
        sb = _seed_saw_event(_db, t, name="Single Buck")
        # 2 heats, both pre-seeded on Block A
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
        f"/scheduling/{tid}/heats/recompute-saw-blocks",
        data={},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with app.app_context():
        from models import Heat

        h1 = Heat.query.get(h1_id)
        h2 = Heat.query.get(h2_id)
        # Alternation applied: heat 1 -> Block A, heat 2 -> Block B
        used_1 = sorted({int(v) for v in h1.get_stand_assignments().values()})
        used_2 = sorted({int(v) for v in h2.get_stand_assignments().values()})
        assert used_1 == BLOCK_A
        assert used_2 == BLOCK_B


def test_status_page_renders(app, auth_client):
    """GET saw-blocks-status returns 200 and includes expected rows."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db)
        sb = _seed_saw_event(_db, t, name="Single Buck")
        _seed_heat(
            _db,
            sb,
            1,
            competitors=[1, 2, 3, 4],
            stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
        )
        _seed_heat(
            _db,
            sb,
            2,
            competitors=[5, 6, 7, 8],
            stand_assignments={"5": 5, "6": 6, "7": 7, "8": 8},
        )
        _db.session.commit()
        tid = t.id

    resp = auth_client.get(f"/scheduling/{tid}/saw-blocks-status")
    assert resp.status_code == 200

    body = resp.get_data(as_text=True)
    # Page header is present
    assert "Saw Stand Block Assignments" in body
    # Both blocks appear
    assert "Block A" in body
    assert "Block B" in body
    # Single Buck event name is in the rendered table
    assert "Single Buck" in body
    # Both day sections render
    assert "Friday" in body
    assert "Saturday" in body
    # Recompute form is rendered
    assert "/heats/recompute-saw-blocks" in body


def test_status_page_empty_state(app, auth_client):
    """Tournament with no saw events renders empty-state message for both days."""
    from database import db as _db

    with app.app_context():
        t = _seed_tournament(_db, name="No Saw Tournament")
        _seed_non_saw_event(_db, t)
        _db.session.commit()
        tid = t.id

    resp = auth_client.get(f"/scheduling/{tid}/saw-blocks-status")
    assert resp.status_code == 200

    body = resp.get_data(as_text=True)
    # Expect the empty-state message to appear (once per day section)
    assert body.count("No hand-saw heats scheduled.") >= 2


def test_sidebar_link_hidden_when_no_saw_events(app, auth_client):
    """Sidebar renders WITHOUT the Saw Block Status link for a saw-free tournament,
    and WITH the link once at least one saw_hand event exists."""
    from database import db as _db

    with app.app_context():
        # Tournament A: no saw events
        t_a = _seed_tournament(_db, name="No Saw")
        _seed_non_saw_event(_db, t_a)
        _db.session.commit()
        tid_a = t_a.id

    # Hitting tournament_detail renders the sidebar context
    resp_a = auth_client.get(f"/tournament/{tid_a}")
    assert resp_a.status_code == 200
    body_a = resp_a.get_data(as_text=True)
    assert "Saw Block Status" not in body_a

    with app.app_context():
        # Tournament B: one saw event
        t_b = _seed_tournament(_db, name="Has Saw")
        _seed_saw_event(_db, t_b, name="Single Buck")
        _db.session.commit()
        tid_b = t_b.id

    resp_b = auth_client.get(f"/tournament/{tid_b}")
    assert resp_b.status_code == 200
    body_b = resp_b.get_data(as_text=True)
    assert "Saw Block Status" in body_b
    assert f"/scheduling/{tid_b}/saw-blocks-status" in body_b
