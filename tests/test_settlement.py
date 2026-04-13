"""
Unit 10 — Payout settlement toggle tests.

Tests the toggle_settlement route on scoring_bp which flips
EventResult.payout_settled by result ID (per-result granularity).

Run: pytest tests/test_settlement.py -v
"""

from __future__ import annotations

import json
import os

import pytest

from database import db as _db

# ---------------------------------------------------------------------------
# Self-contained app + admin fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
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

    if not User.query.filter_by(username="settle_admin").first():
        u = User(username="settle_admin", role="admin")
        u.set_password("settle_pass")
        _db.session.add(u)
        _db.session.commit()


@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        yield _db.session


@pytest.fixture()
def auth_client(app):
    c = app.test_client()
    with app.app_context():
        c.post(
            "/auth/login",
            data={"username": "settle_admin", "password": "settle_pass"},
            follow_redirects=True,
        )
    return c


@pytest.fixture()
def anon_client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _make_tournament(session):
    from models import Tournament

    t = Tournament(name="Settlement Test 2026", year=2026, status="active")
    session.add(t)
    session.flush()
    return t


def _make_pro_competitor(session, tournament, name="Alice Pro"):
    from models.competitor import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender="F",
        events_entered=json.dumps([]),
        gear_sharing=json.dumps({}),
        partners=json.dumps({}),
        status="active",
    )
    session.add(c)
    session.flush()
    return c


def _make_event(session, tournament, name="Pro Underhand"):
    from models.event import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="pro",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="underhand",
        max_stands=4,
        payouts=json.dumps({"1": 500.0, "2": 300.0, "3": 150.0}),
        status="completed",
        is_finalized=True,
    )
    session.add(e)
    session.flush()
    return e


def _make_result(session, event, competitor, payout_amount=500.0, payout_settled=False):
    from models.event import EventResult

    r = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type="pro",
        competitor_name=competitor.name,
        result_value=10.5,
        final_position=1,
        payout_amount=payout_amount,
        payout_settled=payout_settled,
        status="completed",
    )
    session.add(r)
    session.flush()
    return r


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestToggleSettlementHappyPath:
    def test_toggle_unsettled_to_settled(self, app, auth_client, db_session):
        """Toggle an unsettled result → payout_settled becomes True, JSON ok=true."""
        t = _make_tournament(db_session)
        comp = _make_pro_competitor(db_session, t)
        event = _make_event(db_session, t)
        result = _make_result(db_session, event, comp, payout_settled=False)
        _db.session.commit()

        with app.app_context():
            resp = auth_client.post(
                f"/scoring/tournament/{t.id}/result/{result.id}/toggle-settled",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert data["settled"] is True

        with app.app_context():
            from models.event import EventResult

            fresh = EventResult.query.get(result.id)
            assert fresh.payout_settled is True

    def test_toggle_settled_to_unsettled(self, app, auth_client, db_session):
        """Toggle an already-settled result → payout_settled becomes False."""
        t = _make_tournament(db_session)
        comp = _make_pro_competitor(db_session, t, name="Bob Pro")
        event = _make_event(db_session, t, name="Pro Springboard")
        result = _make_result(db_session, event, comp, payout_settled=True)
        _db.session.commit()

        with app.app_context():
            resp = auth_client.post(
                f"/scoring/tournament/{t.id}/result/{result.id}/toggle-settled",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert data["settled"] is False

        with app.app_context():
            from models.event import EventResult

            fresh = EventResult.query.get(result.id)
            assert fresh.payout_settled is False


class TestToggleSettlementEdgeCases:
    def test_toggle_zero_payout_result_succeeds(self, app, auth_client, db_session):
        """Result with payout_amount == 0 toggles without error."""
        t = _make_tournament(db_session)
        comp = _make_pro_competitor(db_session, t, name="Zero Charlie")
        event = _make_event(db_session, t, name="Pro Chainsaw")
        result = _make_result(
            db_session, event, comp, payout_amount=0.0, payout_settled=False
        )
        _db.session.commit()

        with app.app_context():
            resp = auth_client.post(
                f"/scoring/tournament/{t.id}/result/{result.id}/toggle-settled",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert data["settled"] is True

    def test_non_ajax_redirects_on_success(self, app, auth_client, db_session):
        """Non-AJAX POST (no XMLHttpRequest header) redirects instead of JSON."""
        t = _make_tournament(db_session)
        comp = _make_pro_competitor(db_session, t, name="Dave Pro")
        event = _make_event(db_session, t, name="Pro Standing Block")
        result = _make_result(db_session, event, comp, payout_settled=False)
        _db.session.commit()

        with app.app_context():
            resp = auth_client.post(
                f"/scoring/tournament/{t.id}/result/{result.id}/toggle-settled",
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303)


class TestToggleSettlementAuth:
    def test_wrong_tournament_returns_404(self, app, auth_client, db_session):
        """Result that belongs to a different tournament → 404."""
        t1 = _make_tournament(db_session)
        t2 = _make_tournament(db_session)
        comp = _make_pro_competitor(db_session, t1, name="Eve Pro")
        event = _make_event(db_session, t1, name="Pro Underhand M")
        result = _make_result(db_session, event, comp)
        _db.session.flush()

        with app.app_context():
            # Use t2.id but the result belongs to t1
            resp = auth_client.post(
                f"/scoring/tournament/{t2.id}/result/{result.id}/toggle-settled",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )

        assert resp.status_code in (403, 404)

    def test_unauthenticated_redirects_to_login(self, app, anon_client, db_session):
        """Unauthenticated user is redirected (302) not given 200."""
        t = _make_tournament(db_session)
        comp = _make_pro_competitor(db_session, t, name="Frank Pro")
        event = _make_event(db_session, t, name="Pro Birling")
        result = _make_result(db_session, event, comp)
        _db.session.flush()

        with app.app_context():
            resp = anon_client.post(
                f"/scoring/tournament/{t.id}/result/{result.id}/toggle-settled",
                headers={"X-Requested-With": "XMLHttpRequest"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 401)


class TestSettlementSummaryTotals:
    def test_summary_totals_are_correct(self, app, db_session):
        """Verify total_owed / total_settled / total_outstanding arithmetic."""
        t = _make_tournament(db_session)
        event = _make_event(db_session, t)

        comp1 = _make_pro_competitor(db_session, t, name="G1")
        comp2 = _make_pro_competitor(db_session, t, name="G2")
        comp3 = _make_pro_competitor(db_session, t, name="G3")

        _make_result(db_session, event, comp1, payout_amount=500.0, payout_settled=True)
        _make_result(db_session, event, comp2, payout_amount=300.0, payout_settled=True)
        _make_result(
            db_session, event, comp3, payout_amount=150.0, payout_settled=False
        )
        _db.session.commit()

        from models.event import EventResult

        results = EventResult.query.filter_by(event_id=event.id).all()
        total_owed = sum(r.payout_amount for r in results)
        total_settled = sum(r.payout_amount for r in results if r.payout_settled)
        total_outstanding = total_owed - total_settled

        assert total_owed == pytest.approx(950.0)
        assert total_settled == pytest.approx(800.0)
        assert total_outstanding == pytest.approx(150.0)
