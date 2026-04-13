"""
Tests for partner reassignment queue (Unit 11 — Race-Day Integrity).

Covers:
  - Orphaned partner detection after partner scratched
  - Bidirectional partner JSON update on reassignment
  - Empty queue message when no orphans
  - Gender mismatch rejection for mixed-gender events
  - Already-partnered competitor rejection
  - EventResult.partner_name updated when result exists

Run:  pytest tests/test_partner_reassignment.py -v
"""

import json
import os

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-partner-reassign")
os.environ.setdefault("WTF_CSRF_ENABLED", "False")

from database import db as _db
from tests.db_test_utils import create_test_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    _app, db_path = create_test_app()
    with _app.app_context():
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture()
def db_session(app):
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def auth_client(app, db_session, request):
    import uuid

    from models.user import User

    unique_name = f"test_admin_pr_{uuid.uuid4().hex[:8]}"
    u = User(username=unique_name, role="admin")
    u.set_password("pass")
    db_session.add(u)
    db_session.flush()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
    return c


def _make_tournament(session):
    from models import Tournament

    t = Tournament(name="PR Test Tournament", year=2026, status="setup")
    session.add(t)
    session.flush()
    return t


def _make_partnered_event(session, tournament, gender_req="any"):
    from models.event import Event

    e = Event(
        tournament_id=tournament.id,
        name="Partnered Axe Throw",
        event_type="pro",
        scoring_type="score",
        scoring_order="highest_wins",
        is_partnered=True,
        partner_gender_requirement=gender_req,
    )
    session.add(e)
    session.flush()
    return e


def _make_pro(session, tournament, name, gender="M", status="active", partners=None):
    from models.competitor import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status=status,
        partners=json.dumps(partners or {}),
    )
    session.add(c)
    session.flush()
    return c


def _make_result(session, event, competitor, partner_name=None):
    from models.event import EventResult

    r = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type="pro",
        competitor_name=competitor.name,
        partner_name=partner_name,
        status="pending",
    )
    session.add(r)
    session.flush()
    return r


# ---------------------------------------------------------------------------
# Helper: build orphan detection dataset
# ---------------------------------------------------------------------------


def _setup_orphan_scenario(db_session):
    """Alice + Bob paired; Bob scratched → Alice is orphaned. Carol is free."""
    t = _make_tournament(db_session)
    ev = _make_partnered_event(db_session, t, gender_req="any")

    alice = _make_pro(db_session, t, "Alice", gender="F", partners={str(ev.id): "Bob"})
    bob = _make_pro(
        db_session,
        t,
        "Bob",
        gender="M",
        status="scratched",
        partners={str(ev.id): "Alice"},
    )
    carol = _make_pro(db_session, t, "Carol", gender="F", status="active")

    # Alice has an EventResult; Bob's result scratched
    _make_result(db_session, ev, alice, partner_name="Bob")
    _make_result(db_session, ev, bob, partner_name="Alice")

    return t, ev, alice, bob, carol


# ---------------------------------------------------------------------------
# Unit tests: orphan detection service helper
# ---------------------------------------------------------------------------


class TestOrphanDetection:
    def test_orphaned_competitor_detected(self, app, db_session):
        """Alice is orphaned when her partner Bob is scratched."""
        from routes.scheduling.partners import get_orphaned_competitors

        t, ev, alice, bob, carol = _setup_orphan_scenario(db_session)

        orphans = get_orphaned_competitors(ev)
        orphan_ids = [o["competitor"].id for o in orphans]
        assert alice.id in orphan_ids

    def test_scratched_partner_not_in_orphan_list(self, app, db_session):
        """Bob (scratched) is not listed as an orphan himself."""
        from routes.scheduling.partners import get_orphaned_competitors

        t, ev, alice, bob, carol = _setup_orphan_scenario(db_session)

        orphans = get_orphaned_competitors(ev)
        orphan_ids = [o["competitor"].id for o in orphans]
        assert bob.id not in orphan_ids

    def test_no_orphans_when_all_active(self, app, db_session):
        """No orphans when both partners are active."""
        from routes.scheduling.partners import get_orphaned_competitors

        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t)
        dave = _make_pro(
            db_session, t, "Dave", gender="M", partners={str(ev.id): "Eve"}
        )
        eve = _make_pro(db_session, t, "Eve", gender="F", partners={str(ev.id): "Dave"})
        _make_result(db_session, ev, dave, partner_name="Eve")
        _make_result(db_session, ev, eve, partner_name="Dave")

        orphans = get_orphaned_competitors(ev)
        assert orphans == []

    def test_no_orphans_when_event_empty(self, app, db_session):
        """Empty event: no orphans."""
        from routes.scheduling.partners import get_orphaned_competitors

        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t)
        orphans = get_orphaned_competitors(ev)
        assert orphans == []


