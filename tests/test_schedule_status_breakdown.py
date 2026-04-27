"""
Regression tests for the "In heats: X / Y competitors" breakdown on
events.html. Replaces the opaque "Placed: N" metric with four clear buckets:
  1. competitors_placed      — in at least one heat
  2. competitors_non_heat_only — entered only list-only / bracket / state-machine events
  3. competitors_no_events   — empty events_entered
  4. competitors_missing_from_heats — entered a heat event but missing from its heats
     (the bug-surface; if this is non-zero, heat generation skipped them)

The user's live panel showed "Placed: 37 competitors" on a 64-competitor
tournament with 23/29 events holding heats. The 27-unplaced number had
no explanation. After this change, the breakdown tells the user at a
glance whether those 27 are expected (list-only / birling only, no events
entered) or a real bug (missing from heats they should be in).
"""

import json
import os

import pytest

from database import db as _db


@pytest.fixture()
def app():
    from tests.db_test_utils import create_test_app
    from models.user import User

    _app, db_path = create_test_app()
    with _app.app_context():
        if not User.query.filter_by(username="sb_admin").first():
            u = User(username="sb_admin", role="admin")
            u.set_password("sb_pass")
            _db.session.add(u)
            _db.session.commit()
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _make_tournament(session):
    from models import Tournament

    t = Tournament(name="breakdown", year=2026, status="college_active")
    session.add(t)
    session.flush()
    return t


def _make_college_event(session, t, name, stand_type, scoring_type="time", gender=None):
    from models.event import Event

    ev = Event(
        tournament_id=t.id,
        name=name,
        event_type="college",
        gender=gender,
        scoring_type=scoring_type,
        scoring_order="lowest_wins",
        stand_type=stand_type,
        max_stands=5,
    )
    session.add(ev)
    session.flush()
    return ev


def _make_college_comp(session, t, name, events, team_id=None, gender="M"):
    from models.competitor import CollegeCompetitor

    c = CollegeCompetitor(
        tournament_id=t.id,
        team_id=team_id,
        name=name,
        gender=gender,
        events_entered=json.dumps(events),
        partners=json.dumps({}),
        gear_sharing=json.dumps({}),
        status="active",
    )
    session.add(c)
    session.flush()
    return c


def _make_team(session, t):
    from models.team import Team

    team = Team(
        tournament_id=t.id,
        team_code="UM-A",
        school_name="University of Montana",
        school_abbreviation="UM",
    )
    session.add(team)
    session.flush()
    return team


def _make_heat(session, ev, competitor_ids):
    from models.heat import Heat

    h = Heat(
        event_id=ev.id,
        heat_number=1,
        run_number=1,
        competitors=json.dumps([int(c) for c in competitor_ids]),
        stand_assignments=json.dumps({}),
        status="pending",
    )
    session.add(h)
    session.flush()
    return h


def test_breakdown_list_only_signup_counted_correctly(app):
    """
    Competitor entered only in Axe Throw (list-only event) — should show up
    in competitors_non_heat_only, not in competitors_missing_from_heats.
    """
    from services.schedule_status import build_schedule_status

    with app.app_context():
        t = _make_tournament(_db.session)
        team = _make_team(_db.session, t)

        ev_uh = _make_college_event(_db.session, t, "Underhand Chop", "underhand")
        ev_axe = _make_college_event(_db.session, t, "Axe Throw", "axe_throw")

        c1 = _make_college_comp(
            _db.session,
            t,
            "Ranked Rachel",
            ["Underhand Chop"],
            team_id=team.id,
            gender="F",
        )
        c2 = _make_college_comp(
            _db.session, t, "Thrower Tim", ["Axe Throw"], team_id=team.id, gender="M"
        )
        c3 = _make_college_comp(
            _db.session, t, "Empty Emma", [], team_id=team.id, gender="F"
        )
        _make_heat(_db.session, ev_uh, [c1.id])
        _db.session.commit()

        with app.test_request_context():
            status = build_schedule_status(t)
        f = status["friday"]

    assert f["competitors_total"] == 3
    assert f["competitors_placed"] == 1, f"got {f['competitors_placed']}"
    assert (
        f["competitors_non_heat_only"] == 1
    ), f"expected Thrower Tim (axe throw only) in non_heat_only, got {f['competitors_non_heat_only']}"
    assert (
        f["competitors_no_events"] == 1
    ), f"expected Empty Emma in no_events, got {f['competitors_no_events']}"
    assert (
        f["competitors_missing_from_heats"] == 0
    ), f"nobody should be missing from heats here, got {f['competitors_missing_from_heats']}"


