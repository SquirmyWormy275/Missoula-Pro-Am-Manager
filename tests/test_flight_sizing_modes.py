"""
Phase 3: flight sizing form supports 'minutes' (duration-driven) and 'count'
(operator-specified) modes. Operator choices persist to schedule_config.

Covers what was originally proposed as 4 separate files:
  * test_flight_sizing_mode_minutes.py
  * test_flight_sizing_mode_count.py
  * test_flight_sizing_config_persistence.py
  * test_flight_sizing_clamp.py
Consolidated into one file since every test uses the same seed + route client.

Run:  pytest tests/test_flight_sizing_modes.py -v
"""

from __future__ import annotations

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
    """Create an admin user so the build_flights route (judge-gated) is reachable."""
    from models.user import User

    if not User.query.filter_by(username="sizing_admin").first():
        u = User(username="sizing_admin", role="admin")
        u.set_password("sizing_pass")
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
    """Authenticated test client (logged in as sizing_admin)."""
    c = app.test_client()
    c.post(
        "/auth/login",
        data={"username": "sizing_admin", "password": "sizing_pass"},
        follow_redirects=True,
    )
    return c


def _make_tournament(session, name="Flight Sizing Test 2026"):
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


def _seed(session, pro_heat_count=60):
    """Seed enough pro heats across several events so flight sizing math is meaningful."""
    t = _make_tournament(session)
    # Create competitors so heats aren't empty (not strictly required for sizing math).
    pros = [_make_pro(session, t, f"P{i}") for i in range(1, 5)]  # noqa: F841
    # Distribute heats across three events so the builder can actually make multi-event flights.
    ev_sb = _make_pro_event(session, t, "Springboard", "springboard")
    ev_uh = _make_pro_event(
        session, t, "Underhand", "underhand", gender="M", max_stands=5
    )
    ev_op = _make_pro_event(session, t, "Obstacle Pole", "obstacle_pole")

    heats_per_event = pro_heat_count // 3
    for n in range(1, heats_per_event + 1):
        _make_heat(session, ev_sb, n)
        _make_heat(session, ev_uh, n)
        _make_heat(session, ev_op, n)
    return t


# ---------------------------------------------------------------------------
# Helpers for the compute function (unit-level, no HTTP)
# ---------------------------------------------------------------------------


class TestComputeNumFlights:
    def test_60_heats_at_5_5_over_60_min(self):
        """60 heats × 5.5 min / 60 min target = 6 flights."""
        from routes.scheduling.flights import _compute_num_flights_from_duration

        result, clamped = _compute_num_flights_from_duration(60, 5.5, 60)
        assert result == 6
        assert not clamped

    def test_30_heats_at_5_5_over_60_min(self):
        """30 × 5.5 / 60 = 2.75 → ceil 3 flights."""
        from routes.scheduling.flights import _compute_num_flights_from_duration

        result, _ = _compute_num_flights_from_duration(30, 5.5, 60)
        assert result == 3

    def test_clamp_upper(self):
        """200 × 5.5 / 30 = 36.67 → ceil 37 → clamped to 10."""
        from routes.scheduling.flights import _compute_num_flights_from_duration

        result, clamped = _compute_num_flights_from_duration(200, 5.5, 30)
        assert result == 10
        assert clamped

    def test_clamp_lower(self):
        """1 heat × 5.5 / 180 = 0.03 → ceil 1 → clamped to 2."""
        from routes.scheduling.flights import _compute_num_flights_from_duration

        result, clamped = _compute_num_flights_from_duration(1, 5.5, 180)
        assert result == 2
        assert clamped

    def test_zero_heats_degrades_gracefully(self):
        from routes.scheduling.flights import _compute_num_flights_from_duration

        result, _ = _compute_num_flights_from_duration(0, 5.5, 60)
        assert result == 2

    def test_zero_minutes_per_heat_degrades_gracefully(self):
        from routes.scheduling.flights import _compute_num_flights_from_duration

        result, _ = _compute_num_flights_from_duration(60, 0, 60)
        assert result == 2


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


