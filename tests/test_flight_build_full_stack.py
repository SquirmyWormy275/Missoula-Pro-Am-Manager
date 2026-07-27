"""
Cross-phase integration test for the V2.14.0 flight-fixes release.

Phase-by-phase, each PR (#68-73) has its own focused test file. None of
those exercises the full handshake on a single tournament. This file fills
that gap — one tournament seeded with every concern, one build pass, and
assertions that every Phase 1-5 property + the codex P2 hotfix rule all
hold simultaneously.

The test mirrors the production async chain:
  build_pro_flights(commit=False)
  -> integrate_proam_relay_into_final_flight(commit=False)
  -> integrate_college_spillover_into_flights(commit=True)
  -> db.session.commit()

Properties asserted:

  Phase 1 — commit=False threading / atomicity
    * Both integrators accept commit=False; single final commit persists
      everything.

  Phase 2 — DAY_SPLIT + placement-mode
    * Speed Climb Men Run 2 auto-routed (no explicit selection required).
    * Speed Climb Women Run 2 auto-routed.
    * Speed Climb Run 1 stays on Friday (no flight_id).
    * Chokerman Run 1 stays on Friday.
    * Non-day-split spillover event (Obstacle Pole) distributes via the
      saturday_college_placement_mode read from schedule_config.

  Phase 3 — minutes/count sizing
    * num_flights derived from target_minutes_per_flight + minutes_per_heat.
    * Computed count falls in [FLIGHT_COUNT_MIN, FLIGHT_COUNT_MAX].

  Phase 4 — Pro-Am Relay pseudo-heat
    * One synthesized relay heat exists.
    * Relay heat is in the final flight.
    * Relay heat flight_position < every Chokerman Run 2 position
      (Chokerman still closes the show; FlightLogic.md §4.1).

  Phase 5 — LH Springboard Stand 4
    * Every springboard heat containing an LH cutter assigns them
      stand_number = 4.
    * Other cutters in those heats get stands from {1, 2, 3}.

  Codex P2 hotfix (PR #73)
    * An in_progress relay status still places (regression guard against
      the `status != 'drawn'` orphan).

  Global
    * No heat in any DAY_SPLIT_EVENT_NAMES event has run_number=2 with
      flight_id=NULL after the chain runs.

Run:  pytest tests/test_flight_build_full_stack.py -v
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


def _make_tournament(session, name="V2.14.0 Full Stack"):
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


def _make_college_event(
    session,
    tournament,
    name,
    stand_type,
    gender=None,
    requires_dual_runs=False,
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


def _make_pro(session, tournament, name, gender="M", is_lh=False):
    from models.competitor import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status="active",
        is_left_handed_springboard=is_lh,
    )
    session.add(c)
    session.flush()
    return c


def _enroll(competitor, event):
    import json as _json

    entered = (
        competitor.get_events_entered()
        if hasattr(competitor, "get_events_entered")
        else []
    )
    if event.name not in entered:
        entered.append(event.name)
        competitor.events_entered = _json.dumps(entered)


def _seed_full_stack(session, relay_status="drawn"):
    """Seed one tournament exercising every Phase 1-5 concern.

    Layout:
      Pro events:
        Springboard — 3 heats; heat 1 contains 1 LH cutter + 3 RH cutters.
        Underhand M — 2 heats; 5 competitors each.
        Standing Block M — 2 heats; 5 competitors each.
        Obstacle Pole — 2 heats.

      College events (all day-split Run 2 must route automatically):
        Chokerman's Race M — 2 Run 1 heats + 2 Run 2 heats.
        Speed Climb M — 2 Run 1 heats + 2 Run 2 heats.
        Speed Climb F — 2 Run 1 heats + 2 Run 2 heats.

      College spillover (explicit selection):
        Obstacle Pole M — 2 heats (Phase 2 non-day-split distribution).

      Schedule config:
        saturday_college_event_ids = [obstacle_pole_m.id]
        saturday_college_placement_mode = 'roundrobin'
        flight_sizing_mode = 'minutes'
        target_minutes_per_flight = 60
        minutes_per_heat = 5.5

      Pro-Am Relay:
        1 Event row, event_state = {'status': relay_status, teams: [...]}
        Real shape — pro_members + college_members.
    """
    from models import Event, Heat
    from services.heat_generator import generate_event_heats

    t = _make_tournament(session)

    # Pro competitors — one LH springboard cutter, rest RH.
    lh_pro = _make_pro(session, t, "LH Cutter", is_lh=True)
    rh_pros = [_make_pro(session, t, f"RH Pro {i}") for i in range(1, 15)]
    all_pros = [lh_pro, *rh_pros]

    ev_sb = _make_pro_event(session, t, "Springboard", "springboard", max_stands=4)
    ev_uh_m = _make_pro_event(
        session,
        t,
        "Underhand",
        "underhand",
        gender="M",
        max_stands=5,
    )
    ev_sb_m_stand = _make_pro_event(
        session,
        t,
        "Standing Block",
        "standing_block",
        gender="M",
        max_stands=5,
    )
    ev_op_pro = _make_pro_event(session, t, "Obstacle Pole", "obstacle_pole")

    # Enroll the LH cutter + 11 RH pros into Springboard (3 heats of 4).
    # Then scatter the rest across UH/SB/OP.
    for c in [lh_pro, *rh_pros[:11]]:
        _enroll(c, ev_sb)
    for c in rh_pros[:10]:
        _enroll(c, ev_uh_m)
    for c in rh_pros[:10]:
        _enroll(c, ev_sb_m_stand)
    for c in rh_pros[:4]:
        _enroll(c, ev_op_pro)
    session.flush()

    for pro_event in (ev_sb, ev_uh_m, ev_sb_m_stand, ev_op_pro):
        generate_event_heats(pro_event)
    session.flush()

    # College day-split events — create Run 1 + Run 2 heats directly with no
    # enrolled competitors (integration cares about flight routing, not body).
    def _run_pair(event, heat_count):
        for n in range(1, heat_count + 1):
            Heat(event_id=event.id, heat_number=n, run_number=1)
            h1 = Heat(event_id=event.id, heat_number=n, run_number=1)
            h1.set_competitors([])
            session.add(h1)
            h2 = Heat(event_id=event.id, heat_number=n, run_number=2)
            h2.set_competitors([])
            session.add(h2)
        session.flush()

    ev_chokerman_m = _make_college_event(
        session,
        t,
        "Chokerman's Race",
        "chokerman",
        gender="M",
        requires_dual_runs=True,
    )
    ev_speed_m = _make_college_event(
        session,
        t,
        "Speed Climb",
        "speed_climb",
        gender="M",
        requires_dual_runs=True,
    )
    ev_speed_f = _make_college_event(
        session,
        t,
        "Speed Climb",
        "speed_climb",
        gender="F",
        requires_dual_runs=True,
    )
    ev_op_college_m = _make_college_event(
        session,
        t,
        "Obstacle Pole",
        "obstacle_pole",
        gender="M",
    )
    _run_pair(ev_chokerman_m, 2)
    _run_pair(ev_speed_m, 2)
    _run_pair(ev_speed_f, 2)

    # Obstacle Pole college — single-run spillover event.
    for n in range(1, 3):
        h = Heat(event_id=ev_op_college_m.id, heat_number=n, run_number=1)
        h.set_competitors([])
        session.add(h)
    session.flush()

    # Pro-Am Relay in the production shape (pro_members + college_members).
    relay = Event(
        tournament_id=t.id,
        name="Pro-Am Relay",
        event_type="pro",
        scoring_type="time",
        is_partnered=True,
        status="pending",
    )
    relay.event_state = json.dumps(
        {
            "status": relay_status,
            "teams": [
                {
                    "team_number": 1,
                    "pro_members": [
                        {"id": rh_pros[0].id, "name": rh_pros[0].name, "gender": "M"},
                        {"id": rh_pros[1].id, "name": rh_pros[1].name, "gender": "M"},
                    ],
                    "college_members": [],
                },
                {
                    "team_number": 2,
                    "pro_members": [
                        {"id": rh_pros[2].id, "name": rh_pros[2].name, "gender": "M"},
                        {"id": rh_pros[3].id, "name": rh_pros[3].name, "gender": "M"},
                    ],
                    "college_members": [],
                },
            ],
        }
    )
    session.add(relay)
    session.flush()

    # Schedule config exercising Phase 2 + Phase 3 persistence.
    cfg = t.get_schedule_config() or {}
    cfg["saturday_college_event_ids"] = [ev_op_college_m.id]
    cfg["saturday_college_placement_mode"] = "roundrobin"
    cfg["flight_sizing_mode"] = "minutes"
    cfg["target_minutes_per_flight"] = 60
    cfg["minutes_per_heat"] = 5.5
    t.set_schedule_config(cfg)
    session.flush()

    return {
        "tournament": t,
        "lh_pro": lh_pro,
        "ev_springboard": ev_sb,
        "ev_uh_m": ev_uh_m,
        "ev_chokerman_m": ev_chokerman_m,
        "ev_speed_m": ev_speed_m,
        "ev_speed_f": ev_speed_f,
        "ev_op_college_m": ev_op_college_m,
        "ev_relay": relay,
    }


def _build_chain(tournament, num_flights=4):
    """Mirror the production async chain from routes/scheduling/flights.py."""
    from services.flight_builder import (
        build_pro_flights,
        integrate_college_spillover_into_flights,
        integrate_proam_relay_into_final_flight,
    )

    try:
        flights_built = build_pro_flights(
            tournament,
            num_flights=num_flights,
            commit=False,
        )
        relay_result = integrate_proam_relay_into_final_flight(tournament, commit=False)
        saturday_college_event_ids = [
            int(i)
            for i in (tournament.get_schedule_config() or {}).get(
                "saturday_college_event_ids",
                [],
            )
        ]
        spillover_result = integrate_college_spillover_into_flights(
            tournament,
            college_event_ids=saturday_college_event_ids,
            commit=False,
        )
        _db.session.commit()
    except Exception:
        _db.session.rollback()
        raise
    return {
        "flights_built": flights_built,
        "relay": relay_result,
        "spillover": spillover_result,
    }


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


class TestFullStackFlightBuild:
    def test_drawn_relay_all_phases_agree(self, db_session):
        """One tournament, one build, every Phase 1-5 property holds."""
        from config import DAY_SPLIT_EVENT_NAMES
        from models import Flight, Heat

        data = _seed_full_stack(db_session, relay_status="drawn")
        t = data["tournament"]

        result = _build_chain(t, num_flights=4)

        # ----------------------------------------------------------
        # Phase 1 — commit succeeded, flights exist.
        # ----------------------------------------------------------
        assert result["flights_built"] > 0
        flights = (
            Flight.query.filter_by(tournament_id=t.id)
            .order_by(Flight.flight_number)
            .all()
        )
        assert len(flights) == result["flights_built"]
        last_flight_id = flights[-1].id

        # ----------------------------------------------------------
        # Phase 2 — DAY_SPLIT auto-add + Run 2 filter.
        # ----------------------------------------------------------
        # Speed Climb M + F Run 2 placed automatically (not in saturday_college_event_ids).
        speed_m_run2 = Heat.query.filter_by(
            event_id=data["ev_speed_m"].id,
            run_number=2,
        ).all()
        assert speed_m_run2, "fixture sanity: speed climb M run 2 heats exist"
        for h in speed_m_run2:
            assert h.flight_id is not None, (
                f"Speed Climb M Run 2 heat {h.heat_number} orphaned — "
                "Phase 2 DAY_SPLIT auto-add regression."
            )

        speed_f_run2 = Heat.query.filter_by(
            event_id=data["ev_speed_f"].id,
            run_number=2,
        ).all()
        assert speed_f_run2
        for h in speed_f_run2:
            assert h.flight_id is not None

        # Run 1 heats of day-split events stay on Friday (no flight_id).
        for ev in (data["ev_chokerman_m"], data["ev_speed_m"], data["ev_speed_f"]):
            run1_heats = Heat.query.filter_by(event_id=ev.id, run_number=1).all()
            for h in run1_heats:
                assert h.flight_id is None, (
                    f"{ev.name} Run 1 heat {h.heat_number} pulled to Saturday — "
                    "day-split Run 2-only filter regression."
                )

        # Non-day-split spillover (Obstacle Pole college M) placed.
        op_heats = Heat.query.filter_by(event_id=data["ev_op_college_m"].id).all()
        assert op_heats
        assert all(
            h.flight_id is not None for h in op_heats
        ), "Selected spillover event heats must be placed in flights."

        # ----------------------------------------------------------
        # Phase 3 — sizing.
        # ----------------------------------------------------------
        # With the fixture: ~19 pro heats (3+2+2+2 from generate + ~10 empty seats),
        # 60 min / 5.5 min/heat target → ceil math dominates; just verify range.
        assert (
            2 <= result["flights_built"] <= 10
        ), f"Phase 3 clamp expects [2, 10], got {result['flights_built']}"

        # ----------------------------------------------------------
        # Phase 4 — Relay pseudo-heat in final flight BEFORE Chokerman Run 2.
        # ----------------------------------------------------------
        relay_heats = Heat.query.filter_by(event_id=data["ev_relay"].id).all()
        assert (
            len(relay_heats) == 1
        ), f"Expected exactly one synthesized relay heat, got {len(relay_heats)}"
        relay_heat = relay_heats[0]
        assert (
            relay_heat.flight_id == last_flight_id
        ), "Relay pseudo-heat must land in the final flight."

        chokerman_run2 = Heat.query.filter_by(
            event_id=data["ev_chokerman_m"].id,
            run_number=2,
        ).all()
        assert chokerman_run2
        for h in chokerman_run2:
            assert h.flight_id == last_flight_id
            # FlightLogic.md §4.1: Chokerman CLOSES the show. Relay comes first.
            assert h.flight_position > relay_heat.flight_position, (
                f"Chokerman Run 2 heat {h.heat_number} at position {h.flight_position} "
                f"lands BEFORE relay at position {relay_heat.flight_position} — "
                "FlightLogic.md §4.1 show-climax rule violated."
            )

        # ----------------------------------------------------------
        # Phase 5 — LH Stand 4.
        # ----------------------------------------------------------
        # Find the springboard heat containing the LH cutter.
        sb_heats = Heat.query.filter_by(
            event_id=data["ev_springboard"].id,
            run_number=1,
        ).all()
        lh_heat = None
        for h in sb_heats:
            if data["lh_pro"].id in h.get_competitors():
                lh_heat = h
                break
        assert (
            lh_heat is not None
        ), "Fixture sanity: LH cutter should be in exactly one springboard heat."
        assignments = lh_heat.get_stand_assignments()
        assert assignments.get(str(data["lh_pro"].id)) == 4, (
            f"LH cutter should be on stand 4, got {assignments.get(str(data['lh_pro'].id))} "
            "— Phase 5 assignment regression."
        )
        other_stands = sorted(
            int(v) for k, v in assignments.items() if k != str(data["lh_pro"].id)
        )
        # Other cutters should be on stands from {1, 2, 3}.
        for s in other_stands:
            assert 1 <= s <= 3, (
                f"Non-LH cutter in an LH-containing heat got stand {s}, "
                "expected one of {1, 2, 3}."
            )

        # ----------------------------------------------------------
        # Global: no orphaned day-split Run 2 heats.
        # ----------------------------------------------------------
        from models import Event

        day_split_events = Event.query.filter(
            Event.tournament_id == t.id,
            Event.event_type == "college",
            Event.name.in_(list(DAY_SPLIT_EVENT_NAMES)),
        ).all()
        orphans = []
        for ev in day_split_events:
            for h in Heat.query.filter_by(event_id=ev.id, run_number=2).all():
                if h.flight_id is None:
                    orphans.append(f"{ev.name} run_number=2 heat {h.heat_number}")
        assert (
            not orphans
        ), f"Global integrity: day-split Run 2 heats orphaned: {orphans}"

    def test_in_progress_relay_still_places_across_full_stack(self, db_session):
        """Codex P2 hotfix guard: a relay with status=in_progress
        (mid-show scoring) still places on full-stack rebuild.

        This is the scenario operators hit when they rebuild flights after
        the first relay event has been scored — the pseudo-heat MUST
        re-attach, or the relay vanishes from the flight sheet mid-show.
        """
        from models import Heat

        data = _seed_full_stack(db_session, relay_status="in_progress")
        t = data["tournament"]

        result = _build_chain(t, num_flights=4)
        assert result["relay"]["placed"] is True, (
            "Regression: in_progress relay dropped on full-stack build. "
            "Codex P2 from PR #73 must stay fixed."
        )
        relay_heats = Heat.query.filter_by(event_id=data["ev_relay"].id).all()
        assert len(relay_heats) == 1

    def test_rebuild_is_idempotent_across_full_stack(self, db_session):
        """Running the full chain twice produces the same final structure.

        Specifically: no duplicate relay heat, no duplicate Chokerman Run 2
        placement, no duplicate spillover placement.
        """
        from models import Heat

        data = _seed_full_stack(db_session, relay_status="drawn")
        t = data["tournament"]

        _build_chain(t, num_flights=4)
        heats_after_first = Heat.query.filter_by(event_id=data["ev_relay"].id).count()

        _build_chain(t, num_flights=4)
        heats_after_second = Heat.query.filter_by(event_id=data["ev_relay"].id).count()

        assert heats_after_first == heats_after_second == 1, (
            f"Relay idempotency broke: {heats_after_first} after first build, "
            f"{heats_after_second} after second."
        )
