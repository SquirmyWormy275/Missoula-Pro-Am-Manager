"""
Ability Rankings event-entry filtering.

Each pro ability-rankings card must contain only competitors who actually
signed up for an event in that category, segregated by the event's gender.
The prior bug rendered every active pro in every category regardless of
events_entered -- a judge looking at Obstacle Pole saw the entire roster.

Uses a module-scoped app + login-based auth_client so route commits do not
collide with the shared conftest admin_user fixture under nested savepoint
rollback (same pattern as test_proam_relay_redraw_route.py).
"""

import json
import os

import pytest

from database import db as _db


@pytest.fixture(scope="module")
def app():
    from tests.db_test_utils import create_test_app

    _app, db_path = create_test_app()
    with _app.app_context():
        _seed_admin()
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_admin():
    from models.user import User

    if not User.query.filter_by(username="ability_admin").first():
        u = User(username="ability_admin", role="admin")
        u.set_password("ability_pass")
        _db.session.add(u)
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
    c.post(
        "/auth/login",
        data={"username": "ability_admin", "password": "ability_pass"},
        follow_redirects=True,
    )
    return c


def _seed_tournament(session):
    from models import Tournament
    from models.competitor import ProCompetitor
    from models.event import Event

    t = Tournament(name="Ability Filter Test", year=2026, status="pro_active")
    session.add(t)
    session.flush()

    def _event(name, gender, stand_type):
        e = Event(
            tournament_id=t.id,
            name=name,
            event_type="pro",
            gender=gender,
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type=stand_type,
            max_stands=5,
        )
        session.add(e)
        session.flush()
        return e

    ev_uh_m = _event("Underhand", "M", "underhand")
    ev_uh_f = _event("Underhand", "F", "underhand")
    ev_ob_m = _event("Obstacle Pole", "M", "obstacle_pole")
    ev_ob_f = _event("Obstacle Pole", "F", "obstacle_pole")

    def _pro(name, gender, events):
        p = ProCompetitor(
            tournament_id=t.id,
            name=name,
            gender=gender,
            events_entered=json.dumps(events),
            gear_sharing=json.dumps({}),
            partners=json.dumps({}),
            status="active",
        )
        session.add(p)
        session.flush()
        return p

    alice = _pro("Alice Axe", "F", ["Underhand"])
    bob = _pro("Bob Buck", "M", ["Underhand"])
    cal = _pro("Cal Climb", "M", ["Obstacle Pole"])
    dan = _pro("Dan Dormant", "M", [])
    emma = _pro("Emma Evergreen", "F", ["Obstacle Pole"])

    session.commit()

    return {
        "tournament": t,
        "alice": alice,
        "bob": bob,
        "cal": cal,
        "dan": dan,
        "emma": emma,
        "ev_uh_m": ev_uh_m,
        "ev_uh_f": ev_uh_f,
        "ev_ob_m": ev_ob_m,
        "ev_ob_f": ev_ob_f,
    }


def _slice(html, start_marker, end_markers):
    """Return the substring of html starting at start_marker, ending at
    the first end_marker that appears later in the string."""
    start = html.find(start_marker)
    assert start >= 0, f"missing marker {start_marker!r}"
    rest = html[start:]
    end = len(rest)
    for m in end_markers:
        idx = rest.find(m, len(start_marker))
        if idx >= 0 and idx < end:
            end = idx
    return rest[:end]


def test_underhand_card_contains_only_underhand_signups(db_session, auth_client):
    seeded = _seed_tournament(db_session)
    resp = auth_client.get(
        f"/scheduling/{seeded['tournament'].id}/pro/ability-rankings"
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    underhand_card = _slice(
        html,
        "Underhand",
        ["Obstacle Pole", "College Birling Seedings", "Save Rankings"],
    )

    # Bob and Alice signed up for Underhand, must appear.
    assert "Bob Buck" in underhand_card
    assert "Alice Axe" in underhand_card
    # Cal and Emma signed up for Obstacle Pole only; Dan for nothing.
    assert "Cal Climb" not in underhand_card
    assert "Emma Evergreen" not in underhand_card
    assert "Dan Dormant" not in underhand_card


def test_obstacle_pole_card_contains_only_obstacle_pole_signups(
    db_session, auth_client
):
    seeded = _seed_tournament(db_session)
    resp = auth_client.get(
        f"/scheduling/{seeded['tournament'].id}/pro/ability-rankings"
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    obstacle_card = _slice(
        html,
        "Obstacle Pole",
        ["College Birling Seedings", "Save Rankings"],
    )

    assert "Cal Climb" in obstacle_card
    assert "Emma Evergreen" in obstacle_card
    # Underhand-only signups must not appear.
    assert "Bob Buck" not in obstacle_card
    assert "Alice Axe" not in obstacle_card
    assert "Dan Dormant" not in obstacle_card


def test_pro_with_no_signups_never_appears(db_session, auth_client):
    seeded = _seed_tournament(db_session)
    resp = auth_client.get(
        f"/scheduling/{seeded['tournament'].id}/pro/ability-rankings"
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Dan entered no events — must not appear in any card on the page.
    assert "Dan Dormant" not in html
