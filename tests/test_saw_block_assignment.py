"""
Tests for services.saw_block_assignment.

Covers block alternation within a saw event, cross-event continuation on
the same day, non-saw-gap preservation of alternation state, day boundary
reset, partnered event pair preservation, idempotency, pre-flight vs
post-flight recompute, and exclusion of non-saw events.

Run:
    pytest tests/test_saw_block_assignment.py -v
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
def tournament(db_session):
    from models import Tournament

    t = Tournament(name="SawBlock Test 2026", year=2026, status="setup")
    db_session.add(t)
    db_session.flush()
    return t


def _make_event(
    db_session,
    tournament,
    name,
    event_type="college",
    gender="M",
    stand_type="saw_hand",
    is_partnered=False,
    requires_dual_runs=False,
):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender=gender,
        scoring_type="time",
        stand_type=stand_type,
        is_partnered=is_partnered,
        requires_dual_runs=requires_dual_runs,
    )
    db_session.add(e)
    db_session.flush()
    return e


def _make_flight(db_session, tournament, flight_number):
    from models.heat import Flight

    f = Flight(tournament_id=tournament.id, flight_number=flight_number)
    db_session.add(f)
    db_session.flush()
    return f


def _make_heat(
    db_session,
    event,
    heat_number,
    competitors,
    stand_assignments,
    run_number=1,
    flight=None,
    flight_position=None,
):
    """Create a Heat with pre-populated competitors + stand_assignments JSON."""
    from models import Heat

    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
        competitors=json.dumps(competitors),
        stand_assignments=json.dumps(stand_assignments),
    )
    if flight is not None:
        h.flight_id = flight.id
        h.flight_position = flight_position
    db_session.add(h)
    db_session.flush()
    return h


def _assignments(heat):
    """Convenience: return {int(comp_id): int(stand)} for a heat."""
    return {int(k): int(v) for k, v in heat.get_stand_assignments().items()}


# ---------------------------------------------------------------------------
# 1. Within-event alternation (college Single Buck, 3 heats)
# ---------------------------------------------------------------------------


def test_within_event_alternation_single_buck(db_session, tournament):
    from services.saw_block_assignment import assign_saw_blocks, BLOCK_A, BLOCK_B

    sb = _make_event(db_session, tournament, "Single Buck", "college", "M")

    # 3 heats of 4 competitors each, all currently on stands 1-4 (Block A)
    h1 = _make_heat(
        db_session,
        sb,
        1,
        competitors=[1, 2, 3, 4],
        stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
    )
    h2 = _make_heat(
        db_session,
        sb,
        2,
        competitors=[5, 6, 7, 8],
        stand_assignments={"5": 1, "6": 2, "7": 3, "8": 4},
    )
    h3 = _make_heat(
        db_session,
        sb,
        3,
        competitors=[9, 10, 11, 12],
        stand_assignments={"9": 1, "10": 2, "11": 3, "12": 4},
    )

    summary = assign_saw_blocks(tournament)

    # heat 1 -> Block A (1-4), heat 2 -> Block B (5-8), heat 3 -> Block A
    assert set(_assignments(h1).values()) == set(BLOCK_A)
    assert set(_assignments(h2).values()) == set(BLOCK_B)
    assert set(_assignments(h3).values()) == set(BLOCK_A)
    assert summary["friday_saw_heats"] == 3


# ---------------------------------------------------------------------------
# 2. Cross-event continuation on Friday
# ---------------------------------------------------------------------------


def test_cross_event_continuation_friday(db_session, tournament):
    """Single Buck ends on Block B (odd heat count) -> next saw event starts Block A."""
    from services.saw_block_assignment import assign_saw_blocks, BLOCK_A, BLOCK_B

    sb = _make_event(db_session, tournament, "Single Buck", "college", "M")
    jj = _make_event(
        db_session,
        tournament,
        "Jack & Jill Sawing",
        "college",
        gender=None,
        is_partnered=True,
    )
    dbuck = _make_event(
        db_session, tournament, "Double Buck", "college", "M", is_partnered=True
    )

    # Force Friday order: SB -> JJ -> DB
    tournament.set_schedule_config(
        {
            "friday_event_order": [sb.id, jj.id, dbuck.id],
        }
    )

    # SB: 1 heat (ends Block A)  -> then JJ first heat must be Block B
    h_sb1 = _make_heat(
        db_session,
        sb,
        1,
        competitors=[1, 2, 3, 4],
        stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
    )
    # JJ: 2 pairs, 1 heat  -> Block B -> next DB heat Block A
    h_jj1 = _make_heat(
        db_session,
        jj,
        1,
        competitors=[10, 11, 12, 13],
        stand_assignments={"10": 1, "11": 1, "12": 2, "13": 2},
    )
    h_db1 = _make_heat(
        db_session,
        dbuck,
        1,
        competitors=[20, 21, 22, 23],
        stand_assignments={"20": 1, "21": 1, "22": 2, "23": 2},
    )

    db_session.flush()
    assign_saw_blocks(tournament)

    assert set(_assignments(h_sb1).values()) == set(BLOCK_A)
    # JJ inherits Block B (flip from SB's Block A). Only 2 pair-stands used
    # (stands 1 & 2 pre-remap); they map to the first 2 positions of BLOCK_B.
    jj_stands = set(_assignments(h_jj1).values())
    assert jj_stands.issubset(set(BLOCK_B))
    assert jj_stands == {5, 6}
    # DB continues flip -> Block A. Same 2-pair structure -> stands 1 & 2.
    db_stands = set(_assignments(h_db1).values())
    assert db_stands.issubset(set(BLOCK_A))
    assert db_stands == {1, 2}


# ---------------------------------------------------------------------------
# 3. Non-saw gap preserves alternation
# ---------------------------------------------------------------------------


def test_non_saw_gap_preserves_continuity(db_session, tournament):
    """Saw heat N+1 still flips from saw heat N even with non-saw heats between."""
    from services.saw_block_assignment import assign_saw_blocks, BLOCK_A, BLOCK_B

    sb = _make_event(db_session, tournament, "Single Buck", "college", "M")
    underhand = _make_event(
        db_session,
        tournament,
        "Underhand Speed",
        "college",
        "M",
        stand_type="underhand",
    )

    tournament.set_schedule_config(
        {
            "friday_event_order": [
                sb.id,
                underhand.id,
                sb.id,
            ],  # SB -> UH -> (SB already in list)
        }
    )

    # SB heat 1 -> A, SB heat 2 -> B (not A, despite UH gap)
    h_sb1 = _make_heat(
        db_session,
        sb,
        1,
        competitors=[1, 2, 3, 4],
        stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
    )
    h_sb2 = _make_heat(
        db_session,
        sb,
        2,
        competitors=[5, 6, 7, 8],
        stand_assignments={"5": 1, "6": 2, "7": 3, "8": 4},
    )
    # Underhand heat — not saw_hand, should not affect alternation
    h_uh1 = _make_heat(
        db_session,
        underhand,
        1,
        competitors=[30, 31, 32],
        stand_assignments={"30": 1, "31": 2, "32": 3},
    )

    db_session.flush()
    assign_saw_blocks(tournament)

    assert set(_assignments(h_sb1).values()) == set(BLOCK_A)
    assert set(_assignments(h_sb2).values()) == set(BLOCK_B)
    # Underhand untouched — still on its original stands
    assert _assignments(h_uh1) == {30: 1, 31: 2, 32: 3}


# ---------------------------------------------------------------------------
# 4. Day boundary resets to Block A
# ---------------------------------------------------------------------------


def test_day_boundary_resets(db_session, tournament):
    """Saturday saw heats restart from Block A regardless of Friday's end state."""
    from services.saw_block_assignment import assign_saw_blocks, BLOCK_A

    # Friday: 1 college SB heat (ends on Block A, so Friday ends mid-cycle)
    sb_coll = _make_event(db_session, tournament, "Single Buck", "college", "M")
    h_fri = _make_heat(
        db_session,
        sb_coll,
        1,
        competitors=[1, 2, 3, 4],
        stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
    )

    # Saturday: pro saw events, no flights
    sb_pro = _make_event(db_session, tournament, "Single Buck", "pro", "M")
    h_sat = _make_heat(
        db_session,
        sb_pro,
        1,
        competitors=[50, 51, 52, 53],
        stand_assignments={"50": 1, "51": 2, "52": 3, "53": 4},
    )

    db_session.flush()
    assign_saw_blocks(tournament)

    # Friday's first (and only) heat is Block A
    assert set(_assignments(h_fri).values()) == set(BLOCK_A)
    # Saturday's first heat ALSO Block A — reset, not continuation
    assert set(_assignments(h_sat).values()) == set(BLOCK_A)


