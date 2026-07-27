"""
Phase 4: Pro-Am Relay placement in final flight + printable teams sheet.

Consolidates the originally-planned 4 files into one:
  * test_proam_relay_placement.py (drawn relay → one pseudo-heat in final flight)
  * test_proam_relay_no_teams.py (undrawn → placed=False, no heat)
  * test_proam_relay_rebuild_idempotent.py (re-run doesn't duplicate)
  * test_relay_teams_sheet.py (print route renders with team names)

Run:  pytest tests/test_proam_relay_placement.py -v
"""

from __future__ import annotations

import json

import pytest

from database import db as _db


@pytest.fixture(scope="module")
def app():
    import os

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

    if not User.query.filter_by(username="relay_admin").first():
        u = User(username="relay_admin", role="admin")
        u.set_password("relay_pass")
        _db.session.add(u)
        _db.session.commit()


@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post(
        "/auth/login",
        data={"username": "relay_admin", "password": "relay_pass"},
        follow_redirects=True,
    )
    return c


def _make_tournament(session, name="Relay Placement 2026"):
    from models import Tournament

    t = Tournament(name=name, year=2026, status="pro_active")
    session.add(t)
    session.flush()
    return t


def _make_pro_event(session, tournament, name, stand_type, gender=None, max_stands=4):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="pro",
        gender=gender,
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type=stand_type,
        max_stands=max_stands,
    )
    session.add(e)
    session.flush()
    return e


def _make_heat(session, event, heat_number, run_number=1):
    from models import Heat

    h = Heat(event_id=event.id, heat_number=heat_number, run_number=run_number)
    h.set_competitors([])
    session.add(h)
    session.flush()
    return h


def _make_pro(session, tournament, name, gender="M"):
    from models import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id, name=name, gender=gender, status="active"
    )
    session.add(c)
    session.flush()
    return c


def _seed_with_flights(session, with_relay_event=True, with_teams=False, status="drawn"):
    """Tournament with 2 pro flights and (optionally) a Pro-Am Relay in the
    given lottery status (drawn / in_progress / completed)."""
    from models import Event
    from services.flight_builder import build_pro_flights

    t = _make_tournament(session)
    pros = [_make_pro(session, t, f"Pro {i}") for i in range(1, 7)]
    ev_sb = _make_pro_event(session, t, "Springboard", "springboard")
    ev_uh = _make_pro_event(
        session, t, "Underhand", "underhand", gender="M", max_stands=5
    )
    for n in range(1, 3):
        _make_heat(session, ev_sb, n)
        _make_heat(session, ev_uh, n)

    build_pro_flights(t, num_flights=2, commit=False)

    if with_relay_event:
        relay = Event(
            tournament_id=t.id,
            name="Pro-Am Relay",
            event_type="pro",
            scoring_type="time",
            is_partnered=True,
            status="pending",
        )
        if with_teams:
            # Mirror the real ProAmRelay.run_lottery() shape:
            # {'pro_members': [...], 'college_members': [...]}, NOT a combined
            # 'members' list. Codex caught this divergence post-merge.
            relay_data = {
                "status": status,
                "teams": [
                    {
                        "team_number": 1,
                        "pro_members": [
                            {"id": pros[0].id, "name": pros[0].name, "gender": "M"},
                            {"id": pros[1].id, "name": pros[1].name, "gender": "M"},
                        ],
                        "college_members": [],
                    },
                    {
                        "team_number": 2,
                        "pro_members": [
                            {"id": pros[2].id, "name": pros[2].name, "gender": "M"},
                            {"id": pros[3].id, "name": pros[3].name, "gender": "M"},
                        ],
                        "college_members": [],
                    },
                ],
            }
            relay.event_state = json.dumps(relay_data)
        else:
            relay.event_state = json.dumps({"status": "not_drawn", "teams": []})
        session.add(relay)
        session.flush()

    return t, pros


# ---------------------------------------------------------------------------
# Placement behavior
# ---------------------------------------------------------------------------