# ---------------------------------------------------------------------------
# Unit tests: gender validation helper
# ---------------------------------------------------------------------------


class TestGenderValidation:
    def test_mixed_requires_opposite_gender(self, app, db_session):
        """For mixed events, new partner must be opposite gender."""
        from routes.scheduling.partners import validate_reassignment

        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t, gender_req="mixed")
        alice = _make_pro(db_session, t, "Alice", gender="F")
        carol = _make_pro(db_session, t, "Carol", gender="F")

        ok, error = validate_reassignment(ev, alice, carol)
        assert not ok
        assert "gender" in error.lower()

    def test_mixed_allows_opposite_gender(self, app, db_session):
        """For mixed events, new partner of opposite gender is valid."""
        from routes.scheduling.partners import validate_reassignment

        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t, gender_req="mixed")
        alice = _make_pro(db_session, t, "Alice", gender="F")
        frank = _make_pro(db_session, t, "Frank", gender="M")

        ok, error = validate_reassignment(ev, alice, frank)
        assert ok
        assert error is None

    def test_same_gender_requirement(self, app, db_session):
        """For same-gender events, new partner must match orphan gender."""
        from routes.scheduling.partners import validate_reassignment

        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t, gender_req="same")
        alice = _make_pro(db_session, t, "Alice", gender="F")
        frank = _make_pro(db_session, t, "Frank", gender="M")

        ok, error = validate_reassignment(ev, alice, frank)
        assert not ok
        assert "gender" in error.lower()

    def test_any_gender_always_valid(self, app, db_session):
        """For any-gender events, gender combination is always valid."""
        from routes.scheduling.partners import validate_reassignment

        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t, gender_req="any")
        alice = _make_pro(db_session, t, "Alice", gender="F")
        frank = _make_pro(db_session, t, "Frank", gender="M")

        ok, error = validate_reassignment(ev, alice, frank)
        assert ok

    def test_already_partnered_rejected(self, app, db_session):
        """New partner who already has a partner is rejected."""
        from routes.scheduling.partners import validate_reassignment

        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t, gender_req="any")
        alice = _make_pro(db_session, t, "Alice", gender="F")
        frank = _make_pro(
            db_session, t, "Frank", gender="M", partners={str(ev.id): "Grace"}
        )
        # Frank already has a partner stored
        ok, error = validate_reassignment(ev, alice, frank)
        assert not ok
        assert "partner" in error.lower()


# ---------------------------------------------------------------------------
# Unit tests: bidirectional update helper
# ---------------------------------------------------------------------------


class TestBidirectionalUpdate:
    def test_sets_partner_on_both_competitors(self, app, db_session):
        """set_partner_bidirectional writes to both competitors."""
        from routes.scheduling.partners import set_partner_bidirectional

        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t)
        alice = _make_pro(db_session, t, "Alice", gender="F")
        frank = _make_pro(db_session, t, "Frank", gender="M")

        set_partner_bidirectional(alice, frank, ev)

        assert alice.get_partners().get(str(ev.id)) == "Frank"
        assert frank.get_partners().get(str(ev.id)) == "Alice"

    def test_updates_event_result_partner_name(self, app, db_session):
        """set_partner_bidirectional also updates EventResult.partner_name when present."""
        from routes.scheduling.partners import set_partner_bidirectional

        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t)
        alice = _make_pro(db_session, t, "Alice", gender="F")
        frank = _make_pro(db_session, t, "Frank", gender="M")
        result = _make_result(db_session, ev, alice, partner_name="Bob")

        set_partner_bidirectional(alice, frank, ev)
        db_session.flush()

        from models.event import EventResult

        r = EventResult.query.filter_by(
            event_id=ev.id,
            competitor_id=alice.id,
        ).first()
        assert r is not None
        assert r.partner_name == "Frank"


# ---------------------------------------------------------------------------
# Route tests: GET partner_queue
# ---------------------------------------------------------------------------


class TestPartnerQueueRoute:
    def test_queue_shows_orphaned_competitor(self, app, db_session, auth_client):
        """GET partner_queue returns 200 and lists Alice as orphaned."""
        t, ev, alice, bob, carol = _setup_orphan_scenario(db_session)
        db_session.commit()

        resp = auth_client.get(f"/scheduling/{t.id}/events/{ev.id}/partner-queue")
        assert resp.status_code == 200
        assert b"Alice" in resp.data

    def test_queue_empty_message_when_no_orphans(self, app, db_session, auth_client):
        """GET partner_queue shows empty message when no orphans exist."""
        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t)
        db_session.commit()

        resp = auth_client.get(f"/scheduling/{t.id}/events/{ev.id}/partner-queue")
        assert resp.status_code == 200
        assert b"No orphaned partners" in resp.data

    def test_queue_404_for_non_partnered_event(self, app, db_session, auth_client):
        """GET partner_queue on a non-partnered event returns 404."""
        from models.event import Event

        t = _make_tournament(db_session)
        ev = Event(
            tournament_id=t.id,
            name="Single Buck",
            event_type="pro",
            scoring_type="time",
            scoring_order="lowest_wins",
            is_partnered=False,
        )
        db_session.add(ev)
        db_session.flush()
        db_session.commit()

        resp = auth_client.get(f"/scheduling/{t.id}/events/{ev.id}/partner-queue")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Route tests: POST reassign_partner
