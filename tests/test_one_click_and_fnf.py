"""
Regression tests for:
  - Friday Night Feature (FNF) event exclusion from Saturday pro flights
  - MIN_HEATS_PER_FLIGHT clamp in build_pro_flights
  - POST /scheduling/<tid>/flights/one-click-generate end-to-end flow
  - _build_fnf_schedule helper shape + ordering
  - GET /scheduling/<tid>/friday-night/print renders without error

Covers the feature shipped in cd451c6 (PR #47).

Run:
    pytest tests/test_one_click_and_fnf.py -v
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

    if not User.query.filter_by(username="fnf_admin").first():
        u = User(username="fnf_admin", role="admin")
        u.set_password("fnf_pass")
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
        data={"username": "fnf_admin", "password": "fnf_pass"},
        follow_redirects=True,
    )
    return c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _make_tournament(session, name="FNF Test 2026"):
    from models import Tournament

    t = Tournament(name=name, year=2026, status="pro_active")
    session.add(t)
    session.flush()
    return t


def _make_pro_event(session, tournament, name, stand_type, **kwargs):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="pro",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type=stand_type,
        max_stands=kwargs.get("max_stands", 4),
    )
    session.add(e)
    session.flush()
    return e


def _make_pro_comp(session, tournament, name, gender="M"):
    from models import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id, name=name, gender=gender, status="active"
    )
    session.add(c)
    session.flush()
    return c


def _make_heat(session, event, heat_number, competitor_ids):
    from models import Heat

    h = Heat(event_id=event.id, heat_number=heat_number, run_number=1)
    h.set_competitors(competitor_ids)
    session.add(h)
    session.flush()
    return h


def _seed_two_event_show(session):
    """Seed a tournament with Pro 1-Board (3 heats) + Obstacle Pole (3 heats).

    Pro 1-Board is intended as FNF; Obstacle Pole as Saturday show.
    """
    t = _make_tournament(session)
    comps = [_make_pro_comp(session, t, f"Pro {i}") for i in range(1, 13)]

    p1b = _make_pro_event(session, t, "Pro 1-Board", "springboard", max_stands=2)
    op = _make_pro_event(session, t, "Obstacle Pole", "obstacle_pole", max_stands=2)

    # 3 heats each, 2 competitors per heat, rotating competitor pool.
    for i in range(3):
        _make_heat(session, p1b, i + 1, [comps[i * 2].id, comps[i * 2 + 1].id])
        _make_heat(
            session, op, i + 1, [comps[(i * 2 + 6) % 12].id, comps[(i * 2 + 7) % 12].id]
        )

    return {"tournament": t, "p1b": p1b, "op": op, "comps": comps}


# ---------------------------------------------------------------------------
# FNF exclusion
# ---------------------------------------------------------------------------


class TestFNFExclusion:
    """Saturday flight builder must exclude events listed in
    schedule_config['friday_pro_event_ids']."""

    def test_fnf_events_excluded_from_saturday_flights(self, db_session):
        from models import Heat
        from services.flight_builder import build_pro_flights

        data = _seed_two_event_show(db_session)
        t = data["tournament"]
        # Mark Pro 1-Board for Friday Night Feature
        t.set_schedule_config({"friday_pro_event_ids": [data["p1b"].id]})
        db_session.flush()

        built = build_pro_flights(t)

        # Pro 1-Board heats must not be in any flight
        p1b_heats = Heat.query.filter_by(event_id=data["p1b"].id).all()
        assert len(p1b_heats) == 3, "Pro 1-Board heats should still exist in DB"
        for h in p1b_heats:
            assert (
                h.flight_id is None
            ), f"Pro 1-Board heat {h.heat_number} leaked into flight {h.flight_id}"

        # Obstacle Pole heats SHOULD be in a flight
        op_heats = Heat.query.filter_by(event_id=data["op"].id).all()
        assert len(op_heats) == 3
        for h in op_heats:
            assert (
                h.flight_id is not None
            ), f"Obstacle Pole heat {h.heat_number} not assigned to a flight"

        assert built >= 1

    def test_empty_fnf_list_includes_all_events(self, db_session):
        """When no FNF events are configured, every pro event feeds the Saturday builder."""
        from models import Heat
        from services.flight_builder import build_pro_flights

        data = _seed_two_event_show(db_session)
        t = data["tournament"]
        t.set_schedule_config({"friday_pro_event_ids": []})
        db_session.flush()

        build_pro_flights(t)

        # Every heat (both events) should now have a flight_id
        all_heats = Heat.query.filter(
            Heat.event_id.in_([data["p1b"].id, data["op"].id])
        ).all()
        assert len(all_heats) == 6
        unassigned = [h for h in all_heats if h.flight_id is None]
        assert not unassigned, f"{len(unassigned)} heats not assigned to flights"

    def test_fnf_exclusion_survives_malformed_config(self, db_session):
        """Non-integer garbage in friday_pro_event_ids should not crash the builder."""
        from services.flight_builder import build_pro_flights

        data = _seed_two_event_show(db_session)
        t = data["tournament"]
        # Mixed garbage + valid ID
        t.set_schedule_config(
            {
                "friday_pro_event_ids": [
                    data["p1b"].id,
                    "",
                    "not-an-int-but-will-raise",
                ],
            }
        )
        db_session.flush()

        # Should not raise; should at minimum exclude the valid FNF id
        built = build_pro_flights(t)
        assert built >= 0  # didn't crash


# ---------------------------------------------------------------------------
# MIN_HEATS_PER_FLIGHT clamp
# ---------------------------------------------------------------------------


class TestMinHeatsPerFlightClamp:
    """num_flights too high → each flight would have <2 heats → clamp kicks in."""

    def test_clamp_prevents_single_heat_flights(self, db_session):
        from models import Flight
        from services.flight_builder import build_pro_flights

        data = _seed_two_event_show(db_session)
        t = data["tournament"]
        t.set_schedule_config({})  # no FNF, 6 heats total

        # Request 6 flights for 6 heats — that would give 1 heat per flight
        built = build_pro_flights(t, num_flights=6)

        flights = Flight.query.filter_by(tournament_id=t.id).all()
        for f in flights:
            heat_count = len(f.heats.all())
            assert (
                heat_count >= 2
            ), f"Flight {f.flight_number} has {heat_count} heats — clamp failed"
        # With 6 heats, clamp to 2 per flight = 3 flights
        assert built == 3

    def test_default_builder_uses_8_per_flight(self, db_session):
        """num_flights=None → default heats_per_flight=8."""
        from models import Flight
        from services.flight_builder import build_pro_flights

        data = _seed_two_event_show(db_session)
        data["tournament"].set_schedule_config({})
        built = build_pro_flights(data["tournament"])

        # 6 heats, default 8 per flight → 1 flight
        assert built == 1
        flight = Flight.query.filter_by(tournament_id=data["tournament"].id).first()
        assert flight.heats.count() == 6


# ---------------------------------------------------------------------------
# One-click generate route
# ---------------------------------------------------------------------------


class TestOneClickGenerateRoute:
    """POST /scheduling/<tid>/flights/one-click-generate runs the full pipeline."""

    def test_route_is_registered_and_requires_auth(self, app):
        """Unauthenticated POST should redirect to login, not 404 or 500."""
        client = app.test_client()
        # Seed a tournament to have a valid id
        with app.app_context():
            t = _make_tournament(_db.session)
            _db.session.commit()
            tid = t.id

        resp = client.post(
            f"/scheduling/{tid}/flights/one-click-generate", follow_redirects=False
        )
        # Should be a redirect to login (302/303), NOT a 404 or 500
        assert resp.status_code in (
            301,
            302,
            303,
        ), f"Expected redirect for unauthed POST, got {resp.status_code}"

    def test_route_runs_full_pipeline_when_authed(self, app, auth_client):
        from models import Flight, Heat

        with app.app_context():
            data = _seed_two_event_show(_db.session)
            t = data["tournament"]
            t.set_schedule_config({"friday_pro_event_ids": [data["p1b"].id]})
            _db.session.commit()
            tid = t.id
            p1b_id = data["p1b"].id
            op_id = data["op"].id

        resp = auth_client.post(
            f"/scheduling/{tid}/flights/one-click-generate",
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "/flights" in resp.headers.get("Location", "")

        with app.app_context():
            # Obstacle Pole heats should be in flights
            op_heats = Heat.query.filter_by(event_id=op_id).all()
            assert all(
                h.flight_id is not None for h in op_heats
            ), "Obstacle Pole heats should be in flights after one-click"
            # Pro 1-Board heats should NOT be in flights
            p1b_heats = Heat.query.filter_by(event_id=p1b_id).all()
            assert all(
                h.flight_id is None for h in p1b_heats
            ), "Pro 1-Board (FNF) heats must stay unassigned after one-click"
            # At least one flight should exist
            assert Flight.query.filter_by(tournament_id=tid).count() >= 1


# ---------------------------------------------------------------------------
# _build_fnf_schedule helper
# ---------------------------------------------------------------------------


class TestBuildFnfSchedule:
    """_build_fnf_schedule returns heat-by-heat schedule data in correct order."""

    def test_returns_empty_when_no_fnf_selected(self, db_session):
        from routes.scheduling.friday_feature import _build_fnf_schedule

        data = _seed_two_event_show(db_session)
        result = _build_fnf_schedule(
            data["tournament"],
            eligible_events=[data["p1b"]],
            fnf_config={"event_ids": []},
        )
        assert result == []

    def test_includes_only_selected_events(self, db_session):
        from routes.scheduling.friday_feature import _build_fnf_schedule

        data = _seed_two_event_show(db_session)
        result = _build_fnf_schedule(
            data["tournament"],
            eligible_events=[data["p1b"], data["op"]],
            fnf_config={"event_ids": [data["p1b"].id]},
        )
        assert len(result) == 1
        assert result[0]["event"].id == data["p1b"].id
        assert len(result[0]["heats"]) == 3

    def test_heats_ordered_by_run_then_heat_number(self, db_session):
        """Heats should come back sorted by run_number, then heat_number."""
        from routes.scheduling.friday_feature import _build_fnf_schedule

        data = _seed_two_event_show(db_session)
        result = _build_fnf_schedule(
            data["tournament"],
            eligible_events=[data["p1b"]],
            fnf_config={"event_ids": [data["p1b"].id]},
        )
        heats = result[0]["heats"]
        heat_nums = [(h["run_number"], h["heat_number"]) for h in heats]
        assert heat_nums == sorted(
            heat_nums
        ), f"Heats not in (run, heat) order: {heat_nums}"

    def test_heat_row_has_competitor_name_and_stand(self, db_session):
        from routes.scheduling.friday_feature import _build_fnf_schedule

        data = _seed_two_event_show(db_session)
        # Set a stand assignment so we can verify it comes through
        from models import Heat

        h = Heat.query.filter_by(event_id=data["p1b"].id, heat_number=1).first()
        h.set_stand_assignment(data["comps"][0].id, 1)
        h.set_stand_assignment(data["comps"][1].id, 2)
        db_session.flush()

        result = _build_fnf_schedule(
            data["tournament"],
            eligible_events=[data["p1b"]],
            fnf_config={"event_ids": [data["p1b"].id]},
        )
        heat_row = result[0]["heats"][0]
        comp_row = heat_row["competitors"][0]
        assert "name" in comp_row
        assert "stand" in comp_row
        assert comp_row["name"].startswith("Pro ")  # "Pro 1", "Pro 2", etc.


# ---------------------------------------------------------------------------
# FNF print route
# ---------------------------------------------------------------------------


class TestFridayFeaturePrintRoute:
    """GET /scheduling/<tid>/friday-night/print renders a printable schedule."""

    def test_print_route_renders_200(self, app, auth_client):
        with app.app_context():
            data = _seed_two_event_show(_db.session)
            t = data["tournament"]
            t.set_schedule_config({"friday_pro_event_ids": [data["p1b"].id]})
            _db.session.commit()
            tid = t.id

        resp = auth_client.get(f"/scheduling/{tid}/friday-night/print")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Friday Night Feature Schedule" in body
        assert "Pro 1-Board" in body
        # No inline handlers — CSP compliance regression guard
        assert "onclick=" not in body, "Inline onclick= detected; CSP regression"
        assert "onsubmit=" not in body, "Inline onsubmit= detected; CSP regression"
        # The CSP-compliant pattern (id + script with addEventListener)
        assert "fnf-print-btn" in body


# ---------------------------------------------------------------------------
# FNF PDF route — WeasyPrint if installed, HTML fallback on Railway
# ---------------------------------------------------------------------------


class TestFridayFeaturePdfRoute:
    """GET /scheduling/<tid>/friday-night/pdf returns a PDF or HTML fallback."""

    def test_pdf_route_renders_200(self, app, auth_client):
        with app.app_context():
            data = _seed_two_event_show(_db.session)
            t = data["tournament"]
            t.set_schedule_config({"friday_pro_event_ids": [data["p1b"].id]})
            _db.session.commit()
            tid = t.id

        resp = auth_client.get(f"/scheduling/{tid}/friday-night/pdf")
        assert resp.status_code == 200
        ctype = resp.headers.get("Content-Type", "")
        # Either the PDF path (WeasyPrint installed) or the HTML fallback.
        # On Railway and in the test env, WeasyPrint is NOT installed, so we
        # expect text/html. Both are valid.
        assert ctype.startswith("application/pdf") or ctype.startswith("text/html"), (
            f"unexpected Content-Type {ctype!r} — want application/pdf or text/html"
        )

    def test_pdf_route_pdf_branch_sets_download_header(self, app, auth_client, monkeypatch):
        """Force the WeasyPrint branch to run and assert the Content-Disposition."""
        # Stub weasyprint.HTML so the route takes the PDF branch without needing
        # cairo/pango installed. Must stub BEFORE the import inside the helper
        # fires, so patch the services.print_response module's lazy import target.
        fake_pdf_bytes = b"%PDF-1.4 fake pdf for test\n"

        class _FakeWP:
            def __init__(self, string):
                self.string = string

            def write_pdf(self):
                return fake_pdf_bytes

        import sys as _sys
        import types as _types

        fake_module = _types.ModuleType("weasyprint")
        fake_module.HTML = _FakeWP  # type: ignore[attr-defined]
        monkeypatch.setitem(_sys.modules, "weasyprint", fake_module)

        with app.app_context():
            data = _seed_two_event_show(_db.session)
            t = data["tournament"]
            t.set_schedule_config({"friday_pro_event_ids": [data["p1b"].id]})
            _db.session.commit()
            tid = t.id

        resp = auth_client.get(f"/scheduling/{tid}/friday-night/pdf")
        assert resp.status_code == 200
        assert resp.headers.get("Content-Type") == "application/pdf"
        cd = resp.headers.get("Content-Disposition", "")
        assert "attachment" in cd
        assert "friday_night_feature" in cd
        assert cd.endswith('.pdf"')
        assert resp.data == fake_pdf_bytes