class TestRelayPlacement:
    def test_drawn_relay_places_pseudo_heat_in_final_flight(self, db_session):
        from models import Flight, Heat
        from services.flight_builder import integrate_proam_relay_into_final_flight

        t, _ = _seed_with_flights(db_session, with_relay_event=True, with_teams=True)
        result = integrate_proam_relay_into_final_flight(t, commit=False)

        assert result["placed"] is True
        assert result["team_count"] == 2
        assert result["heat_id"] is not None

        flights = (
            Flight.query.filter_by(tournament_id=t.id)
            .order_by(Flight.flight_number)
            .all()
        )
        last = flights[-1]
        assert result["flight_id"] == last.id

        relay_heats = Heat.query.filter_by(id=result["heat_id"]).all()
        assert len(relay_heats) == 1
        assert relay_heats[0].flight_id == last.id
        assert relay_heats[0].flight_position == (
            Heat.query.filter_by(flight_id=last.id).count()
        )

    def test_undrawn_relay_returns_placed_false(self, db_session):
        from services.flight_builder import integrate_proam_relay_into_final_flight

        t, _ = _seed_with_flights(db_session, with_relay_event=True, with_teams=False)
        result = integrate_proam_relay_into_final_flight(t, commit=False)
        assert result["placed"] is False
        assert result["reason"] == "not_drawn"

    def test_no_relay_event_returns_placed_false(self, db_session):
        from services.flight_builder import integrate_proam_relay_into_final_flight

        t, _ = _seed_with_flights(db_session, with_relay_event=False)
        result = integrate_proam_relay_into_final_flight(t, commit=False)
        assert result["placed"] is False
        assert result["reason"] == "no_relay_event"

    def test_no_flights_returns_placed_false(self, db_session):
        from models import Event
        from services.flight_builder import integrate_proam_relay_into_final_flight

        t = _make_tournament(db_session)
        relay = Event(
            tournament_id=t.id,
            name="Pro-Am Relay",
            event_type="pro",
            scoring_type="time",
            is_partnered=True,
            status="pending",
            event_state=json.dumps(
                {
                    "status": "drawn",
                    "teams": [{"team_number": 1, "members": []}],
                }
            ),
        )
        db_session.add(relay)
        db_session.flush()

        result = integrate_proam_relay_into_final_flight(t, commit=False)
        assert result["placed"] is False
        assert result["reason"] == "no_flights"

    def test_rebuild_is_idempotent(self, db_session):
        """Running twice must not produce two relay heats."""
        from models import Event, Heat
        from services.flight_builder import integrate_proam_relay_into_final_flight

        t, _ = _seed_with_flights(db_session, with_relay_event=True, with_teams=True)
        integrate_proam_relay_into_final_flight(t, commit=False)
        integrate_proam_relay_into_final_flight(t, commit=False)

        relay_event = Event.query.filter_by(
            tournament_id=t.id, name="Pro-Am Relay"
        ).first()
        relay_heats = Heat.query.filter_by(event_id=relay_event.id).all()
        assert len(relay_heats) == 1, (
            f"idempotency regression: got {len(relay_heats)} relay heats after "
            "2 invocations"
        )


# ---------------------------------------------------------------------------
# Chain ordering — relay lands BEFORE Chokerman (locked decision #4)
# ---------------------------------------------------------------------------