# ---------------------------------------------------------------------------


class TestReassignPartnerRoute:
    def test_happy_path_reassigns_bidirectionally(self, app, db_session, auth_client):
        """POST reassign_partner updates both partners' JSON and redirects."""
        t, ev, alice, bob, carol = _setup_orphan_scenario(db_session)
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/events/{ev.id}/reassign-partner",
            data={
                "orphan_id": str(alice.id),
                "orphan_type": "pro",
                "new_partner_id": str(carol.id),
                "new_partner_type": "pro",
            },
            follow_redirects=False,
        )
        # Must POST-redirect-GET
        assert resp.status_code in (302, 303)

        # Verify bidirectional update
        from models.competitor import ProCompetitor

        alice_fresh = ProCompetitor.query.get(alice.id)
        carol_fresh = ProCompetitor.query.get(carol.id)
        assert alice_fresh.get_partners().get(str(ev.id)) == "Carol"
        assert carol_fresh.get_partners().get(str(ev.id)) == "Alice"

    def test_wrong_gender_flashes_error(self, app, db_session, auth_client):
        """POST reassign_partner with gender mismatch flashes error and does not update."""
        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t, gender_req="mixed")
        alice = _make_pro(
            db_session, t, "Alice", gender="F", partners={str(ev.id): "Bob"}
        )
        _make_pro(
            db_session,
            t,
            "Bob",
            gender="M",
            status="scratched",
            partners={str(ev.id): "Alice"},
        )
        carol = _make_pro(db_session, t, "Carol", gender="F")
        _make_result(db_session, ev, alice, partner_name="Bob")
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/events/{ev.id}/reassign-partner",
            data={
                "orphan_id": str(alice.id),
                "orphan_type": "pro",
                "new_partner_id": str(carol.id),
                "new_partner_type": "pro",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"gender" in resp.data.lower()

        from models.competitor import ProCompetitor

        alice_fresh = ProCompetitor.query.get(alice.id)
        # partner should still be Bob (unchanged)
        assert alice_fresh.get_partners().get(str(ev.id)) == "Bob"

    def test_already_partnered_flashes_error(self, app, db_session, auth_client):
        """POST reassign_partner with already-partnered new partner flashes error."""
        t = _make_tournament(db_session)
        ev = _make_partnered_event(db_session, t, gender_req="any")
        alice = _make_pro(
            db_session, t, "Alice", gender="F", partners={str(ev.id): "Bob"}
        )
        _make_pro(
            db_session,
            t,
            "Bob",
            gender="M",
            status="scratched",
            partners={str(ev.id): "Alice"},
        )
        grace = _make_pro(
            db_session, t, "Grace", gender="F", partners={str(ev.id): "Henry"}
        )
        _make_result(db_session, ev, alice, partner_name="Bob")
        db_session.commit()

        resp = auth_client.post(
            f"/scheduling/{t.id}/events/{ev.id}/reassign-partner",
            data={
                "orphan_id": str(alice.id),
                "orphan_type": "pro",
                "new_partner_id": str(grace.id),
                "new_partner_type": "pro",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"partner" in resp.data.lower()

        from models.competitor import ProCompetitor

        alice_fresh = ProCompetitor.query.get(alice.id)
        assert alice_fresh.get_partners().get(str(ev.id)) == "Bob"

    def test_result_partner_name_updated(self, app, db_session, auth_client):
        """POST reassign_partner updates EventResult.partner_name for the orphan."""
        t, ev, alice, bob, carol = _setup_orphan_scenario(db_session)
        db_session.commit()

        auth_client.post(
            f"/scheduling/{t.id}/events/{ev.id}/reassign-partner",
            data={
                "orphan_id": str(alice.id),
                "orphan_type": "pro",
                "new_partner_id": str(carol.id),
                "new_partner_type": "pro",
            },
            follow_redirects=False,
        )

        from models.event import EventResult

        r = EventResult.query.filter_by(
            event_id=ev.id,
            competitor_id=alice.id,
        ).first()
        assert r is not None
        assert r.partner_name == "Carol"
