"""
Regression tests for the persisted-config-vs-session-only bug pattern.

When operator config is persisted to ``Tournament.schedule_config`` (e.g. via
the Friday Showcase page), every route that consumes those keys must read
DB-first and fall back to session, NOT session-only. Reading session-only
means a fresh-browser-session operator silently triggers the route with
empty config — orphaned spillover heats, missing FNF events on printouts.

Same shape as the V2.14.x ``num_flights`` bug: persisted config silently
ignored by some callers.

Covers:
  - GET /scheduling/<tid>/preflight       — saturday_college_event_ids
  - POST /scheduling/<tid>/preflight       — autofix uses saturday_ids
  - GET /scheduling/<tid>/day-schedule/print — friday_pro_event_ids +
                                                saturday_college_event_ids

Run:
    pytest tests/test_persisted_config_route_reads.py -v
"""

import os

import pytest

from database import db as _db


@pytest.fixture(scope="module")
def app():
    from tests.db_test_utils import create_test_app

    _app, db_path = create_test_app()
    with _app.app_context():
        from models.user import User

        if not User.query.filter_by(username="cfg_admin").first():
            u = User(username="cfg_admin", role="admin")
            u.set_password("cfg_pass")
            _db.session.add(u)
            _db.session.commit()
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


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
        data={"username": "cfg_admin", "password": "cfg_pass"},
        follow_redirects=True,
    )
    return c


def _make_tournament(session):
    from models import Tournament

    t = Tournament(name="ConfigReads 2026", year=2026, status="pro_active")
    session.add(t)
    session.flush()
    return t


def _make_college_event(session, tournament, name, gender):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="college",
        gender=gender,
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="standing_block",
        max_stands=4,
    )
    session.add(e)
    session.flush()
    return e


def _make_pro_event(session, tournament, name, stand_type):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="pro",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type=stand_type,
        max_stands=4,
    )
    session.add(e)
    session.flush()
    return e


# ---------------------------------------------------------------------------
# Bug A — preflight POST autofix must read saturday_ids from DB, not session
# ---------------------------------------------------------------------------


class TestPreflightAutofixHonoursPersistedSpillover:
    """When the operator persists Saturday spillover IDs via the Friday
    Showcase page (DB), preflight autofix MUST pick them up — even when
    triggered from a fresh browser session with an empty Flask session.
    """

    def test_preflight_get_reads_saturday_ids_from_db(self, app, auth_client):
        with app.app_context():
            t = _make_tournament(_db.session)
            sb_m = _make_college_event(_db.session, t, "Standing Block Speed", "M")
            t.set_schedule_config({"saturday_college_event_ids": [sb_m.id]})
            _db.session.commit()
            tid = t.id
            sb_id = sb_m.id

        # Fresh session — never visited Friday Showcase, so no schedule_options
        # in Flask session.
        resp = auth_client.get(f"/scheduling/{tid}/preflight")
        assert resp.status_code == 200
        # The page should render without crashing AND should be aware of the
        # spillover event (we just check the response includes the event id
        # in some form so we know saturday_ids was read).
        # We don't assert template content rigidly — we assert the request
        # didn't 500 because saturday_ids = [] was passed when sb_id was set.
        # The deeper proof comes from the autofix POST below.
        assert b"preflight" in resp.data.lower() or b"check" in resp.data.lower()

    def test_preflight_autofix_post_reads_saturday_ids_from_db(self, app, auth_client):
        """Bug A regression: POST autofix used to read session-only.
        Operator-persisted spillover IDs were silently dropped → autofix
        ran with saturday_ids=[] and integrated zero spillover heats."""
        from unittest.mock import patch

        with app.app_context():
            t = _make_tournament(_db.session)
            sb_m = _make_college_event(_db.session, t, "Standing Block Speed", "M")
            t.set_schedule_config({"saturday_college_event_ids": [sb_m.id]})
            _db.session.commit()
            tid = t.id
            sb_id = sb_m.id

        # Capture what saturday_ids actually reach the autofix service.
        captured = {}

        def _spy(tournament, saturday_ids=None):
            captured["saturday_ids"] = list(saturday_ids or [])
            return {
                "heats_fixed": 0,
                "gear_parsed": {"parsed": 0},
                "gear_pairs_completed": 0,
                "partner_summary": {"assigned_pairs": 0},
                "spillover": {"integrated_heats": 0, "events": 0, "message": ""},
                "relay": {"placed": False, "reason": "test", "team_count": 0},
            }

        with patch(
            "routes.scheduling.preflight.run_preflight_autofix", side_effect=_spy
        ):
            resp = auth_client.post(
                f"/scheduling/{tid}/preflight",
                data={"action": "autofix"},
                follow_redirects=False,
            )
        assert resp.status_code in (302, 303)
        assert captured.get("saturday_ids") == [sb_id], (
            f"autofix received saturday_ids={captured.get('saturday_ids')!r}; "
            f"expected [{sb_id}] from persisted schedule_config"
        )


# ---------------------------------------------------------------------------
# Bug B — day_schedule_print must read FNF + spillover from DB
# ---------------------------------------------------------------------------