class TestRelayVsChokermanOrdering:
    def test_relay_is_before_chokerman_run2_in_last_flight(self, db_session):
        """Chokerman Run 2 must run AFTER the relay pseudo-heat."""
        from models import Flight, Heat
        from services.flight_builder import (
            integrate_college_spillover_into_flights,
            integrate_proam_relay_into_final_flight,
        )

        t, _ = _seed_with_flights(db_session, with_relay_event=True, with_teams=True)

        # Add a Chokerman college event with Run 2 heats.
        from models import Event

        chokerman = Event(
            tournament_id=t.id,
            name="Chokerman's Race",
            event_type="college",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="chokerman",
            requires_dual_runs=True,
        )
        db_session.add(chokerman)
        db_session.flush()
        for n in range(1, 3):
            _make_heat(db_session, chokerman, n, run_number=1)
            _make_heat(db_session, chokerman, n, run_number=2)

        # Relay FIRST, then spillover — same order as production chain.
        integrate_proam_relay_into_final_flight(t, commit=False)
        integrate_college_spillover_into_flights(t, college_event_ids=[], commit=False)

        flights = (
            Flight.query.filter_by(tournament_id=t.id)
            .order_by(Flight.flight_number)
            .all()
        )
        last = flights[-1]

        last_flight_heats = (
            Heat.query.filter_by(flight_id=last.id)
            .order_by(
                Heat.flight_position,
            )
            .all()
        )
        relay_positions = [
            h.flight_position
            for h in last_flight_heats
            if h.event_id != chokerman.id
            and Event.query.get(h.event_id).name == "Pro-Am Relay"
        ]
        chokerman_positions = [
            h.flight_position for h in last_flight_heats if h.event_id == chokerman.id
        ]

        assert relay_positions, "relay heat should be in the last flight"
        assert chokerman_positions, "chokerman run 2 heats should be in the last flight"
        assert max(relay_positions) < min(chokerman_positions), (
            f"Chokerman must close the show. Got relay positions {relay_positions}, "
            f"chokerman positions {chokerman_positions}."
        )


# ---------------------------------------------------------------------------
# Printable teams sheet
# ---------------------------------------------------------------------------


class TestRelayTeamsSheetRoute:
    def test_renders_200_when_drawn(self, db_session, client):
        t, pros = _seed_with_flights(db_session, with_relay_event=True, with_teams=True)
        _db.session.commit()

        resp = client.get(f"/scheduling/{t.id}/relay-teams-sheet")
        assert resp.status_code == 200
        assert resp.content_type.startswith(
            ("application/pdf", "text/html")
        ), f"unexpected content-type {resp.content_type}"
        # If HTML fallback, team names should be in the body.
        if "text/html" in resp.content_type:
            body = resp.data.decode("utf-8", errors="ignore")
            assert "Pro 1" in body
            assert "Pro 2" in body
            assert "Team 1" in body or "Team" in body

    def test_renders_200_when_undrawn(self, db_session, client):
        """Sheet still renders with an 'undrawn' note rather than 500."""
        t, _ = _seed_with_flights(db_session, with_relay_event=True, with_teams=False)
        _db.session.commit()

        resp = client.get(f"/scheduling/{t.id}/relay-teams-sheet")
        assert resp.status_code == 200
        if "text/html" in resp.content_type:
            body = resp.data.decode("utf-8", errors="ignore")
            assert "not been drawn" in body.lower() or "not drawn" in body.lower()

    def test_renders_200_without_relay_event(self, db_session, client):
        t, _ = _seed_with_flights(db_session, with_relay_event=False)
        _db.session.commit()

        resp = client.get(f"/scheduling/{t.id}/relay-teams-sheet")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Codex post-merge P2: relay survives past the 'drawn' status
# ---------------------------------------------------------------------------


