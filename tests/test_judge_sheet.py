"""Phase 2 — Judge sheet utility + routes (blank recording forms)."""

from __future__ import annotations

import pytest

from tests.conftest import (
    make_tournament,
    make_team,
    make_college_competitor,
    make_pro_competitor,
    make_event,
    make_heat,
)


class TestGetEventHeatsForJudging:
    def test_single_run_event_shape(self, db_session):
        from services.judge_sheet import get_event_heats_for_judging

        t = make_tournament(db_session)
        team = make_team(db_session, t, code="UM-A", school="University of Montana")
        a = make_college_competitor(db_session, t, team, "Alice", gender="F")
        b = make_college_competitor(db_session, t, team, "Bob", gender="M")
        event = make_event(
            db_session,
            t,
            "Stock Saw",
            event_type="college",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="stock_saw",
            max_stands=2,
        )
        make_heat(
            db_session,
            event,
            heat_number=1,
            run_number=1,
            competitors=[a.id, b.id],
            stand_assignments={str(a.id): 1, str(b.id): 2},
        )
        db_session.flush()

        data = get_event_heats_for_judging(event.id)
        assert data is not None
        assert data["num_runs"] == 1
        assert data["scoring_type"] == "timed"
        assert len(data["heats"]) == 1
        assert data["heats"][0]["heat_number"] == 1
        names = [c["name"] for c in data["heats"][0]["competitors"]]
        assert names == ["Alice", "Bob"]
        # College competitor team codes must come through.
        assert all(c["team_code"] == "UM-A" for c in data["heats"][0]["competitors"])

    def test_dual_run_event_reports_num_runs_two(self, db_session):
        from services.judge_sheet import get_event_heats_for_judging

        t = make_tournament(db_session)
        team = make_team(db_session, t)
        a = make_college_competitor(db_session, t, team, "Climber One", gender="M")
        event = make_event(
            db_session,
            t,
            "Speed Climb",
            event_type="college",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="speed_climb",
            max_stands=2,
            requires_dual_runs=True,
        )
        # Two Heat rows are created for dual-run events (run 1 and run 2).
        make_heat(db_session, event, heat_number=1, run_number=1, competitors=[a.id])
        make_heat(db_session, event, heat_number=1, run_number=2, competitors=[a.id])
        db_session.flush()

        data = get_event_heats_for_judging(event.id)
        assert data["num_runs"] == 2
        # Run-2 rows are filtered out so the same competitor doesn't appear twice
        # on the printed sheet.
        assert len(data["heats"]) == 1

    def test_triple_run_event_reports_num_runs_three(self, db_session):
        from services.judge_sheet import get_event_heats_for_judging

        t = make_tournament(db_session)
        team = make_team(db_session, t)
        a = make_college_competitor(db_session, t, team, "Thrower", gender="M")
        event = make_event(
            db_session,
            t,
            "Axe Throw",
            event_type="college",
            scoring_type="score",
            scoring_order="highest_wins",
            stand_type="axe_throw",
            max_stands=1,
            requires_triple_runs=True,
        )
        make_heat(db_session, event, heat_number=1, run_number=1, competitors=[a.id])
        db_session.flush()

        data = get_event_heats_for_judging(event.id)
        assert data["num_runs"] == 3
        assert data["scoring_type"] == "scored"

    def test_pro_event_has_no_team_code(self, db_session):
        from services.judge_sheet import get_event_heats_for_judging

        t = make_tournament(db_session)
        pro = make_pro_competitor(db_session, t, "Pro Jack")
        event = make_event(
            db_session,
            t,
            "Pro Underhand",
            event_type="pro",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="underhand",
            max_stands=5,
        )
        make_heat(db_session, event, heat_number=1, run_number=1, competitors=[pro.id])
        db_session.flush()

        data = get_event_heats_for_judging(event.id)
        assert data["heats"][0]["competitors"][0]["team_code"] is None
        assert data["heats"][0]["competitors"][0]["name"] == "Pro Jack"

    def test_event_with_no_heats_returns_empty_list(self, db_session):
        from services.judge_sheet import get_event_heats_for_judging

        t = make_tournament(db_session)
        event = make_event(
            db_session,
            t,
            "Orphan Event",
            event_type="pro",
            scoring_type="time",
        )
        db_session.flush()

        data = get_event_heats_for_judging(event.id)
        assert data is not None
        assert data["heats"] == []


def _make_admin_and_client(app, db_session, suffix):
    """Create a unique admin + return a client logged in as that admin.

    The scoring blueprint is in MANAGEMENT_BLUEPRINTS so every route requires
    an authenticated judge/admin — anonymous hits redirect to /auth/login.
    """
    from models.user import User
    u = User(username=f"judge_sheet_{suffix}", role="admin")
    u.set_password("testpass")
    db_session.add(u)
    db_session.flush()
    db_session.commit()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
    return c


class TestJudgeSheetRoutes:
    def test_single_event_route_returns_pdf_or_html(self, app, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        a = make_college_competitor(db_session, t, team, "Route Tester", gender="M")
        event = make_event(
            db_session,
            t,
            "Single Buck",
            event_type="college",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="saw_hand",
            max_stands=4,
        )
        make_heat(db_session, event, heat_number=1, run_number=1, competitors=[a.id])
        db_session.commit()

        c = _make_admin_and_client(app, db_session, "single")
        resp = c.get(f"/scoring/{t.id}/event/{event.id}/judge-sheet")
        assert resp.status_code == 200
        assert resp.content_type in (
            "application/pdf",
            "text/html",
            "text/html; charset=utf-8",
        )
        # HTML fallback must contain the event name; PDF will not, so check HTML path only.
        if "text/html" in resp.content_type:
            assert b"Single Buck" in resp.data
            assert b"Heat 1" in resp.data
            assert b"Route Tester" in resp.data

    def test_all_sheets_route_skips_events_without_heats(self, app, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        a = make_college_competitor(db_session, t, team, "All Sheets Alice")

        event_with_heats = make_event(
            db_session,
            t,
            "Underhand",
            event_type="college",
            scoring_type="time",
            stand_type="underhand",
            max_stands=5,
        )
        make_heat(
            db_session,
            event_with_heats,
            heat_number=1,
            run_number=1,
            competitors=[a.id],
        )
        # Event with NO heats — must be skipped without erroring.
        make_event(
            db_session,
            t,
            "Empty Stocksaw",
            event_type="college",
            scoring_type="time",
            stand_type="stock_saw",
            max_stands=2,
        )
        db_session.commit()

        c = _make_admin_and_client(app, db_session, "all")
        resp = c.get(f"/scoring/{t.id}/judge-sheets/all")
        assert resp.status_code == 200
        if "text/html" in resp.content_type:
            assert b"Underhand" in resp.data
            # Empty event rendered with a "no heats" message would still leak
            # the event name; the route is supposed to skip it entirely.
            assert b"Empty Stocksaw" not in resp.data

    def test_all_sheets_route_with_zero_eligible_events_flashes_and_redirects(
        self,
        app,
        db_session,
    ):
        t = make_tournament(db_session)
        # No events, no heats at all.
        db_session.commit()

        c = _make_admin_and_client(app, db_session, "empty")
        resp = c.get(f"/scoring/{t.id}/judge-sheets/all", follow_redirects=False)
        assert resp.status_code == 302