class TestConfigPersistence:
    def test_roundtrip_writes_and_reads(self, db_session):
        from routes.scheduling.flights import (
            _persist_flight_sizing_config,
            _read_flight_sizing_config,
        )

        t = _seed(db_session, pro_heat_count=30)

        _persist_flight_sizing_config(t, "minutes", 90, 6.5, 5)
        db_session.flush()

        cfg = _read_flight_sizing_config(t)
        assert cfg["mode"] == "minutes"
        assert cfg["target_minutes_per_flight"] == 90
        assert cfg["minutes_per_heat"] == 6.5
        assert cfg["num_flights"] == 5

    def test_defaults_when_missing(self, db_session):
        from routes.scheduling.flights import (
            FLIGHT_SIZING_DEFAULTS,
            _read_flight_sizing_config,
        )

        t = _seed(db_session, pro_heat_count=30)
        cfg = _read_flight_sizing_config(t)
        assert cfg["mode"] == FLIGHT_SIZING_DEFAULTS["mode"]
        assert (
            cfg["target_minutes_per_flight"]
            == FLIGHT_SIZING_DEFAULTS["target_minutes_per_flight"]
        )
        assert cfg["minutes_per_heat"] == FLIGHT_SIZING_DEFAULTS["minutes_per_heat"]

    def test_clamps_out_of_range_persisted_values(self, db_session):
        from routes.scheduling.flights import _read_flight_sizing_config

        t = _seed(db_session, pro_heat_count=30)
        t.set_schedule_config(
            {
                "flight_sizing_mode": "minutes",
                "target_minutes_per_flight": 999,  # way over max
                "minutes_per_heat": -5.0,  # under min
                "num_flights": 99,  # over max
            }
        )
        db_session.flush()

        cfg = _read_flight_sizing_config(t)
        assert 30 <= cfg["target_minutes_per_flight"] <= 180
        assert 1.0 <= cfg["minutes_per_heat"] <= 15.0
        assert 2 <= cfg["num_flights"] <= 10

    def test_invalid_mode_falls_back(self, db_session):
        from routes.scheduling.flights import _read_flight_sizing_config

        t = _seed(db_session, pro_heat_count=30)
        t.set_schedule_config({"flight_sizing_mode": "nonsense"})
        db_session.flush()
        cfg = _read_flight_sizing_config(t)
        assert cfg["mode"] == "minutes"  # default


# ---------------------------------------------------------------------------
# HTTP — POST /scheduling/<tid>/flights/build
# ---------------------------------------------------------------------------


class TestBuildFlightsRouteModes:
    def _post_build(self, client, t_id, **form):
        form.setdefault("csrf_token", "x")
        return client.post(
            f"/scheduling/{t_id}/flights/build",
            data=form,
            follow_redirects=False,
        )

    def test_minutes_mode_posts_and_persists(self, db_session, client):
        from routes.scheduling.flights import _read_flight_sizing_config

        t = _seed(db_session, pro_heat_count=30)
        _db.session.commit()  # POST handler needs data visible on a fresh session

        resp = self._post_build(
            client,
            t.id,
            flight_sizing_mode="minutes",
            target_minutes_per_flight="60",
            minutes_per_heat="5.5",
            num_flights="4",
        )
        assert resp.status_code in (302, 303), resp.data[:200]

        from models import Tournament

        t_reloaded = Tournament.query.get(t.id)
        cfg = _read_flight_sizing_config(t_reloaded)
        assert cfg["mode"] == "minutes"
        assert cfg["target_minutes_per_flight"] == 60
        assert cfg["minutes_per_heat"] == 5.5

    def test_count_mode_posts_and_persists(self, db_session, client):
        from routes.scheduling.flights import _read_flight_sizing_config

        t = _seed(db_session, pro_heat_count=30)
        _db.session.commit()

        resp = self._post_build(
            client,
            t.id,
            flight_sizing_mode="count",
            num_flights="3",
            target_minutes_per_flight="60",
            minutes_per_heat="5.5",
        )
        assert resp.status_code in (302, 303)

        from models import Tournament

        t_reloaded = Tournament.query.get(t.id)
        cfg = _read_flight_sizing_config(t_reloaded)
        assert cfg["mode"] == "count"
        assert cfg["num_flights"] == 3

    def test_minutes_mode_produces_reasonable_flight_count(self, db_session, client):
        """60-heat tournament, 60 min target, 5.5 min/heat → 6 flights built."""
        from models import Flight

        t = _seed(db_session, pro_heat_count=60)
        _db.session.commit()

        resp = self._post_build(
            client,
            t.id,
            flight_sizing_mode="minutes",
            target_minutes_per_flight="60",
            minutes_per_heat="5.5",
        )
        assert resp.status_code in (302, 303)

        built = Flight.query.filter_by(tournament_id=t.id).count()
        # 60 * 5.5 / 60 = 5.5 → ceil 6. Builder may clamp to fewer if per-flight
        # heat count constraint kicks in. Accept the clamped range.
        assert 2 <= built <= 10, f"unexpected flight count {built}"
        assert (
            built == 6
        ), f"expected 6 flights for 60-heat minute-mode calc, got {built}"

    def test_count_mode_builds_exact_count(self, db_session, client):
        from models import Flight

        t = _seed(db_session, pro_heat_count=30)
        _db.session.commit()

        self._post_build(
            client,
            t.id,
            flight_sizing_mode="count",
            num_flights="3",
            target_minutes_per_flight="60",
            minutes_per_heat="5.5",
        )
        built = Flight.query.filter_by(tournament_id=t.id).count()
        assert built == 3