class TestRelayStatusTransitions:
    """Locked fix for codex finding: relay pseudo-heat must be re-placeable
    on rebuild after scoring has started. The state machine is
    not_drawn → drawn → in_progress → completed. Only 'not_drawn' (or
    missing/empty teams) should skip placement."""

    def test_in_progress_status_still_places(self, db_session):
        from services.flight_builder import integrate_proam_relay_into_final_flight

        t, _ = _seed_with_flights(
            db_session, with_relay_event=True, with_teams=True, status="in_progress",
        )
        result = integrate_proam_relay_into_final_flight(t, commit=False)
        assert result["placed"] is True, (
            "Regression: an in_progress relay must not disappear from the "
            "flight sheet when flights are rebuilt mid-show."
        )

    def test_completed_status_still_places(self, db_session):
        from services.flight_builder import integrate_proam_relay_into_final_flight

        t, _ = _seed_with_flights(
            db_session, with_relay_event=True, with_teams=True, status="completed",
        )
        result = integrate_proam_relay_into_final_flight(t, commit=False)
        assert result["placed"] is True

    def test_not_drawn_still_skips(self, db_session):
        """Baseline guard: not_drawn still short-circuits placement."""
        from services.flight_builder import integrate_proam_relay_into_final_flight

        t, _ = _seed_with_flights(db_session, with_relay_event=True, with_teams=False)
        result = integrate_proam_relay_into_final_flight(t, commit=False)
        assert result["placed"] is False
        assert result["reason"] == "not_drawn"

    def test_rebuild_mid_show_reattaches_heat(self, db_session):
        """Simulate the exact codex scenario: run placement while 'drawn',
        then delete the pseudo-heat (as build_pro_flights would when rebuilding),
        bump status to 'in_progress', and re-run placement. The relay must
        land in the final flight again."""
        from models import Event, Heat
        from services.flight_builder import integrate_proam_relay_into_final_flight

        t, _ = _seed_with_flights(
            db_session, with_relay_event=True, with_teams=True, status="drawn",
        )
        first = integrate_proam_relay_into_final_flight(t, commit=False)
        assert first["placed"] is True

        # Mimic build_pro_flights wiping Heat.flight_id for relay heat + bump status.
        relay_event = Event.query.filter_by(
            tournament_id=t.id, name="Pro-Am Relay",
        ).first()
        Heat.query.filter_by(event_id=relay_event.id).delete(synchronize_session=False)
        state = json.loads(relay_event.event_state or "{}")
        state["status"] = "in_progress"
        relay_event.event_state = json.dumps(state)
        db_session.flush()

        second = integrate_proam_relay_into_final_flight(t, commit=False)
        assert second["placed"] is True, (
            "Rebuild mid-show lost the relay — codex P2 regression."
        )


# ---------------------------------------------------------------------------
# Codex post-merge P2: teams sheet must render real member names
# ---------------------------------------------------------------------------


class TestRelayTeamsSheetRendersRealShape:
    """ProAmRelay.run_lottery() stores team members as pro_members +
    college_members (two separate lists). A template loop over 'members'
    rendered empty rows against real production data — codex caught this."""

    def test_sheet_renders_pro_member_names(self, db_session, client):
        t, _pros = _seed_with_flights(
            db_session, with_relay_event=True, with_teams=True, status="drawn",
        )
        _db.session.commit()

        resp = client.get(f"/scheduling/{t.id}/relay-teams-sheet")
        assert resp.status_code == 200
        if "text/html" not in resp.content_type:
            pytest.skip("PDF fallback — name assertion is HTML-fallback only.")
        body = resp.data.decode("utf-8", errors="ignore")
        # Every pro member seeded in both teams should appear by name.
        for name in ("Pro 1", "Pro 2", "Pro 3", "Pro 4"):
            assert name in body, (
                f"Relay teams sheet did not render pro member {name!r}. "
                "Codex P2 regression: template must read pro_members + "
                "college_members, not a combined members list."
            )
        # Division badges match the real data shape.
        assert "PRO" in body
        assert "Team 1" in body
        assert "Team 2" in body

    def test_sheet_renders_when_status_is_in_progress(self, db_session, client):
        t, _ = _seed_with_flights(
            db_session, with_relay_event=True, with_teams=True, status="in_progress",
        )
        _db.session.commit()

        resp = client.get(f"/scheduling/{t.id}/relay-teams-sheet")
        assert resp.status_code == 200
        if "text/html" not in resp.content_type:
            pytest.skip("PDF fallback")
        body = resp.data.decode("utf-8", errors="ignore")
        assert "Pro 1" in body, (
            "Teams sheet must keep rendering once scoring starts — codex P2 guard."
        )
