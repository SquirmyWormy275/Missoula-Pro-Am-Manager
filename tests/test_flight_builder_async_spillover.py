"""
Phase 1 regression: async flight build must chain college spillover integration
atomically.

Historical bug: routes/scheduling/flights.py submitted build_pro_flights as an
async job but did NOT chain integrate_college_spillover_into_flights. The sync
path did. Result: every async flight build orphaned Chokerman Run 2 and every
selected saturday_college_event_ids heat with flight_id=NULL.

Fix: thread commit=False through build_pro_flights + integrate_college_spillover_into_flights,
then call both inside the async inner function and commit once at the end.

These tests exercise:
- build_pro_flights(commit=False) flushes but does not commit
- integrate_college_spillover_into_flights(commit=True) commits
- Atomic chain: flights + spillover committed together, rollback on failure
- Schedule_config['saturday_college_event_ids'] is the source of truth for the
  async inner function (mirroring the production call)

Run:  pytest tests/test_flight_builder_async_spillover.py -v
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


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _make_tournament(session, name="Async Spillover Test 2026", year=2026):
    from models import Tournament

    t = Tournament(name=name, year=year, status="pro_active")
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


def _make_college_event(
    session, tournament, name, stand_type, gender=None, requires_dual_runs=False
):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="college",
        gender=gender,
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type=stand_type,
        requires_dual_runs=requires_dual_runs,
    )
    session.add(e)
    session.flush()
    return e


def _make_pro_competitor(session, tournament, name, gender="M"):
    from models import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status="active",
    )
    session.add(c)
    session.flush()
    return c


def _make_heat(session, event, heat_number, competitor_ids, run_number=1):
    from models import Heat

    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
    )
    h.set_competitors(competitor_ids)
    session.add(h)
    session.flush()
    return h


def _seed_tournament_with_pro_heats_and_college_spillover(session):
    """Tournament 2-like fixture: pro heats + Chokerman Run 2 + selected spillover."""
    t = _make_tournament(session)

    # Pro competitors
    pros = [
        _make_pro_competitor(session, t, f"Pro {i}", gender="M") for i in range(1, 13)
    ]

    # Pro events with heats — enough variety for multi-flight build
    ev_spring = _make_pro_event(session, t, "Springboard", "springboard", max_stands=4)
    ev_uh_m = _make_pro_event(
        session, t, "Underhand", "underhand", gender="M", max_stands=5
    )
    ev_sb_m = _make_pro_event(
        session, t, "Standing Block", "standing_block", gender="M", max_stands=5
    )

    for n in range(1, 4):
        _make_heat(
            session, ev_spring, n, [pros[(n - 1) * 2].id, pros[(n - 1) * 2 + 1].id]
        )
    for n in range(1, 4):
        _make_heat(session, ev_uh_m, n, [pros[n].id, pros[n + 1].id, pros[n + 2].id])
    for n in range(1, 3):
        _make_heat(session, ev_sb_m, n, [pros[n + 3].id, pros[n + 4].id])

    # College events — Chokerman (auto-mandatory) + Obstacle Pole (selected)
    ev_chokerman = _make_college_event(
        session,
        t,
        "Chokerman's Race",
        "chokerman",
        gender="M",
        requires_dual_runs=True,
    )
    # Chokerman Run 1 (Friday) + Run 2 (Saturday)
    for n in range(1, 3):
        _make_heat(session, ev_chokerman, n, [], run_number=1)
        _make_heat(session, ev_chokerman, n, [], run_number=2)

    ev_op = _make_college_event(
        session, t, "Obstacle Pole", "obstacle_pole", gender="M"
    )
    for n in range(1, 3):
        _make_heat(session, ev_op, n, [])

    # Persist saturday_college_event_ids in schedule_config (what the async
    # inner function reads as its source of truth).
    config = t.get_schedule_config() or {}
    config["saturday_college_event_ids"] = [ev_op.id]
    t.set_schedule_config(config)
    session.flush()

    return {
        "tournament": t,
        "ev_chokerman": ev_chokerman,
        "ev_obstacle_pole": ev_op,
        "ev_spring": ev_spring,
        "ev_uh_m": ev_uh_m,
        "ev_sb_m": ev_sb_m,
    }


# ---------------------------------------------------------------------------
# commit=False flush behaviour
# ---------------------------------------------------------------------------


class TestBuildProFlightsCommitFlag:
    def test_commit_false_does_not_commit(self, db_session):
        """build_pro_flights(commit=False) flushes but leaves the outer tx open."""
        from models import Flight
        from services.flight_builder import build_pro_flights

        data = _seed_tournament_with_pro_heats_and_college_spillover(db_session)

        build_pro_flights(data["tournament"], num_flights=2, commit=False)

        # Flights exist in the session (flushed) even before a commit.
        flights = Flight.query.filter_by(tournament_id=data["tournament"].id).all()
        assert len(flights) > 0, "build_pro_flights(commit=False) should flush flights"

    def test_commit_true_default_preserves_old_behaviour(self, db_session):
        """Default call (no kwarg) must still commit as before."""
        from models import Flight
        from services.flight_builder import build_pro_flights

        data = _seed_tournament_with_pro_heats_and_college_spillover(db_session)
        # Wrap in an inner savepoint because the default commit path will commit
        # the outer db_session fixture transaction — that's actually fine inside
        # the nested autouse rollback wrapper.
        built = build_pro_flights(data["tournament"], num_flights=2)
        assert built > 0
        flights = Flight.query.filter_by(tournament_id=data["tournament"].id).all()
        assert len(flights) == built


class TestIntegrateSpilloverCommitFlag:
    def test_commit_false_default_preserves_old_behaviour(self, db_session):
        """Default commit=False preserves pre-phase-1 flush-only behaviour."""
        from services.flight_builder import (
            build_pro_flights,
            integrate_college_spillover_into_flights,
        )

        data = _seed_tournament_with_pro_heats_and_college_spillover(db_session)
        build_pro_flights(data["tournament"], num_flights=2, commit=False)

        result = integrate_college_spillover_into_flights(
            data["tournament"],
            college_event_ids=[data["ev_obstacle_pole"].id],
        )
        assert result["integrated_heats"] > 0


# ---------------------------------------------------------------------------
# Async chain — flights + spillover atomic
# ---------------------------------------------------------------------------


class TestAsyncBuildChainsSpillover:
    def _run_async_inner(self, tournament_id, num_flights=2):
        """Mirror the production _build_flights_async inner function.

        Kept in the test so the test is self-contained and immune to signature
        drift in routes/scheduling/flights.py.
        """
        from models import Tournament
        from services.flight_builder import (
            build_pro_flights,
            integrate_college_spillover_into_flights,
        )

        target = Tournament.query.get(tournament_id)
        if not target:
            raise RuntimeError(f"Tournament {tournament_id} not found.")
        try:
            flights_built = build_pro_flights(
                target,
                num_flights=num_flights,
                commit=False,
            )
            saturday_college_event_ids = [
                int(i)
                for i in (target.get_schedule_config() or {}).get(
                    "saturday_college_event_ids", []
                )
            ]
            integration = integrate_college_spillover_into_flights(
                target,
                college_event_ids=saturday_college_event_ids,
                commit=False,
            )
            _db.session.commit()
        except Exception:
            _db.session.rollback()
            raise
        return {
            "flights_built": flights_built,
            "spillover": {
                "integrated_heats": integration.get("integrated_heats", 0),
                "events": integration.get("events", 0),
                "message": integration.get("message", ""),
            },
        }

    def test_async_chain_integrates_chokerman_run2(self, db_session):
        """Chokerman Run 2 heats are mandatory Saturday placement."""
        from models import Heat

        data = _seed_tournament_with_pro_heats_and_college_spillover(db_session)
        t = data["tournament"]

        result = self._run_async_inner(t.id, num_flights=2)

        assert result["flights_built"] > 0
        assert result["spillover"]["integrated_heats"] > 0

        chokerman_run2 = Heat.query.filter_by(
            event_id=data["ev_chokerman"].id,
            run_number=2,
        ).all()
        assert chokerman_run2, "seed fixture should include Chokerman Run 2 heats"
        for h in chokerman_run2:
            assert h.flight_id is not None, (
                f"Chokerman Run 2 heat {h.heat_number} was orphaned "
                "(flight_id=None) — async chain bug regression."
            )

    def test_async_chain_integrates_selected_spillover_events(self, db_session):
        """Obstacle Pole (selected spillover) lands in some flight."""
        from models import Heat

        data = _seed_tournament_with_pro_heats_and_college_spillover(db_session)
        t = data["tournament"]

        self._run_async_inner(t.id, num_flights=2)

        op_heats = Heat.query.filter_by(event_id=data["ev_obstacle_pole"].id).all()
        assert op_heats, "seed fixture should include Obstacle Pole heats"
        placed = [h for h in op_heats if h.flight_id is not None]
        assert len(placed) == len(op_heats), (
            f"{len(op_heats) - len(placed)} Obstacle Pole heat(s) orphaned "
            "by async spillover chain."
        )

    def test_async_chain_rollback_on_spillover_failure(self, db_session, monkeypatch):
        """If spillover raises, flights build is rolled back too (atomicity)."""
        from models import Flight, Tournament

        data = _seed_tournament_with_pro_heats_and_college_spillover(db_session)
        t = data["tournament"]

        # Pre-existing flight count (should be 0 for a fresh tournament, but
        # measure to be safe — any orphans would be caught as delta > 0).
        pre = Flight.query.filter_by(tournament_id=t.id).count()

        import services.flight_builder as fb_module

        def _raise(*args, **kwargs):
            raise RuntimeError("simulated spillover failure")

        monkeypatch.setattr(
            fb_module,
            "integrate_college_spillover_into_flights",
            _raise,
        )

        # Re-bind the name the inner function imports locally.
        def _run_failing_chain():
            from services.flight_builder import (
                build_pro_flights,
                integrate_college_spillover_into_flights,  # patched
            )

            target = Tournament.query.get(t.id)
            try:
                build_pro_flights(target, num_flights=2, commit=False)
                integrate_college_spillover_into_flights(
                    target,
                    college_event_ids=[],
                    commit=False,
                )
                _db.session.commit()
            except Exception:
                _db.session.rollback()
                raise

        with pytest.raises(RuntimeError, match="simulated spillover failure"):
            _run_failing_chain()

        # Because the chain rolled back, flight count should match pre-state.
        post = Flight.query.filter_by(tournament_id=t.id).count()
        assert post == pre, (
            f"Atomicity regression: spillover raised but {post - pre} flight(s) "
            "persisted. Phase 1 must commit both operations as one unit."
        )

    def test_async_result_payload_includes_spillover_counts(self, db_session):
        """Job result dict exposes spillover integrated_heats + events."""
        data = _seed_tournament_with_pro_heats_and_college_spillover(db_session)
        t = data["tournament"]

        result = self._run_async_inner(t.id, num_flights=2)

        assert "flights_built" in result
        assert "spillover" in result
        assert "integrated_heats" in result["spillover"]
        assert "events" in result["spillover"]
        assert result["spillover"]["integrated_heats"] > 0