class TestRunShowGenerateAllSizing:
    def test_minutes_mode_resolves_after_fresh_heat_generation(
        self, db_session, client
    ):
        from models import Flight

        t = _make_tournament(db_session, name="Run Show Fresh Minutes")
        underhand = _make_pro_event(
            db_session, t, "Underhand", "underhand", max_stands=1
        )
        standing = _make_pro_event(
            db_session, t, "Standing Block", "standing_block", max_stands=1
        )
        for i in range(12):
            comp = _make_pro(db_session, t, f"Run Show Pro {i + 1}")
            comp.set_events_entered([
                underhand.name if i < 6 else standing.name
            ])
        _db.session.commit()

        resp = client.post(
            f"/scheduling/{t.id}/events",
            data={
                "csrf_token": "x",
                "action": "generate_all",
                "flight_sizing_mode": "minutes",
                "target_minutes_per_flight": "60",
                "minutes_per_heat": "15.0",
                "num_flights": "4",
            },
            follow_redirects=False,
        )

        assert resp.status_code in (302, 303), resp.data[:200]
        built = Flight.query.filter_by(tournament_id=t.id).count()
        assert built == 3, (
            "Run Show generate_all must size minutes mode from freshly "
            f"generated heats; got {built} flights"
        )


# ---------------------------------------------------------------------------
# Clamp behaviour via HTTP
# ---------------------------------------------------------------------------


class TestSizingClamp:
    def test_extreme_duration_inputs_clamp_without_500(self, db_session, client):
        t = _seed(db_session, pro_heat_count=20)
        _db.session.commit()

        # Target way too low — would compute 20*5.5/10 = 11 → clamp to 10.
        resp = client.post(
            f"/scheduling/{t.id}/flights/build",
            data={
                "csrf_token": "x",
                "flight_sizing_mode": "minutes",
                "target_minutes_per_flight": "10",
                "minutes_per_heat": "5.5",
                "num_flights": "4",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303), f"clamp path returned {resp.status_code}"

    def test_negative_inputs_coerce_to_bounds(self, db_session, client):
        from routes.scheduling.flights import _read_flight_sizing_config

        t = _seed(db_session, pro_heat_count=20)
        _db.session.commit()

        client.post(
            f"/scheduling/{t.id}/flights/build",
            data={
                "csrf_token": "x",
                "flight_sizing_mode": "minutes",
                "target_minutes_per_flight": "-999",
                "minutes_per_heat": "-5",
                "num_flights": "-3",
            },
            follow_redirects=False,
        )
        from models import Tournament

        t_reloaded = Tournament.query.get(t.id)
        cfg = _read_flight_sizing_config(t_reloaded)
        assert 30 <= cfg["target_minutes_per_flight"] <= 180
        assert 1.0 <= cfg["minutes_per_heat"] <= 15.0
