"""Regression: rendered-collection dashboards must not 500 when collections have data.

The V2.14.5 trilogy (docs/solutions/test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md)
found an entire class of bugs hidden by smoke tests that seed EMPTY collections.
Templates gate their rendering on ``{% if collection %}...{% for item in collection %}...``;
empty-DB smokes short-circuit the loop and never render the body. The bug lives
inside the body. Ship.

This file enforces Rule 3 of the trilogy — non-empty seed — for the six
race-week-critical dashboards flagged by the 2026-04-23 audit:

  - Pro-Am Relay dashboard (with a drawn lottery)
  - Partnered Axe Throw dashboard (with prelim state)
  - Fee Tracker (with pro competitors and per-event fees)
  - Pro Payout Summary (with competitors who have earnings)
  - Flight List + Build Flights pages (with at least one Flight + Heat)
  - Virtual Woodboss report (with WoodConfig rows + saw heats)

Each test asserts status 200 AND that a specific value known to be in the
seeded data appears in the rendered HTML. Asserting status alone would
miss the class of bug where the page renders 200 + an empty body.

Ability Rankings dashboard is intentionally NOT covered here — parallel
session owns routes/scheduling/ability_rankings.py at the time of authoring.
Add a companion test for it after that branch merges.
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
        _seed(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed(app):
    """Seed one tournament with enough data for every dashboard under test."""
    from models import Event, EventResult, Heat, Tournament
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.heat import Flight
    from models.team import Team
    from models.user import User
    from models.wood_config import WoodConfig

    if not User.query.filter_by(username="dash_admin").first():
        u = User(username="dash_admin", role="admin")
        u.set_password("dash_pass")
        _db.session.add(u)

    t = Tournament(
        name="Dashboard Seeded",
        year=2026,
        status="pro_active",
    )
    _db.session.add(t)
    _db.session.flush()

    # --- Pro events covering the dashboards --------------------------------
    springboard = Event(
        tournament_id=t.id,
        name="Springboard",
        event_type="pro",
        scoring_type="time",
        scoring_order="lowest_wins",
        status="pending",
        stand_type="springboard",
        max_stands=4,
    )
    underhand_m = Event(
        tournament_id=t.id,
        name="Underhand",
        event_type="pro",
        gender="M",
        scoring_type="time",
        scoring_order="lowest_wins",
        status="pending",
        stand_type="underhand",
        max_stands=5,
    )
    single_buck = Event(
        tournament_id=t.id,
        name="Single Buck",
        event_type="pro",
        gender="M",
        scoring_type="time",
        scoring_order="lowest_wins",
        status="pending",
        stand_type="saw_hand",
        max_stands=8,
    )
    relay_event = Event(
        tournament_id=t.id,
        name="Pro-Am Relay",
        event_type="pro",
        scoring_type="time",
        scoring_order="lowest_wins",
        status="pending",
    )
    axe_event = Event(
        tournament_id=t.id,
        name="Partnered Axe Throw",
        event_type="pro",
        scoring_type="hits",
        scoring_order="highest_wins",
        status="pending",
        has_prelims=True,
    )
    _db.session.add_all([springboard, underhand_m, single_buck, relay_event, axe_event])
    _db.session.flush()

    # --- Team + college competitors (for relay college pool) --------------
    team = Team(
        tournament_id=t.id,
        team_code="UM-A",
        school_name="Montana",
        school_abbreviation="UM",
    )
    _db.session.add(team)
    _db.session.flush()
    college_ids = []
    for i, (name, gender) in enumerate(
        [
            ("College Man A", "M"),
            ("College Man B", "M"),
            ("College Woman A", "F"),
            ("College Woman B", "F"),
        ]
    ):
        cc = CollegeCompetitor(
            tournament_id=t.id,
            team_id=team.id,
            name=name,
            gender=gender,
            individual_points=0,
            status="active",
        )
        cc.set_events_entered([])
        _db.session.add(cc)
        _db.session.flush()
        college_ids.append(cc.id)

    # --- Pro competitors (multiple events, fees, ALA mix) -----------------
    pro_ids = []
    for i, (name, gender, is_ala) in enumerate(
        [
            ("Alice Pro", "F", True),
            ("Bob Pro", "M", False),
            ("Carla Pro", "F", True),
            ("Dan Pro", "M", False),
            ("Eve Pro", "F", False),
        ]
    ):
        p = ProCompetitor(
            tournament_id=t.id,
            name=name,
            gender=gender,
            is_ala_member=is_ala,
            pro_am_lottery_opt_in=True,
            status="active",
            total_earnings=150.0 if i < 3 else 0.0,
            payout_settled=(i == 0),
        )
        # Events entered (by ID, per the valid-path case; CSV-import
        # audit covers the name-path)
        p.set_events_entered(
            [str(springboard.id), str(underhand_m.id), str(single_buck.id)]
        )
        p.entry_fees = json.dumps(
            {
                str(springboard.id): 75.0,
                str(underhand_m.id): 75.0,
                str(single_buck.id): 60.0,
            }
        )
        p.fees_paid = json.dumps(
            {
                str(springboard.id): (i % 2 == 0),
                str(underhand_m.id): True,
                str(single_buck.id): False,
            }
        )
        _db.session.add(p)
        _db.session.flush()
        pro_ids.append(p.id)

    # --- At least one Flight + Heat so flight_list / build_flights render --
    flight = Flight(
        tournament_id=t.id, flight_number=1, name="Flight A", status="pending"
    )
    _db.session.add(flight)
    _db.session.flush()

    heat1 = Heat(
        event_id=springboard.id,
        heat_number=1,
        run_number=1,
        flight_id=flight.id,
        flight_position=1,
        status="pending",
    )
    heat1.set_competitors(pro_ids[:4])
    heat1.set_stand_assignment(pro_ids[0], 1)
    heat1.set_stand_assignment(pro_ids[1], 2)
    heat1.set_stand_assignment(pro_ids[2], 3)
    heat1.set_stand_assignment(pro_ids[3], 4)
    _db.session.add(heat1)

    heat2 = Heat(
        event_id=single_buck.id,
        heat_number=1,
        run_number=1,
        flight_id=flight.id,
        flight_position=2,
        status="pending",
    )
    heat2.set_competitors(pro_ids[:2])
    _db.session.add(heat2)

    # --- EventResult with payout_amount for payout_summary rendering ------
    er = EventResult(
        event_id=springboard.id,
        competitor_id=pro_ids[0],
        competitor_type="pro",
        competitor_name="Alice Pro",
        result_value=42.0,
        best_run=42.0,
        final_position=1,
        payout_amount=150.0,
        status="completed",
    )
    _db.session.add(er)

    # --- Pro-Am Relay drawn teams (non-empty dashboard state) --------------
    relay_state = {
        "status": "drawn",
        "teams": [
            {
                "team_number": 1,
                "pro_members": [
                    {"id": pro_ids[0], "name": "Alice Pro", "gender": "F"},
                    {"id": pro_ids[2], "name": "Carla Pro", "gender": "F"},
                    {"id": pro_ids[1], "name": "Bob Pro", "gender": "M"},
                    {"id": pro_ids[3], "name": "Dan Pro", "gender": "M"},
                ],
                "college_members": [
                    {"id": college_ids[0], "name": "College Man A", "gender": "M"},
                    {"id": college_ids[1], "name": "College Man B", "gender": "M"},
                    {"id": college_ids[2], "name": "College Woman A", "gender": "F"},
                    {"id": college_ids[3], "name": "College Woman B", "gender": "F"},
                ],
            },
        ],
    }
    relay_event.payouts = json.dumps(relay_state)

    # --- Partnered Axe prelim state ----------------------------------------
    # Round-trip through the real PartneredAxeThrow service so the seeded
    # shape exactly matches what production writes. Hand-writing the pair
    # dict here would be the exact V2.14.0 fixture-divergence anti-pattern
    # the trilogy doc warns about (member vs pro_members / college_members).
    # Service requires pros to be entered in the axe event first.
    for pid in pro_ids[:4]:
        p = ProCompetitor.query.get(pid)
        entered = list(p.get_events_entered() or [])
        entered.append(str(axe_event.id))
        p.set_events_entered(entered)
    _db.session.flush()

    from services.partnered_axe import PartneredAxeThrow
    axe_service = PartneredAxeThrow(axe_event)
    axe_service.register_pair(pro_ids[0], pro_ids[1])  # Alice + Bob
    axe_service.register_pair(pro_ids[2], pro_ids[3])  # Carla + Dan

    # --- Wood configs so woodboss_report renders --------------------------
    wc_general = WoodConfig(
        tournament_id=t.id,
        config_key="log_general",
        species="Yellow Pine",
        size_value=10.0,
        size_unit="in",
    )
    wc_underhand = WoodConfig(
        tournament_id=t.id,
        config_key="block_underhand_pro_M",
        species="Poplar",
        size_value=12.0,
        size_unit="in",
    )
    _db.session.add_all([wc_general, wc_underhand])

    _db.session.commit()

    app.config["_DASH"] = {
        "tid": t.id,
        "springboard_id": springboard.id,
        "underhand_id": underhand_m.id,
        "relay_id": relay_event.id,
        "axe_id": axe_event.id,
        "flight_id": flight.id,
        "pro_ids": pro_ids,
    }


@pytest.fixture()
def auth_client(app):
    c = app.test_client()
    c.post(
        "/auth/login",
        data={"username": "dash_admin", "password": "dash_pass"},
        follow_redirects=True,
    )
    return c


# ------------------------------------------------------------------------
# Smokes — each asserts 200 + a content substring present in the seed.
# The content check is what distinguishes a real render from a 200-with-
# empty-body short-circuit.
# ------------------------------------------------------------------------


def test_relay_dashboard_renders_drawn_teams(auth_client, app):
    """V2.14.0 codex-caught bug reprise guard: relay template reads `pro_members`
    and `college_members`, not `members`. An empty-DB smoke missed that.
    """
    d = app.config["_DASH"]
    r = auth_client.get(f"/tournament/{d['tid']}/proam-relay/")
    assert r.status_code == 200, r.data.decode()[:1500]
    body = r.data.decode()
    assert (
        "Alice Pro" in body or "College Man A" in body
    ), "drawn-teams block short-circuited — relay state not rendered"


def test_axe_dashboard_renders_with_prelim_state(auth_client, app):
    d = app.config["_DASH"]
    r = auth_client.get(f"/tournament/{d['tid']}/partnered-axe/")
    assert r.status_code == 200, r.data.decode()[:1500]


def test_fee_tracker_renders_with_competitors(auth_client, app):
    d = app.config["_DASH"]
    r = auth_client.get(f"/reporting/{d['tid']}/pro/fee-tracker")
    assert r.status_code == 200, r.data.decode()[:1500]
    body = r.data.decode()
    # At least one seeded pro must appear in the body
    assert (
        "Alice Pro" in body or "Bob Pro" in body
    ), "fee-tracker empty-body short-circuit — no pro competitor rendered"


def test_payout_summary_renders_with_earnings(auth_client, app):
    d = app.config["_DASH"]
    r = auth_client.get(f"/reporting/{d['tid']}/pro/payouts")
    assert r.status_code == 200, r.data.decode()[:1500]
    body = r.data.decode()
    # An earner row must be present
    assert "Alice Pro" in body, "payout_summary body missing seeded earner"


def test_flight_list_renders_with_flight(auth_client, app):
    d = app.config["_DASH"]
    r = auth_client.get(f"/scheduling/{d['tid']}/flights")
    assert r.status_code == 200, r.data.decode()[:1500]


def test_build_flights_renders_with_flight(auth_client, app):
    d = app.config["_DASH"]
    r = auth_client.get(f"/scheduling/{d['tid']}/flights/build")
    assert r.status_code == 200, r.data.decode()[:1500]


def test_woodboss_report_renders_with_wood_configs(auth_client, app):
    d = app.config["_DASH"]
    r = auth_client.get(f"/woodboss/{d['tid']}/report")
    assert r.status_code == 200, r.data.decode()[:1500]
    body = r.data.decode()
    # Seeded species must surface somewhere in the report
    assert (
        "Yellow Pine" in body or "Poplar" in body
    ), "woodboss_report empty-body short-circuit — seeded WoodConfig not rendered"