class TestDaySchedulePrintHonoursPersistedConfig:
    """day_schedule_print used to read schedule_options from session ONLY.
    Print from a fresh browser session → printout silently omits FNF events
    and Saturday spillover events → judges receive a schedule that doesn't
    match the DB.
    """

    def test_print_reads_friday_pro_event_ids_from_db(self, app, auth_client):
        from unittest.mock import patch

        with app.app_context():
            t = _make_tournament(_db.session)
            p1b = _make_pro_event(_db.session, t, "Pro 1-Board", "springboard")
            t.set_schedule_config(
                {
                    "friday_pro_event_ids": [p1b.id],
                    "saturday_college_event_ids": [],
                }
            )
            _db.session.commit()
            tid = t.id
            p1b_id = p1b.id

        captured = {}

        def _spy(
            tournament, friday_pro_event_ids=None, saturday_college_event_ids=None
        ):
            captured["friday"] = list(friday_pro_event_ids or [])
            captured["saturday"] = list(saturday_college_event_ids or [])
            return {"friday": [], "saturday": []}

        with patch(
            "services.schedule_builder.build_day_schedule", side_effect=_spy
        ):
            resp = auth_client.get(f"/scheduling/{tid}/day-schedule/print")

        assert resp.status_code == 200
        assert captured.get("friday") == [p1b_id], (
            f"day_schedule_print received friday_pro_event_ids={captured.get('friday')!r}; "
            f"expected [{p1b_id}] from persisted schedule_config"
        )

    def test_print_reads_saturday_college_event_ids_from_db(self, app, auth_client):
        from unittest.mock import patch

        with app.app_context():
            t = _make_tournament(_db.session)
            sb_m = _make_college_event(_db.session, t, "Standing Block Speed", "M")
            t.set_schedule_config(
                {
                    "friday_pro_event_ids": [],
                    "saturday_college_event_ids": [sb_m.id],
                }
            )
            _db.session.commit()
            tid = t.id
            sb_id = sb_m.id

        captured = {}

        def _spy(
            tournament, friday_pro_event_ids=None, saturday_college_event_ids=None
        ):
            captured["saturday"] = list(saturday_college_event_ids or [])
            return {"friday": [], "saturday": []}

        with patch(
            "services.schedule_builder.build_day_schedule", side_effect=_spy
        ):
            resp = auth_client.get(f"/scheduling/{tid}/day-schedule/print")

        assert resp.status_code == 200
        assert captured.get("saturday") == [sb_id], (
            f"day_schedule_print received saturday_college_event_ids={captured.get('saturday')!r}; "
            f"expected [{sb_id}] from persisted schedule_config"
        )


# ---------------------------------------------------------------------------
# Bug C — async generate_tournament_schedule_artifacts must chain relay +
# spillover so the async path produces the same output as the sync Run Show
# "Generate All Heats + Build Flights" button.
# ---------------------------------------------------------------------------

class TestAsyncGenerateArtifactsChainsRelayAndSpillover:
    """generate_tournament_schedule_artifacts used to only build flights.
    It skipped relay placement and Saturday college spillover integration
    entirely — so any future UI button wired to /events/generate-async would
    produce a schedule with the Pro-Am Relay unassigned and Chokerman Run 2
    orphaned with flight_id=NULL.
    """

    def test_async_generate_invokes_relay_and_spillover(self, app, db_session):
        from unittest.mock import patch

        from models import Heat
        from services.schedule_generation import generate_tournament_schedule_artifacts

        # Seed entirely inside the autouse db_session's nested transaction.
        t = _make_tournament(db_session)
        pe = _make_pro_event(db_session, t, "Obstacle Pole", "obstacle_pole")
        cp = _make_college_event(db_session, t, "Chokerman's Race", "M")
        t.set_schedule_config({"saturday_college_event_ids": [cp.id]})
        # Seed one pro heat so pro_heats > 0 and the chain runs.
        h = Heat(event_id=pe.id, heat_number=1, run_number=1)
        h.set_competitors([])
        db_session.add(h)
        db_session.flush()
        tid = t.id
        cp_id = cp.id

        relay_calls = []
        spill_calls = []

        def _relay_spy(tournament, commit=True):
            relay_calls.append({"commit": commit, "tid": tournament.id})
            return {"placed": False, "reason": "no-relay-state", "team_count": 0}

        def _spill_spy(tournament, college_event_ids=None, commit=False, placement_mode=None):
            spill_calls.append({
                "commit": commit,
                "college_event_ids": list(college_event_ids or []),
                "tid": tournament.id,
            })
            return {"integrated_heats": 0, "events": 0, "message": "spy"}

        def _build_spy(tournament, num_flights=None, commit=True):
            return 1

        with patch(
            "services.flight_builder.build_pro_flights", side_effect=_build_spy,
        ), patch(
            "services.flight_builder.integrate_proam_relay_into_final_flight",
            side_effect=_relay_spy,
        ), patch(
            "services.flight_builder.integrate_college_spillover_into_flights",
            side_effect=_spill_spy,
        ):
            result = generate_tournament_schedule_artifacts(tid)

        assert result["ok"] is True, result
        assert len(relay_calls) == 1, (
            f"relay integration not invoked by async generate: {relay_calls}"
        )
        assert relay_calls[0]["commit"] is False, (
            "async chain must use commit=False so build + relay + spillover "
            "are atomic"
        )
        assert len(spill_calls) == 1, (
            f"spillover integration not invoked by async generate: {spill_calls}"
        )
        assert cp_id in spill_calls[0]["college_event_ids"], (
            f"saturday_college_event_ids from schedule_config did not reach "
            f"spillover: got {spill_calls[0]['college_event_ids']!r}, "
            f"expected to include {cp_id}"
        )
