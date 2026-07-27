"""Tests for services/schedule_status.py — the Current Schedule panel aggregator."""

import pytest

from database import db
from models.event import Event
from models.heat import Flight, Heat
from models.tournament import Tournament
from services.schedule_status import build_schedule_status
from tests.db_test_utils import create_test_app


@pytest.fixture(scope="module")
def app():
    _app, db_path = create_test_app()
    with _app.app_context():
        yield _app
        db.session.remove()
    import os

    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def tournament(app):
    with app.app_context():
        t = Tournament(name="Status Test", year=2026, status="setup")
        db.session.add(t)
        db.session.commit()
        tid = t.id
        yield t
        # Explicit cascade in FK dependency order: Heat → Flight → Event → Tournament.
        # Flight.id is referenced by Heat.flight_id with no ON DELETE CASCADE,
        # so we can't rely on Tournament cascade alone.
        db.session.rollback()
        event_ids = [e.id for e in Event.query.filter_by(tournament_id=tid).all()]
        if event_ids:
            Heat.query.filter(Heat.event_id.in_(event_ids)).delete(synchronize_session=False)
        Flight.query.filter_by(tournament_id=tid).delete()
        Event.query.filter_by(tournament_id=tid).delete()
        Tournament.query.filter_by(id=tid).delete()
        db.session.commit()


class TestBuildScheduleStatus:
    def test_empty_tournament_is_info(self, app, tournament):
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        assert s["overall_severity"] == "info"
        assert s["overall_label"] == "No events configured yet"
        assert s["friday"]["events_configured"] == 0
        assert s["saturday"]["events_configured"] == 0
        assert s["saturday_flights"] == 0
        assert s["warnings"] == []

    def test_events_without_heats_warns(self, app, tournament):
        with app.app_context():
            ev = Event(
                tournament_id=tournament.id,
                name="Underhand",
                event_type="college",
                gender="M",
                scoring_type="time",
                stand_type="underhand",
                is_open=False,
            )
            db.session.add(ev)
            db.session.commit()
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        assert s["friday"]["events_configured"] == 1
        assert s["friday"]["events_with_heats"] == 0
        assert s["friday"]["heats_total"] == 0
        assert any("no heats yet" in w["title"] for w in s["warnings"])
        assert s["overall_severity"] in ("warning", "info")

    def test_open_college_event_without_heats_not_warned(self, app, tournament):
        """OPEN events (Axe Throw, Caber Toss, etc.) don't use heats — no warning."""
        with app.app_context():
            ev = Event(
                tournament_id=tournament.id,
                name="Axe Throw",
                event_type="college",
                gender="M",
                scoring_type="hits",
                stand_type="axe_throw",
                is_open=True,
            )
            db.session.add(ev)
            db.session.commit()
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        # Open event counts as configured but no "missing heats" warning
        assert s["friday"]["events_configured"] >= 1
        assert not any(
            "no heats" in w["title"] and "college" in w["title"] for w in s["warnings"]
        )

    def test_closed_signup_only_event_not_warned(self, app, tournament):
        """V2.14.2 regression: is_open=False + name in LIST_ONLY_EVENT_NAMES
        should NOT trigger 'no heats yet' warning.

        Operators are allowed to configure traditionally-OPEN events as CLOSED
        on the Event Setup page (CLAUDE.md §3). When they do, the events still
        run come-and-go signup-list format and never produce Heat rows. The
        previous _is_open_list_only(event.is_open) check missed this path
        and produced a phantom warning on every Run Show page load.
        """
        with app.app_context():
            ev = Event(
                tournament_id=tournament.id,
                name="Caber Toss",
                event_type="college",
                gender="M",
                scoring_type="distance",
                stand_type="caber",
                is_open=False,  # the previously-broken path
            )
            db.session.add(ev)
            db.session.commit()
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        assert not any(
            "no heats" in w["title"] and "college" in w["title"]
            for w in s["warnings"]
        ), "Closed signup-only college event should not warn (LIST_ONLY_EVENT_NAMES)"

    def test_state_machine_pro_event_not_warned(self, app, tournament):
        """V2.14.2 regression: Partnered Axe Throw and Pro-Am Relay run on
        state machines stored in Event.payouts JSON, not regular Heat rows.
        Their steady-state heat count is zero — that is not a warning."""
        with app.app_context():
            for name in ("Partnered Axe Throw", "Pro-Am Relay"):
                ev = Event(
                    tournament_id=tournament.id,
                    name=name,
                    event_type="pro",
                    scoring_type="hits",
                    is_open=False,
                )
                db.session.add(ev)
            db.session.commit()
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        assert not any(
            "no heats" in w["title"] and "pro" in w["title"]
            for w in s["warnings"]
        ), "State-machine pro events should not warn (Partnered Axe Throw, Pro-Am Relay)"

    def test_pro_heats_without_flights_warns(self, app, tournament):
        with app.app_context():
            ev = Event(
                tournament_id=tournament.id,
                name="Springboard",
                event_type="pro",
                gender="M",
                scoring_type="time",
                stand_type="springboard",
                is_open=False,
            )
            db.session.add(ev)
            db.session.flush()
            h = Heat(event_id=ev.id, heat_number=1, run_number=1, competitors="[]")
            db.session.add(h)
            db.session.commit()
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        assert s["saturday"]["heats_total"] >= 1
        assert s["saturday_flights"] == 0
        assert any("flights are not built" in w["title"] for w in s["warnings"])

    def test_ready_schedule_shows_success(self, app, tournament):
        """All events have heats and flights exist → success severity."""
        with app.app_context():
            ev = Event(
                tournament_id=tournament.id,
                name="Hot Saw",
                event_type="pro",
                gender="M",
                scoring_type="time",
                stand_type="hot_saw",
                is_open=False,
            )
            db.session.add(ev)
            db.session.flush()

            f = Flight(tournament_id=tournament.id, flight_number=1, name="A")
            db.session.add(f)
            db.session.flush()

            h = Heat(
                event_id=ev.id,
                heat_number=1,
                run_number=1,
                competitors="[1]",
                flight_id=f.id,
                flight_position=1,
            )
            db.session.add(h)
            db.session.commit()

        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        assert s["saturday"]["events_with_heats"] == s["saturday"]["events_configured"]
        assert s["saturday_flights"] == 1
        assert s["saturday_heats_per_flight_avg"] == 1.0
        assert s["overall_severity"] == "success"
        assert s["overall_label"] == "Schedule ready"


