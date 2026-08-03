"""Service-layer roster invariants: the mutators that seat and unseat people.

DOMAIN_CONTRACT (2026-04-27, inverted by D12-C commit E, narrowed by F2).

This module was ``test_heat_sync_invariants.py``. The contract it enforced ran
JSON-first: ``Heat.competitors`` was the source of truth, four mutators wrote
it, and every caller had to remember ``heat.sync_assignments(comp_type)``
afterwards or the ``HeatAssignment`` rows the validation service, judge sheets
and exports read would drift. Three of its five tests wrote the columns by
hand and asserted the rows came to match.

Commit E made the rows the only place a roster is written and commit F2
deleted ``sync_assignments`` along with the two accessors that read the
columns, so there is no longer a JSON roster for anything to adopt and no way
to express the drift those three tests staged. They are gone.

What survives is the half that was never about the columns: the two service
entry points that seat and unseat competitors have to leave every heat they
touch actually seated, and a test that goes through the real generator and
the real scratch cascade is the only thing in the suite that says so end to
end.

Run: pytest tests/test_roster_service_invariants.py -v
"""

from __future__ import annotations

import json
import os

import pytest

from database import db as _db


@pytest.fixture(scope="module")
def app():
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
# Setup helpers
# ---------------------------------------------------------------------------


def _make_tournament(session):
    from models import Tournament

    t = Tournament(name="RosterServiceInvariants", year=2026, status="setup")
    session.add(t)
    session.flush()
    return t


def _make_event(session, tournament, *, name="Underhand", event_type="pro", gender="M"):
    from models import Event

    ev = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender=gender,
        scoring_type="time",
        stand_type="underhand",
    )
    session.add(ev)
    session.flush()
    return ev


def _make_pro(session, tournament, name, gender="M"):
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


def _table_ids(heat_id):
    from models import HeatAssignment

    return {
        a.competitor_id for a in HeatAssignment.query.filter_by(heat_id=heat_id).all()
    }


def _assert_seated(heat):
    """The rows are the roster, so a seated heat is a heat with rows.

    This used to be ``_assert_in_sync``, comparing the JSON list against the
    rows. That comparison is unavailable and would be vacuous anyway. What is
    still worth asserting, and is the thing both services below have gotten
    wrong before, is that a heat which claims a roster has the rows to back
    it: a generator that built heats and seated nobody, or a cascade that
    emptied one, would leave the judge sheet blank on race day.
    """
    ids = _table_ids(heat.id)
    assert ids, f"heat {heat.id} was created or touched and seated nobody"
    assert set(heat.get_competitors()) == ids, (
        f"heat {heat.id} roster {heat.get_competitors()} does not match its "
        f"rows {sorted(ids)}")


# ---------------------------------------------------------------------------
# Service-layer mutators
# ---------------------------------------------------------------------------


def test_heat_generator_seats_every_heat_it_creates(db_session):
    """Heat generation must leave fresh heats seated without the caller
    having to do anything afterwards."""
    from models import Heat
    from services.heat_generator import generate_event_heats

    t = _make_tournament(db_session)
    ev = _make_event(db_session, t, name="Underhand", event_type="pro", gender="M")
    pros = [_make_pro(db_session, t, f"Pro {i}", "M") for i in range(8)]
    for p in pros:
        p.events_entered = json.dumps([str(ev.id)])
    db_session.flush()

    generate_event_heats(ev)
    heats = Heat.query.filter_by(event_id=ev.id).all()
    assert heats, "heat generator must create at least one heat"
    for h in heats:
        _assert_seated(h)


def test_scratch_cascade_removes_the_target_from_every_heat(db_session):
    """The cascade must unseat the scratched competitor everywhere, and must
    not empty the heats it walks through to do it."""
    from models import Heat, User
    from services.heat_generator import generate_event_heats
    from services.scratch_cascade import compute_scratch_effects, execute_cascade

    t = _make_tournament(db_session)
    ev = _make_event(db_session, t, name="Underhand", event_type="pro", gender="M")
    pros = [_make_pro(db_session, t, f"P{i}", "M") for i in range(6)]
    for p in pros:
        p.events_entered = json.dumps([str(ev.id)])
    db_session.flush()
    generate_event_heats(ev)
    db_session.flush()

    judge = User(
        username="invariant-judge",
        password_hash="x",
        role="judge",
    )
    db_session.add(judge)
    db_session.flush()

    target = pros[2]
    effects = compute_scratch_effects(target, t)
    execute_cascade(target, effects, judge.id, t)

    for h in Heat.query.filter_by(event_id=ev.id).all():
        _assert_seated(h)
        assert target.id not in h.get_competitors()
        assert target.id not in _table_ids(h.id)