def test_breakdown_flags_real_bug_missing_from_heats(app):
    """
    Competitor entered Underhand (a heat-generating event) but heat row
    does not include them. This is the bug-surface bucket — breakdown
    must count them as missing_from_heats, not non_heat_only.
    """
    from services.schedule_status import build_schedule_status

    with app.app_context():
        t = _make_tournament(_db.session)
        team = _make_team(_db.session, t)

        ev_uh = _make_college_event(_db.session, t, "Underhand Chop", "underhand")

        c1 = _make_college_comp(
            _db.session,
            t,
            "In Heat Ian",
            ["Underhand Chop"],
            team_id=team.id,
            gender="M",
        )
        c2 = _make_college_comp(
            _db.session,
            t,
            "Missing Morgan",
            ["Underhand Chop"],
            team_id=team.id,
            gender="M",
        )
        # Heat only places c1; c2 is in events_entered but absent from heat.
        _make_heat(_db.session, ev_uh, [c1.id])
        _db.session.commit()

        with app.test_request_context():
            status = build_schedule_status(t)
        f = status["friday"]

    assert f["competitors_total"] == 2
    assert f["competitors_placed"] == 1
    assert f["competitors_missing_from_heats"] == 1, (
        f"BUG SURFACE — expected Missing Morgan flagged, got {f['competitors_missing_from_heats']}. "
        f"Sample: {f.get('competitors_missing_sample')}"
    )
    assert "Missing Morgan" in f["competitors_missing_sample"]


def test_breakdown_bracket_only_signup_is_non_heat(app):
    """
    Competitor entered only in College Birling (bracket-scored event) —
    belongs in non_heat_only, not missing.
    """
    from services.schedule_status import build_schedule_status

    with app.app_context():
        t = _make_tournament(_db.session)
        team = _make_team(_db.session, t)

        ev_bir = _make_college_event(
            _db.session,
            t,
            "College Birling",
            "birling",
            scoring_type="bracket",
            gender="M",
        )

        c1 = _make_college_comp(
            _db.session,
            t,
            "Birler Bob",
            ["College Birling"],
            team_id=team.id,
            gender="M",
        )
        _db.session.commit()

        with app.test_request_context():
            status = build_schedule_status(t)
        f = status["friday"]

    assert f["competitors_placed"] == 0
    assert f["competitors_non_heat_only"] == 1, (
        f"Birling is bracket-scored, not a heat event. "
        f"Birler Bob should be non_heat_only, got non_heat={f['competitors_non_heat_only']}, "
        f"missing={f['competitors_missing_from_heats']}"
    )
    assert f["competitors_missing_from_heats"] == 0


