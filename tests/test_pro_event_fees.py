"""
Tests for Pro event fee configuration UI.

Route: GET/POST /reporting/<tid>/pro/event-fees
Template: templates/reporting/event_fee_config.html

Covers:
  - GET renders with event rows, enrolled counts, suggested fees
  - POST sets entry_fees on competitors enrolled in each event
  - POST skips competitors not enrolled in an event (even if the form posted a fee)
  - POST skips blank form fields (no-op, doesn't wipe existing)
  - POST respects the 'overwrite' flag: default skips existing non-zero, flag overwrites
  - Invalid fee input flashes an error but doesn't crash

Run:
    pytest tests/test_pro_event_fees.py -v
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

    if not User.query.filter_by(username="fees_admin").first():
        u = User(username="fees_admin", role="admin")
        u.set_password("fees_pass")
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
        data={"username": "fees_admin", "password": "fees_pass"},
        follow_redirects=True,
    )
    return c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_tournament_with_events(session):
    """Seed a tournament with 2 pro events and 3 pro competitors.

    comps[0] enrolled in BOTH events
    comps[1] enrolled in event A only
    comps[2] enrolled in event B only
    Nobody has any entry_fees set initially.
    """
    from models import Event, Tournament
    from models.competitor import ProCompetitor

    t = Tournament(name="Fee UI Test 2026", year=2026, status="setup")
    session.add(t)
    session.flush()

    evt_a = Event(
        tournament_id=t.id,
        name="Springboard",
        event_type="pro",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="springboard",
        max_stands=4,
    )
    evt_b = Event(
        tournament_id=t.id,
        name="Underhand",
        event_type="pro",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="underhand",
        max_stands=5,
    )
    session.add_all([evt_a, evt_b])
    session.flush()

    c0 = ProCompetitor(
        tournament_id=t.id, name="Both Events", gender="M", status="active"
    )
    c1 = ProCompetitor(
        tournament_id=t.id, name="Springboard Only", gender="M", status="active"
    )
    c2 = ProCompetitor(
        tournament_id=t.id, name="Underhand Only", gender="M", status="active"
    )
    session.add_all([c0, c1, c2])
    session.flush()

    c0.set_events_entered([evt_a.id, evt_b.id])
    c1.set_events_entered([evt_a.id])
    c2.set_events_entered([evt_b.id])
    session.flush()

    return {"t": t, "evt_a": evt_a, "evt_b": evt_b, "c0": c0, "c1": c1, "c2": c2}


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


class TestEventFeesGet:

    def test_get_renders_with_event_rows(self, app, auth_client):
        with app.app_context():
            data = _seed_tournament_with_events(_db.session)
            _db.session.commit()
            tid = data["t"].id
            evt_a_name = data["evt_a"].name
            evt_b_name = data["evt_b"].name

        resp = auth_client.get(f"/reporting/{tid}/pro/event-fees")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert evt_a_name in body
        assert evt_b_name in body
        # Enrollment counts surfaced
        assert "Enrolled" in body or "enrolled" in body.lower()


# ---------------------------------------------------------------------------
# POST — fee application
# ---------------------------------------------------------------------------


class TestEventFeesPostSetsFees:

    def test_post_applies_fee_to_enrolled_competitors_only(self, app, auth_client):
        with app.app_context():
            data = _seed_tournament_with_events(_db.session)
            _db.session.commit()
            tid = data["t"].id
            evt_a_id = data["evt_a"].id
            c0_id, c1_id, c2_id = data["c0"].id, data["c1"].id, data["c2"].id

        resp = auth_client.post(
            f"/reporting/{tid}/pro/event-fees",
            data={f"fee_{evt_a_id}": "25"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        with app.app_context():
            from models.competitor import ProCompetitor

            c0 = ProCompetitor.query.get(c0_id)
            c1 = ProCompetitor.query.get(c1_id)
            c2 = ProCompetitor.query.get(c2_id)

            # c0 and c1 are enrolled in evt_a → fee set
            assert c0.get_entry_fees().get(str(evt_a_id)) == 25
            assert c1.get_entry_fees().get(str(evt_a_id)) == 25
            # c2 is NOT enrolled in evt_a → no fee set for evt_a
            assert str(evt_a_id) not in c2.get_entry_fees()

    def test_post_blank_fee_field_is_noop(self, app, auth_client):
        """An empty fee input must not wipe, set zero, or otherwise disturb fees."""
        with app.app_context():
            data = _seed_tournament_with_events(_db.session)
            # Pre-set c0's fee for evt_a so we can prove it wasn't touched
            c0 = data["c0"]
            c0.set_entry_fee(data["evt_a"].id, 15)
            _db.session.commit()
            tid = data["t"].id
            evt_a_id = data["evt_a"].id
            c0_id = c0.id

        resp = auth_client.post(
            f"/reporting/{tid}/pro/event-fees",
            data={f"fee_{evt_a_id}": ""},  # blank
            follow_redirects=False,
        )
        assert resp.status_code == 302

        with app.app_context():
            from models.competitor import ProCompetitor

            c0 = ProCompetitor.query.get(c0_id)
            assert c0.get_entry_fees().get(str(evt_a_id)) == 15  # untouched

    def test_post_skips_existing_non_zero_fee_by_default(self, app, auth_client):
        """Without overwrite flag, competitors with an existing fee are skipped."""
        with app.app_context():
            data = _seed_tournament_with_events(_db.session)
            # c0 already has $15 for evt_a. c1 has nothing set yet.
            data["c0"].set_entry_fee(data["evt_a"].id, 15)
            _db.session.commit()
            tid = data["t"].id
            evt_a_id = data["evt_a"].id
            c0_id, c1_id = data["c0"].id, data["c1"].id

        resp = auth_client.post(
            f"/reporting/{tid}/pro/event-fees",
            data={f"fee_{evt_a_id}": "30"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        with app.app_context():
            from models.competitor import ProCompetitor

            c0 = ProCompetitor.query.get(c0_id)
            c1 = ProCompetitor.query.get(c1_id)
            # c0 kept the original 15 (existing, no overwrite)
            assert c0.get_entry_fees().get(str(evt_a_id)) == 15
            # c1 got the new 30 (no existing fee)
            assert c1.get_entry_fees().get(str(evt_a_id)) == 30

    def test_post_overwrite_flag_replaces_existing_fees(self, app, auth_client):
        """With overwrite=on, existing non-zero fees ARE replaced."""
        with app.app_context():
            data = _seed_tournament_with_events(_db.session)
            data["c0"].set_entry_fee(data["evt_a"].id, 15)
            _db.session.commit()
            tid = data["t"].id
            evt_a_id = data["evt_a"].id
            c0_id = data["c0"].id

        resp = auth_client.post(
            f"/reporting/{tid}/pro/event-fees",
            data={f"fee_{evt_a_id}": "30", "overwrite": "on"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        with app.app_context():
            from models.competitor import ProCompetitor

            c0 = ProCompetitor.query.get(c0_id)
            assert c0.get_entry_fees().get(str(evt_a_id)) == 30

    def test_post_invalid_fee_does_not_crash(self, app, auth_client):
        """Garbage in the fee field flashes an error but returns 302, not 500."""
        with app.app_context():
            data = _seed_tournament_with_events(_db.session)
            _db.session.commit()
            tid = data["t"].id
            evt_a_id = data["evt_a"].id

        resp = auth_client.post(
            f"/reporting/{tid}/pro/event-fees",
            data={f"fee_{evt_a_id}": "twenty bucks"},
            follow_redirects=False,
        )
        assert resp.status_code == 302  # no 500