# ---------------------------------------------------------------------------
# 5. Partnered event preserves pair-sharing
# ---------------------------------------------------------------------------


def test_partnered_event_pair_preservation(db_session, tournament):
    """Double Buck with 2 pairs: pairs share stand, and remap preserves that."""
    from services.saw_block_assignment import assign_saw_blocks

    sb = _make_event(db_session, tournament, "Single Buck", "college", "M")
    dbuck = _make_event(
        db_session, tournament, "Double Buck", "college", "M", is_partnered=True
    )

    tournament.set_schedule_config(
        {
            "friday_event_order": [sb.id, dbuck.id],
        }
    )

    # SB heat 1 -> Block A
    _make_heat(
        db_session,
        sb,
        1,
        competitors=[1, 2, 3, 4],
        stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
    )
    # DB heat 1 starts on {10:1, 11:1, 12:2, 13:2} (2 pairs on stands 1+2)
    h_db = _make_heat(
        db_session,
        dbuck,
        1,
        competitors=[10, 11, 12, 13],
        stand_assignments={"10": 1, "11": 1, "12": 2, "13": 2},
    )

    db_session.flush()
    assign_saw_blocks(tournament)

    # SB on Block A -> DB flips to Block B.
    # Pair-sharing structure: 10&11 share one stand, 12&13 share another.
    # Block B stands used: 5 and 6 (the first two positions of BLOCK_B).
    assigns = _assignments(h_db)
    assert assigns[10] == assigns[11]
    assert assigns[12] == assigns[13]
    assert assigns[10] != assigns[12]
    assert assigns[10] == 5
    assert assigns[12] == 6