class TestEventListRouteRendersPanel:
    def test_route_includes_current_schedule_card(self, app, client):
        """GET /scheduling/<tid>/events renders the Current Schedule panel."""
        with app.app_context():
            t = Tournament(name="Render Test", year=2026, status="setup")
            db.session.add(t)
            db.session.commit()
            tid = t.id

            # Log in as a judge so the route returns 200
            from models.user import User

            u = User(username="panel_test_admin", role="admin")
            u.set_password("testpass123")
            db.session.add(u)
            db.session.commit()
            uid = u.id

        with client.session_transaction() as sess:
            sess["_user_id"] = str(uid)
            sess["_fresh"] = True

        r = client.get(f"/scheduling/{tid}/events")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Current Schedule" in html, "status panel missing from events.html"
        assert "No events configured yet" in html


class TestWarningsCarrySubmitAction:
    """Regression tests for the V2.14.x bug where the 'Generate pro heats'
    button in the schedule status panel was a hyperlink to scheduling.event_list
    — i.e. the page the operator was already on. Clicking it just reloaded
    the page with no generation triggered, which the operator (correctly)
    perceived as a broken button.

    The fix: actionable warnings now carry a ``submit_action`` field that the
    template uses to render a POST <form> button submitting that action to
    scheduling.event_list. One click runs the operation it advertises.
    """

    def test_pro_missing_heats_carries_submit_action(self, app, tournament):
        with app.app_context():
            ev = Event(
                tournament_id=tournament.id,
                name="Springboard",
                event_type="pro",
                gender="M",
                scoring_type="time",
                stand_type="springboard",
                is_open=False,
            )
            db.session.add(ev)
            db.session.commit()
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        pro_warning = next(
            (w for w in s["warnings"] if "pro event" in w["title"]), None
        )
        assert pro_warning is not None
        assert pro_warning.get("submit_action") == "generate_all", (
            "pro_missing warning must carry submit_action='generate_all' so the "
            "warning button actually generates instead of reloading the page"
        )

    def test_college_missing_heats_carries_submit_action(self, app, tournament):
        with app.app_context():
            ev = Event(
                tournament_id=tournament.id,
                name="Underhand",
                event_type="college",
                gender="M",
                scoring_type="time",
                stand_type="underhand",
                is_open=False,
            )
            db.session.add(ev)
            db.session.commit()
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        college_warning = next(
            (w for w in s["warnings"] if "college event" in w["title"]), None
        )
        assert college_warning is not None
        assert college_warning.get("submit_action") == "generate_all"

    def test_pro_heats_without_flights_carries_rebuild_action(self, app, tournament):
        with app.app_context():
            ev = Event(
                tournament_id=tournament.id,
                name="Springboard",
                event_type="pro",
                gender="M",
                scoring_type="time",
                stand_type="springboard",
                is_open=False,
            )
            db.session.add(ev)
            db.session.flush()
            h = Heat(event_id=ev.id, heat_number=1, run_number=1, competitors="[]")
            db.session.add(h)
            db.session.commit()
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        flights_warning = next(
            (w for w in s["warnings"] if "flights are not built" in w["title"]), None
        )
        assert flights_warning is not None
        assert flights_warning.get("submit_action") == "rebuild_flights"

    def test_template_renders_form_button_when_submit_action_set(self, app, client):
        """The events.html template must render an actionable warning as a POST
        form button (not a hyperlink) so clicking it triggers generation.
        """
        with app.app_context():
            t = Tournament(name="Submit Action Render Test", year=2026, status="setup")
            db.session.add(t)
            db.session.flush()
            ev = Event(
                tournament_id=t.id,
                name="Springboard",
                event_type="pro",
                gender="M",
                scoring_type="time",
                stand_type="springboard",
                is_open=False,
            )
            db.session.add(ev)
            db.session.commit()
            tid = t.id

            from models.user import User
            u = User(username="submit_action_admin", role="admin")
            u.set_password("testpass123")
            db.session.add(u)
            db.session.commit()
            uid = u.id

        with client.session_transaction() as sess:
            sess["_user_id"] = str(uid)
            sess["_fresh"] = True

        r = client.get(f"/scheduling/{tid}/events")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        # The warning must render as a POST form with the action field set.
        # A bare <a href> would mean the click bug is back.
        assert 'name="action" value="generate_all"' in html, (
            "actionable warning must render as a POST submit button so a single "
            "click triggers generation"
        )