def test_inactive_competitor_in_heat_does_not_inflate_placed(app):
    """CODEX P2: a scratched competitor still appears in the heats they
    were originally assigned to. competitors_total only counts active
    competitors, so without this guard placed_ids could exceed total
    and produce impossible ratios like '38 / 37'.
    """
    from services.schedule_status import build_schedule_status

    with app.app_context():
        t = _make_tournament(_db.session)
        team = _make_team(_db.session, t)
        ev_uh = _make_college_event(_db.session, t, "Underhand Chop", "underhand")

        active = _make_college_comp(
            _db.session, t, "Active Alex", ["Underhand Chop"], team.id, "M"
        )
        scratched = _make_college_comp(
            _db.session, t, "Scratched Sam", ["Underhand Chop"], team.id, "M"
        )
        scratched.status = "scratched"
        _db.session.flush()
        # Heat row still references scratched competitor (mid-event scratch).
        _make_heat(_db.session, ev_uh, [active.id, scratched.id])
        _db.session.commit()

        with app.test_request_context():
            status = build_schedule_status(t)
        f = status["friday"]

    assert f["competitors_total"] == 1, "scratched competitor excluded from total"
    assert f["competitors_placed"] == 1, (
        f"only active competitor counts as placed, got {f['competitors_placed']}"
    )
    assert f["competitors_placed"] <= f["competitors_total"], (
        f"placed ({f['competitors_placed']}) cannot exceed total "
        f"({f['competitors_total']}) — this is the '38/37' bug"
    )


def test_opposite_gender_event_does_not_flag_competitor_as_missing(app):
    """CODEX P2: men's and women's events often share names ("Underhand"
    for both). The classification loop must apply the same gender filter
    that _signed_up_competitors uses, otherwise a man entered in the
    Men's event matches the Women's event by name and gets flagged
    MISSING FROM HEATS for an event he was never eligible for.
    """
    from services.schedule_status import build_schedule_status

    with app.app_context():
        t = _make_tournament(_db.session)
        team = _make_team(_db.session, t)
        # Two same-named events, different genders.
        ev_men = _make_college_event(
            _db.session, t, "Underhand", "underhand", gender="M"
        )
        ev_women = _make_college_event(
            _db.session, t, "Underhand", "underhand", gender="F"
        )

        male_comp = _make_college_comp(
            _db.session, t, "Bob Buck", ["Underhand"], team.id, "M"
        )
        # Heat exists for MEN's event, BOB IS IN IT. Women's event has no heats.
        _make_heat(_db.session, ev_men, [male_comp.id])
        _db.session.commit()

        with app.test_request_context():
            status = build_schedule_status(t)
        f = status["friday"]

    assert f["competitors_placed"] == 1
    assert f["competitors_missing_from_heats"] == 0, (
        "Bob is placed in the Men's heat. He must NOT be flagged as missing "
        "from the empty Women's event just because the names match. "
        f"Got missing={f['competitors_missing_from_heats']}, "
        f"sample={f.get('competitors_missing_sample')}"
    )


def test_breakdown_placed_plus_buckets_equals_total(app):
    """Conservation check: every competitor falls into exactly one bucket."""
    from services.schedule_status import build_schedule_status

    with app.app_context():
        t = _make_tournament(_db.session)
        team = _make_team(_db.session, t)
        ev_uh = _make_college_event(_db.session, t, "Underhand Chop", "underhand")
        ev_axe = _make_college_event(_db.session, t, "Axe Throw", "axe_throw")

        placed1 = _make_college_comp(
            _db.session, t, "P1", ["Underhand Chop"], team.id, "M"
        )
        placed2 = _make_college_comp(
            _db.session, t, "P2", ["Underhand Chop"], team.id, "M"
        )
        axe_only = _make_college_comp(_db.session, t, "A1", ["Axe Throw"], team.id, "M")
        no_events = _make_college_comp(_db.session, t, "N1", [], team.id, "M")
        missing = _make_college_comp(
            _db.session, t, "M1", ["Underhand Chop"], team.id, "M"
        )

        _make_heat(_db.session, ev_uh, [placed1.id, placed2.id])
        _db.session.commit()

        with app.test_request_context():
            status = build_schedule_status(t)
        f = status["friday"]

    accounted = (
        f["competitors_placed"]
        + f["competitors_non_heat_only"]
        + f["competitors_no_events"]
        + f["competitors_missing_from_heats"]
    )
    assert accounted == f["competitors_total"], (
        f"buckets {accounted} != total {f['competitors_total']}. "
        f"placed={f['competitors_placed']} non_heat={f['competitors_non_heat_only']} "
        f"no_events={f['competitors_no_events']} missing={f['competitors_missing_from_heats']}"
    )