# ---------------------------------------------------------------------------
# 6. Jack & Jill mixed-gender pair remap
# ---------------------------------------------------------------------------


def test_jack_and_jill_mixed_gender_assignment(db_session, tournament):
    from services.saw_block_assignment import assign_saw_blocks

    jj = _make_event(
        db_session,
        tournament,
        "Jack & Jill Sawing",
        "college",
        gender=None,
        is_partnered=True,
    )
    # 2 mixed-gender pairs, starting on Block A stands 1 & 2
    h = _make_heat(
        db_session,
        jj,
        1,
        competitors=[100, 101, 102, 103],
        stand_assignments={"100": 1, "101": 1, "102": 2, "103": 2},
    )

    db_session.flush()
    assign_saw_blocks(tournament)

    # First saw heat of Friday -> Block A, so {1,1,2,2} stays identical
    a = _assignments(h)
    assert a[100] == a[101]
    assert a[102] == a[103]
    assert a[100] != a[102]
    assert {a[100], a[102]} == {1, 2}


# ---------------------------------------------------------------------------
# 7. Idempotent re-run
# ---------------------------------------------------------------------------


def test_idempotent_rerun(db_session, tournament):
    from services.saw_block_assignment import assign_saw_blocks

    sb = _make_event(db_session, tournament, "Single Buck", "college", "M")
    _make_heat(
        db_session,
        sb,
        1,
        competitors=[1, 2, 3, 4],
        stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
    )
    _make_heat(
        db_session,
        sb,
        2,
        competitors=[5, 6, 7, 8],
        stand_assignments={"5": 1, "6": 2, "7": 3, "8": 4},
    )

    assign_saw_blocks(tournament)  # first pass: heat 2 moves from A to B
    second = assign_saw_blocks(tournament)  # second pass: no changes

    assert second["heats_updated"] == 0
    assert second["heats_unchanged"] == 2


