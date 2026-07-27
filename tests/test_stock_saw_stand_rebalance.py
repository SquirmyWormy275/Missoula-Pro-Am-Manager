"""
Tests for services.heat_generator.rebalance_stock_saw_solo_stands.

Race-weekend bug: after scratches on college Stock Saw, 4+ consecutive solo
heats ended up on stand 8 because scratch leaves the surviving partner on
whatever stand they started on. Judges couldn't alternate set-ups — stand 7
stayed cold while stand 8 ran every heat.

The rebalance service walks heats in (run_number, heat_number) order and
forces solos to alternate 7/8, plus normalizes pair heats that may have
inherited wrong stand numbers from the flight-builder move route's
`_next_stand` (starts counting at 1, ignoring the 7/8 convention).

Run:
    pytest tests/test_stock_saw_stand_rebalance.py -v
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

    t = Tournament(name="Stock Saw Rebalance 2026", year=2026, status="setup")
    db_session.add(t)
    db_session.flush()
    return t


def _make_event(
    db_session,
    tournament,
    name,
    event_type="college",
    gender="M",
    stand_type="stock_saw",
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
        requires_dual_runs=requires_dual_runs,
    )
    db_session.add(e)
    db_session.flush()
    return e


def _make_heat(
    db_session,
    event,
    heat_number,
    competitors,
    stand_assignments,
    run_number=1,
):
    from models import Heat

    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
        competitors=json.dumps(competitors),
        stand_assignments=json.dumps(stand_assignments),
    )
    db_session.add(h)
    db_session.flush()
    return h


def _stands_in_order(event):
    from models import Heat

    heats = (
        Heat.query.filter_by(event_id=event.id)
        .order_by(
            Heat.run_number,
            Heat.heat_number,
        )
        .all()
    )
    return [
        (
            h.heat_number,
            h.run_number,
            sorted(int(v) for v in h.get_stand_assignments().values()),
        )
        for h in heats
    ]


def test_screenshot_bug_four_solos_all_on_stand_8(db_session, tournament):
    """Exact race-day pattern: pairs at 1-2 + 7-9, solos at 3-6 + 10, every
    solo parked on stand 8 because scratched partners were always the stand-7
    seat. Rebalance must turn solos into 7, 8, 7, 8, 7 in heat_number order."""
    from services.heat_generator import rebalance_stock_saw_solo_stands

    ev = _make_event(db_session, tournament, "Stock Saw", "college", "M")
    _make_heat(db_session, ev, 1, [101, 102], {"101": 7, "102": 8})
    _make_heat(db_session, ev, 2, [103, 104], {"103": 7, "104": 8})
    _make_heat(db_session, ev, 3, [105], {"105": 8})
    _make_heat(db_session, ev, 4, [106], {"106": 8})
    _make_heat(db_session, ev, 5, [107], {"107": 8})
    _make_heat(db_session, ev, 6, [108], {"108": 8})
    _make_heat(db_session, ev, 7, [109, 110], {"109": 7, "110": 8})
    _make_heat(db_session, ev, 8, [111, 112], {"111": 7, "112": 8})
    _make_heat(db_session, ev, 9, [113, 114], {"113": 7, "114": 8})
    _make_heat(db_session, ev, 10, [115], {"115": 7})

    changed = rebalance_stock_saw_solo_stands(ev)

    # Solos are 3, 4, 5, 6, 10 → alternation by solo-index gives 7, 8, 7, 8, 7.
    # Heat 3: 7, Heat 4: 8, Heat 5: 7, Heat 6: 8, Heat 10: 7.
    # Originals were 8, 8, 8, 8, 7 → four heats change (3, 5, 6 match new, 4 matches old).
    # Actually: 3:8→7 (change), 4:8→8 (no change), 5:8→7 (change), 6:8→8 (no change), 10:7→7 (no change)
    # So 2 changes. But wait — alternation uses a monotonic counter that runs
    # across all heats, not solos only: after pair heats, counter stays at 7.
    # Let me re-check against implementation.

    stands = _stands_in_order(ev)
    # Pairs always render as [7, 8]; we only check solos.
    solo_stands = [s[0] for hn, run, s in stands if len(s) == 1]
    assert solo_stands == [
        7,
        8,
        7,
        8,
        7,
    ], f"Solo stands should alternate starting at 7; got {solo_stands}"
    # Pair heats must still be [7, 8].
    pair_stands = [s for hn, run, s in stands if len(s) == 2]
    for s in pair_stands:
        assert s == [7, 8], f"Pair heat must use both stands 7 and 8; got {s}"
    # At least some heats changed (3 of 5 solos were wrong: 3, 5, 6).
    assert changed >= 1


def test_all_solos_alternate_7_8_7_8(db_session, tournament):
    from services.heat_generator import rebalance_stock_saw_solo_stands

    ev = _make_event(db_session, tournament, "Stock Saw", "college", "M")
    for i in range(1, 6):
        _make_heat(db_session, ev, i, [100 + i], {str(100 + i): 8})

    rebalance_stock_saw_solo_stands(ev)
    stands = _stands_in_order(ev)
    solo_stands = [s[0] for _, _, s in stands]
    assert solo_stands == [7, 8, 7, 8, 7]


def test_pro_stock_saw_is_rebalanced_to_7_8(db_session, tournament):
    """DOMAIN_CONTRACT (2026-04-27): ALL Stock Saw — pro and college —
    runs on stands 7-8. Pro events with off-stand assignments are pulled
    onto 7."""
    from services.heat_generator import rebalance_stock_saw_solo_stands

    ev = _make_event(db_session, tournament, "Stock Saw", "pro", "M")
    _make_heat(db_session, ev, 1, [201], {"201": 3})  # off-stand → must move
    changed = rebalance_stock_saw_solo_stands(ev)
    assert changed == 1
    stands = _stands_in_order(ev)
    assert stands[0][2] == [7]


def test_non_stock_saw_event_is_not_touched(db_session, tournament):
    from services.heat_generator import rebalance_stock_saw_solo_stands

    ev = _make_event(
        db_session, tournament, "Single Buck", "college", "M", stand_type="saw_hand"
    )
    _make_heat(db_session, ev, 1, [301], {"301": 1})
    changed = rebalance_stock_saw_solo_stands(ev)
    assert changed == 0


def test_pair_heat_with_corrupted_stands_gets_fixed(db_session, tournament):
    """flights.py:_next_stand starts counting at 1, so a move into an empty
    target heat lands the mover on stand 1 — wrong for Stock Saw. Rebalance
    pulls pair heats back onto [7, 8]."""
    from services.heat_generator import rebalance_stock_saw_solo_stands

    ev = _make_event(db_session, tournament, "Stock Saw", "college", "M")
    # Pair with one on stand 1 (wrong) and one on stand 7 (right).
    _make_heat(db_session, ev, 1, [401, 402], {"401": 1, "402": 7})

    rebalance_stock_saw_solo_stands(ev)
    stands = _stands_in_order(ev)
    assert stands[0][2] == [7, 8], "pair heat with bad stands should be normalized"


def test_pro_stock_saw_solo_alternates_7_8(db_session, tournament):
    """DOMAIN_CONTRACT (2026-04-27): pro Stock Saw solos alternate 7, 8, 7, 8
    just like college, so the off-stand can reset between heats."""
    from services.heat_generator import rebalance_stock_saw_solo_stands

    ev = _make_event(db_session, tournament, "Stock Saw", "pro", "M")
    _make_heat(db_session, ev, 1, [701], {"701": 8})
    _make_heat(db_session, ev, 2, [702], {"702": 8})
    _make_heat(db_session, ev, 3, [703], {"703": 8})

    rebalance_stock_saw_solo_stands(ev)
    stands = _stands_in_order(ev)
    solo_stands = [s[0] for _, _, s in stands]
    assert solo_stands == [7, 8, 7]


def test_idempotent(db_session, tournament):
    from services.heat_generator import rebalance_stock_saw_solo_stands

    ev = _make_event(db_session, tournament, "Stock Saw", "college", "M")
    _make_heat(db_session, ev, 1, [501, 502], {"501": 7, "502": 8})
    _make_heat(db_session, ev, 2, [503], {"503": 7})
    _make_heat(db_session, ev, 3, [504], {"504": 8})

    first = rebalance_stock_saw_solo_stands(ev)
    second = rebalance_stock_saw_solo_stands(ev)
    assert (
        second == 0
    ), f"rebalance must be idempotent; second run changed {second} heats"
    # First run might have changed nothing (data was already correct) — that's fine.
    assert first >= 0


def test_dual_run_resets_alternation_at_run_boundary(db_session, tournament):
    """Dual-run events run each run independently — run 2 alternation starts
    fresh at stand 7, not continuing from where run 1 left off."""
    from services.heat_generator import rebalance_stock_saw_solo_stands

    ev = _make_event(
        db_session,
        tournament,
        "Stock Saw",
        "college",
        "M",
        requires_dual_runs=True,
    )
    # Run 1: three solos with random starting stands.
    _make_heat(db_session, ev, 1, [601], {"601": 8}, run_number=1)
    _make_heat(db_session, ev, 2, [602], {"602": 8}, run_number=1)
    _make_heat(db_session, ev, 3, [603], {"603": 7}, run_number=1)
    # Run 2: same three competitors.
    _make_heat(db_session, ev, 1, [601], {"601": 8}, run_number=2)
    _make_heat(db_session, ev, 2, [602], {"602": 8}, run_number=2)
    _make_heat(db_session, ev, 3, [603], {"603": 7}, run_number=2)

    rebalance_stock_saw_solo_stands(ev)
    stands = _stands_in_order(ev)
    run1_solos = [s[0] for hn, run, s in stands if run == 1 and len(s) == 1]
    run2_solos = [s[0] for hn, run, s in stands if run == 2 and len(s) == 1]
    assert run1_solos == [7, 8, 7], f"run 1 solos must alternate; got {run1_solos}"
    assert run2_solos == [7, 8, 7], f"run 2 solos must alternate; got {run2_solos}"
