"""
O3: every dynamic relationship returns rows in its declared order, proven
against a database whose PHYSICAL row order disagrees with it.

These tests are the kill-switch for the order_by clauses added to the nine
lazy='dynamic' relationships in cycle c35. On the normal mirror they pass
whether or not the order_by exists, because the heap mostly agrees with id
order there. They only mean something on the REVERSED template
(proam_prod_mirror_p0rev), where every table's rows were reinserted in
descending key order: there, each assertion below fails the moment its
relationship loses its order_by. The mutation battery runs this module
against the reversed template for exactly that reason.

Run the reversed lane:

    PROAM_RIG_TEMPLATE=proam_prod_mirror_p0rev pytest proam_regression

Build the reversed template (once per rig, as a Postgres superuser):

    createdb proam_prod_mirror_p0rev -T proam_prod_mirror_p0 -O proam
    -- then for every user table except alembic_version:
    SET session_replication_role = replica;
    CREATE TEMP TABLE _rx AS TABLE <t>;
    DELETE FROM <t>;
    INSERT INTO <t> SELECT * FROM _rx ORDER BY <pk> DESC;

WHY THIS EXISTS, measured not argued: the real production heap is NOT in id
order. Pro competitor row 16 sits second (moved by an update); 35 of 64
college rows are displaced. Until c35 the snake draft consumed physical row
order under a comment claiming registration order, which means heat
composition in production depended on Postgres heap placement and could
change after any UPDATE to a competitor row. See the c35 commit for the
full record.
"""

import pytest
import rig

TID = rig.TOURNAMENT_ID


def _is_sorted(seq):
    return all(a <= b for a, b in zip(seq, seq[1:]))


def _tournament():
    from database import db
    from models import Tournament

    return db.session.get(Tournament, TID)


@pytest.mark.sev3
def test_tournament_collections_come_back_in_id_order(app):
    """teams, college_competitors, pro_competitors, events, wood_configs."""
    t = _tournament()
    for rel in ("teams", "college_competitors", "pro_competitors",
                "events", "wood_configs"):
        ids = [row.id for row in getattr(t, rel).all()]
        assert len(ids) > 1, f"{rel}: population too small to prove anything"
        assert _is_sorted(ids), (
            f"tournament.{rel} returned rows out of id order: {ids[:10]}..."
        )


@pytest.mark.sev3
def test_team_members_come_back_in_id_order(app):
    from models.team import Team

    checked = 0
    for team in Team.query.filter_by(tournament_id=TID).all():
        ids = [c.id for c in team.members.all()]
        if len(ids) > 1:
            assert _is_sorted(ids), (
                f"team {team.id} members out of id order: {ids}"
            )
            checked += 1
    assert checked > 0, "no team with more than one member; vacuous run"


@pytest.mark.sev3
def test_event_heats_come_back_in_run_then_heat_order(app):
    from models.event import Event

    checked = 0
    for ev in Event.query.filter_by(tournament_id=TID).all():
        keys = [(h.run_number, h.heat_number, h.id) for h in ev.heats.all()]
        if len(keys) > 1:
            assert keys == sorted(keys), (
                f"event {ev.id} heats out of (run, heat, id) order: {keys}"
            )
            checked += 1
    assert checked > 0, "no event with more than one heat; vacuous run"


@pytest.mark.sev3
def test_event_results_come_back_in_id_order(app):
    from models.event import Event

    checked = 0
    for ev in Event.query.filter_by(tournament_id=TID).all():
        ids = [r.id for r in ev.results.all()]
        if len(ids) > 1:
            assert _is_sorted(ids), (
                f"event {ev.id} results out of id order: {ids[:10]}..."
            )
            checked += 1
    assert checked > 0, "no event with more than one result; vacuous run"


@pytest.mark.sev3
def test_flight_heats_come_back_in_position_order(app):
    from models.heat import Flight

    checked = 0
    for fl in Flight.query.filter_by(tournament_id=TID).all():
        keys = [(h.flight_position if h.flight_position is not None else -1,
                 h.id) for h in fl.heats.all()]
        # The relationship orders by (flight_position, id). NULL placement is
        # backend-defined and callers who care use get_heats_ordered; here we
        # assert the non-NULL run is monotone, which is what the relationship
        # guarantees portably.
        non_null = [k for k in keys if k[0] != -1]
        if len(non_null) > 1:
            assert non_null == sorted(non_null), (
                f"flight {fl.id} heats out of (position, id) order: {keys}"
            )
            checked += 1
    assert checked > 0, "no flight with positioned heats; vacuous run"


@pytest.mark.sev3
def test_the_snake_draft_input_is_registration_order_not_heap_order(app):
    """The c35 heat_generator fix, asserted at its own layer: the competitor
    list handed to the draft is id-ordered no matter what order the heap
    returns rows in."""
    from models.event import Event
    from services.heat_generator import _get_event_competitors

    ev = Event.query.filter_by(tournament_id=TID, name="Underhand",
                               event_type="pro", gender="M").first()
    assert ev is not None, "pro Men's Underhand missing from the mirror"
    comps = _get_event_competitors(ev)
    ids = [c["id"] for c in comps]
    assert len(ids) > 10
    assert _is_sorted(ids), (
        f"snake draft input is not registration order: {ids}"
    )


@pytest.mark.sev3
def test_entity_key_adoption_never_goes_backwards(app):
    """c40 ratchet. services/entity_key.py sat imported by nothing while
    hand-rolled pair-keying spread to six sites (backlog section E class).
    This counts production importers and refuses to let the number shrink;
    raise the floor as call sites convert. The lint rule proper (banning new
    bare-int competitor keys outside entity_key) is filed as tranche-2 work;
    a ratchet is what fits in a test today."""
    import pathlib
    import re

    root = pathlib.Path(rig.APP_ROOT)
    importers = set()
    for sub in ("routes", "services"):
        for p in (root / sub).rglob("*.py"):
            if p.name == "entity_key.py":
                continue
            if re.search(r"from services\.entity_key import|import services\.entity_key",
                         p.read_text(encoding="utf-8")):
                importers.add(str(p.relative_to(root)))
    FLOOR = 1  # c40: routes/scheduling/flights.py (the c29 SMS site)
    assert len(importers) >= FLOOR, (
        f"EntityKey production importers fell below the ratchet floor "
        f"({FLOOR}): {sorted(importers)}"
    )
