"""
Workflow invariants for ``Heat.competitors`` ↔ ``HeatAssignment`` sync.

DOMAIN_CONTRACT (2026-04-27): every route or service that mutates
``Heat.competitors`` (the JSON source of truth) MUST call
``heat.sync_assignments(comp_type)`` so the ``HeatAssignment`` rows used by
the validation service, judge sheets, and exports stay in lockstep. This
test module exercises each mutation entry point and asserts the invariant
post-mutation rather than relying on each callsite to remember.

Each test calls a service-layer mutator in isolation (no route plumbing),
performs the mutation, and re-queries the DB to confirm the JSON list and
HeatAssignment rows agree. If a future change introduces a mutation site
that forgets to call ``sync_assignments``, the test catches the drift
before it ships.

Run: pytest tests/test_heat_sync_invariants.py -v
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

    t = Tournament(name="HeatSyncInvariants", year=2026, status="setup")
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


def _make_heat(session, event, heat_number, comp_ids, stand_assignments):
    from models import Heat

    h = Heat(event_id=event.id, heat_number=heat_number, run_number=1)
    h.competitors = json.dumps(comp_ids)
    h.stand_assignments = json.dumps(stand_assignments)
    session.add(h)
    session.flush()
    return h


def _table_ids(heat_id):
    from models import HeatAssignment

    return {
        a.competitor_id for a in HeatAssignment.query.filter_by(heat_id=heat_id).all()
    }


def _assert_in_sync(heat):
    """Hard invariant: JSON list must match HeatAssignment rows by id."""
    json_ids = set(heat.get_competitors())
    table_ids = _table_ids(heat.id)
    assert (
        json_ids == table_ids
    ), f"Heat {heat.id} drift: JSON={sorted(json_ids)} table={sorted(table_ids)}"


# ---------------------------------------------------------------------------
# Mutation #1: heat.set_competitors() + sync_assignments
# ---------------------------------------------------------------------------


def test_set_competitors_then_sync_keeps_assignments_in_lockstep(db_session):
    t = _make_tournament(db_session)
    ev = _make_event(db_session, t)
    a = _make_pro(db_session, t, "Alice", "F")
    b = _make_pro(db_session, t, "Bob", "M")
    c = _make_pro(db_session, t, "Carol", "F")
    h = _make_heat(db_session, ev, 1, [a.id, b.id], {str(a.id): 1, str(b.id): 2})
    h.sync_assignments(ev.event_type)
    _assert_in_sync(h)

    h.set_competitors([a.id, c.id])
    h.set_stand_assignment(c.id, 2)
    h.sync_assignments(ev.event_type)
    _assert_in_sync(h)


# ---------------------------------------------------------------------------
# Mutation #2: heat.add_competitor()
# ---------------------------------------------------------------------------


def test_add_competitor_then_sync_keeps_assignments_in_lockstep(db_session):
    t = _make_tournament(db_session)
    ev = _make_event(db_session, t)
    a = _make_pro(db_session, t, "A", "M")
    b = _make_pro(db_session, t, "B", "M")
    h = _make_heat(db_session, ev, 1, [a.id], {str(a.id): 1})
    h.sync_assignments(ev.event_type)

    h.add_competitor(b.id)
    h.set_stand_assignment(b.id, 2)
    h.sync_assignments(ev.event_type)

    _assert_in_sync(h)
    assert _table_ids(h.id) == {a.id, b.id}


# ---------------------------------------------------------------------------
# Mutation #3: heat.remove_competitor()
# ---------------------------------------------------------------------------


def test_remove_competitor_then_sync_drops_assignment_row(db_session):
    t = _make_tournament(db_session)
    ev = _make_event(db_session, t)
    a = _make_pro(db_session, t, "A", "M")
    b = _make_pro(db_session, t, "B", "M")
    h = _make_heat(db_session, ev, 1, [a.id, b.id], {str(a.id): 1, str(b.id): 2})
    h.sync_assignments(ev.event_type)

    h.remove_competitor(b.id)
    h.sync_assignments(ev.event_type)

    _assert_in_sync(h)
    assert _table_ids(h.id) == {a.id}


# ---------------------------------------------------------------------------
# Service-layer mutators that internally call sync_assignments
# ---------------------------------------------------------------------------


def test_heat_generator_creates_synced_heats(db_session):
    """Heat generation must leave fresh heats in a synced state without the
    caller having to sync afterwards."""
    from models import Event, Heat
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
        _assert_in_sync(h)


def test_scratch_cascade_keeps_heats_in_sync(db_session):
    """Scratch cascade must call sync_assignments on every touched heat,
    not just remove from the JSON list."""
    from models import Event, Heat, User
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
        _assert_in_sync(h)
        assert target.id not in h.get_competitors()