# ---------------------------------------------------------------------------
# 8. Flight builder reshuffle -> recompute reflects new run order
# ---------------------------------------------------------------------------


def test_flight_builder_reshuffle_recomputes_correctly(db_session, tournament):
    """Pre-flight assignment differs from post-flight. Re-running after flight
    build gives correct blocks for the new run order."""
    from services.saw_block_assignment import assign_saw_blocks, BLOCK_A, BLOCK_B

    sb = _make_event(db_session, tournament, "Single Buck", "pro", "M")

    # 2 pro saw heats — no flights yet (pre-flight fallback)
    h_a = _make_heat(
        db_session,
        sb,
        1,
        competitors=[1, 2, 3, 4],
        stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
    )
    h_b = _make_heat(
        db_session,
        sb,
        2,
        competitors=[5, 6, 7, 8],
        stand_assignments={"5": 1, "6": 2, "7": 3, "8": 4},
    )

    assign_saw_blocks(tournament)
    # Pre-flight order: h_a (heat_number 1) then h_b (heat_number 2)
    assert set(_assignments(h_a).values()) == set(BLOCK_A)
    assert set(_assignments(h_b).values()) == set(BLOCK_B)

    # Simulate flight-build reshuffling: h_b runs first, then h_a
    f1 = _make_flight(db_session, tournament, 1)
    h_b.flight_id = f1.id
    h_b.flight_position = 1
    h_a.flight_id = f1.id
    h_a.flight_position = 2
    db_session.flush()

    assign_saw_blocks(tournament)

    # Now h_b is first -> Block A, h_a is second -> Block B
    assert set(_assignments(h_b).values()) == set(BLOCK_A)
    assert set(_assignments(h_a).values()) == set(BLOCK_B)


# ---------------------------------------------------------------------------
# 9. Stock Saw is unaffected
# ---------------------------------------------------------------------------


def test_stock_saw_unaffected(db_session, tournament):
    """Non-saw_hand stand types are not touched."""
    from services.saw_block_assignment import assign_saw_blocks

    stock = _make_event(
        db_session, tournament, "Stock Saw", "college", "M", stand_type="stock_saw"
    )

    # Stock saw runs on stands 7-8 per Missoula rule
    h = _make_heat(
        db_session, stock, 1, competitors=[1, 2], stand_assignments={"1": 7, "2": 8}
    )

    assign_saw_blocks(tournament)

    assert _assignments(h) == {1: 7, 2: 8}


# ---------------------------------------------------------------------------
# 10. HeatAssignment sync after remap
# ---------------------------------------------------------------------------


def test_sync_assignments_called(db_session, tournament):
    """After assign_saw_blocks, HeatAssignment rows match the JSON."""
    from models import HeatAssignment
    from services.saw_block_assignment import assign_saw_blocks

    sb = _make_event(db_session, tournament, "Single Buck", "college", "M")
    h1 = _make_heat(
        db_session,
        sb,
        1,
        competitors=[1, 2, 3, 4],
        stand_assignments={"1": 1, "2": 2, "3": 3, "4": 4},
    )
    h2 = _make_heat(
        db_session,
        sb,
        2,
        competitors=[5, 6, 7, 8],
        stand_assignments={"5": 1, "6": 2, "7": 3, "8": 4},
    )

    # Seed HeatAssignment rows that reflect the pre-remap state so we can
    # verify they get rewritten.
    h1.sync_assignments("college")
    h2.sync_assignments("college")
    db_session.flush()

    assign_saw_blocks(tournament)

    # For each heat, HeatAssignment rows must match stand_assignments JSON
    for heat in (h1, h2):
        json_map = heat.get_stand_assignments()
        rows = HeatAssignment.query.filter_by(heat_id=heat.id).all()
        assert len(rows) == len(json_map)
        for row in rows:
            assert row.stand_number == json_map.get(str(row.competitor_id))
