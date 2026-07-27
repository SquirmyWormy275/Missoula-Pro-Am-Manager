"""Phase 1 — DQ status + status_reason field tests (Judge Sheet feature)."""

from __future__ import annotations

import json

import pytest

from database import db
from models.event import EventResult
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_event_result,
    make_heat,
    make_team,
    make_tournament,
)


class TestStatusReasonPersistence:
    def test_dq_status_round_trips(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        comp = make_college_competitor(db_session, t, team, "DQ Dan")
        event = make_event(db_session, t, "Men's Underhand", event_type="college")

        result = make_event_result(
            db_session,
            event,
            comp,
            competitor_type="college",
            status="dq",
        )
        result.status_reason = "illegal axe"
        db_session.flush()

        fresh = EventResult.query.get(result.id)
        assert fresh.status == "dq"
        assert fresh.status_reason == "illegal axe"

    def test_dnf_status_with_reason(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        comp = make_college_competitor(db_session, t, team, "DNF Dave")
        event = make_event(db_session, t, "Single Buck", event_type="college")

        result = make_event_result(
            db_session,
            event,
            comp,
            competitor_type="college",
            status="dnf",
        )
        result.status_reason = "injury"
        db_session.flush()

        fresh = EventResult.query.get(result.id)
        assert fresh.status == "dnf"
        assert fresh.status_reason == "injury"

    def test_status_reason_nullable_by_default(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        comp = make_college_competitor(db_session, t, team, "Clean Carl")
        event = make_event(db_session, t, "Stock Saw", event_type="college")

        result = make_event_result(
            db_session,
            event,
            comp,
            competitor_type="college",
            status="completed",
            result_value=12.34,
        )
        db_session.flush()

        fresh = EventResult.query.get(result.id)
        assert fresh.status == "completed"
        assert fresh.status_reason is None


def _make_unique_admin(db_session, suffix):
    """Create an admin user with a unique username so POST-committed state
    from a prior test doesn't collide with the next test's fixture insert."""
    from models.user import User
    u = User(username=f'dq_admin_{suffix}', role='admin')
    u.set_password('testpass')
    db_session.add(u)
    db_session.flush()
    db_session.commit()  # persist so route's login-load finds it
    return u


class TestScoringSubmissionAcceptsDq:
    """POST to enter_heat_results with status=dq + reason populates the column."""

    def test_scoring_post_saves_dq_and_reason(self, app, db_session):
        admin_user = _make_unique_admin(db_session, 'dq')
        # Seed: tournament, team, one competitor, one Hard-Hit event (single input
        # path — no dual-timer fields required).
        t = make_tournament(db_session)
        team = make_team(db_session, t, code="UM-A", school="University of Montana")
        comp = make_college_competitor(db_session, t, team, "Test Competitor")

        # Hard-Hit event — scoring_type='hits' uses the single-input legacy path
        # which is the simplest to drive from a form POST.
        event = make_event(
            db_session,
            t,
            "Underhand Hard Hit",
            event_type="college",
            scoring_type="hits",
            scoring_order="highest_wins",
            stand_type="underhand",
            max_stands=5,
        )
        heat = make_heat(
            db_session,
            event,
            heat_number=1,
            run_number=1,
            competitors=[comp.id],
            stand_assignments={str(comp.id): 1},
        )
        db_session.commit()

        c = app.test_client()
        with c.session_transaction() as sess:
            sess["_user_id"] = str(admin_user.id)

        resp = c.post(
            f"/scoring/{t.id}/heat/{heat.id}/enter",
            data={
                "heat_version": str(heat.version_id),
                f"result_{comp.id}": "7",
                f"status_{comp.id}": "dq",
                f"reason_{comp.id}": "stepped out of stand",
            },
            follow_redirects=False,
        )
        # Accept either 302 (redirect to next action) or 200 (re-render).
        assert resp.status_code in (200, 302), resp.data[:400]

        # Re-fetch (commit-level) — the route committed the transaction.
        row = EventResult.query.filter_by(
            event_id=event.id, competitor_id=comp.id
        ).one()
        assert row.status == "dq"
        assert row.status_reason == "stepped out of stand"

        # Cleanup: route committed outside our transactional rollback — manual wipe.
        EventResult.query.filter_by(event_id=event.id).delete()
        db.session.commit()

    def test_scoring_post_clears_reason_when_completed(self, app, db_session):
        """status=completed must wipe any stale reason."""
        admin_user = _make_unique_admin(db_session, "clear")
        t = make_tournament(db_session)
        team = make_team(db_session, t, code="UM-B", school="University of Montana")
        comp = make_college_competitor(db_session, t, team, "Clear Reason Carl")
        event = make_event(
            db_session,
            t,
            "Standing Block Hard Hit",
            event_type="college",
            scoring_type="hits",
            scoring_order="highest_wins",
            stand_type="standing_block",
            max_stands=5,
        )
        heat = make_heat(
            db_session,
            event,
            heat_number=1,
            run_number=1,
            competitors=[comp.id],
            stand_assignments={str(comp.id): 1},
        )
        # Pre-seed a stale DQ reason to verify it gets cleared.
        make_event_result(
            db_session,
            event,
            comp,
            competitor_type="college",
            result_value=5,
            status="dq",
        )
        # Reload + set reason, commit so route sees it.
        prior = EventResult.query.filter_by(
            event_id=event.id, competitor_id=comp.id
        ).one()
        prior.status_reason = "stale reason"
        db_session.commit()

        c = app.test_client()
        with c.session_transaction() as sess:
            sess["_user_id"] = str(admin_user.id)

        # Re-fetch heat for current version_id after commit.
        from models.heat import Heat

        heat = Heat.query.get(heat.id)

        resp = c.post(
            f"/scoring/{t.id}/heat/{heat.id}/enter",
            data={
                "heat_version": str(heat.version_id),
                f"result_{comp.id}": "8",
                f"status_{comp.id}": "completed",
                f"reason_{comp.id}": "this should be cleared",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 302), resp.data[:400]

        row = EventResult.query.filter_by(
            event_id=event.id, competitor_id=comp.id
        ).one()
        assert row.status == "completed"
        assert row.status_reason is None

        EventResult.query.filter_by(event_id=event.id).delete()
        db.session.commit()
