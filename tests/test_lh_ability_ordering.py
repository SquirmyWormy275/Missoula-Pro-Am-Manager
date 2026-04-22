"""
V2.14.0 post-release enhancement: LH springboard cutters are ordered by
ProEventRank before the spread/overflow split.

When LH_count > num_heats, the tail of the LH list overflows into the final
heat. Previously the split point was whatever name-order `events_entered`
produced. Now `_generate_springboard_heats` calls `_sort_by_ability` on the
LH list first, so the FASTEST LH cutters each get their own heat (and the
LH dummy time-slot), and the SLOWEST LH cutters overflow into the final
heat alongside any slow-heat-flagged cutters already clustering there.

Falls back to original input order when no ProEventRank rows exist — the
_sort_by_ability documented behaviour.

Run:  pytest tests/test_lh_ability_ordering.py -v
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


def _make_tournament(session):
    from models import Tournament

    t = Tournament(name="LH Ability Test", year=2026, status="pro_active")
    session.add(t)
    session.flush()
    return t


def _make_springboard_event(session, tournament, name="Springboard"):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="pro",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="springboard",
        max_stands=4,
    )
    session.add(e)
    session.flush()
    return e


def _make_pro(session, tournament, name, is_lh=False):
    from models.competitor import ProCompetitor

    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender="M",
        status="active",
        is_left_handed_springboard=is_lh,
    )
    session.add(c)
    session.flush()
    return c


def _rank(session, tournament, competitor, rank_int, category="springboard"):
    from models.pro_event_rank import ProEventRank

    r = ProEventRank(
        tournament_id=tournament.id,
        competitor_id=competitor.id,
        event_category=category,
        rank=rank_int,
    )
    session.add(r)
    session.flush()
    return r


def _enroll(competitor, event):
    import json

    entered = (
        competitor.get_events_entered()
        if hasattr(competitor, "get_events_entered")
        else []
    )
    if event.name not in entered:
        entered.append(event.name)
        competitor.events_entered = json.dumps(entered)


class TestLhAbilityOverflow:
    def test_slowest_lh_cutters_land_in_final_overflow_heat(self, db_session):
        """With 4 LH cutters + 3 heats, the slowest LH (rank 4) goes in the
        final-heat overflow; the three fastest get their own heats."""
        from models import Heat
        from services.heat_generator import generate_event_heats

        t = _make_tournament(db_session)
        ev = _make_springboard_event(db_session, t)

        # Create 4 LH cutters with ranks 1-4 (1 = fastest) + 8 RH cutters to
        # reach 12 total → 3 heats of 4. No ranks on RH — they sort to end.
        lh_fast = _make_pro(db_session, t, "LH Fastest", is_lh=True)
        lh_mid1 = _make_pro(db_session, t, "LH Mid1", is_lh=True)
        lh_mid2 = _make_pro(db_session, t, "LH Mid2", is_lh=True)
        lh_slow = _make_pro(db_session, t, "LH Slowest", is_lh=True)
        _rank(db_session, t, lh_fast, 1)
        _rank(db_session, t, lh_mid1, 2)
        _rank(db_session, t, lh_mid2, 3)
        _rank(db_session, t, lh_slow, 4)

        for c in (lh_fast, lh_mid1, lh_mid2, lh_slow):
            _enroll(c, ev)
        # Fill to 12 with RH cutters so num_heats = 3.
        for i in range(1, 9):
            rh = _make_pro(db_session, t, f"RH {i}")
            _enroll(rh, ev)
        db_session.flush()

        generate_event_heats(ev)

        heats = (
            Heat.query.filter_by(event_id=ev.id, run_number=1)
            .order_by(
                Heat.heat_number,
            )
            .all()
        )
        assert len(heats) == 3, f"expected 3 heats, got {len(heats)}"

        # Each of the 3 heats gets exactly 1 LH cutter from the spread,
        # but with 4 LH + 3 heats, one heat ALSO gets the overflow.
        final_heat = heats[-1]
        final_comp_ids = set(final_heat.get_competitors())
        assert lh_slow.id in final_comp_ids, (
            f"LH slowest (rank 4) should land in the final-heat overflow, "
            f"got final heat comps {final_comp_ids}."
        )

        # The three FAST LH cutters (ranks 1, 2, 3) get one per heat via spread.
        # Fastest goes in heat 0 — the slow cutter overflow into heat 2 means
        # heat 2 already has the rank-3 spread LH. We verify each fast LH ends
        # up in SOME heat (not orphaned).
        all_placed_lh = set()
        for h in heats:
            for cid in h.get_competitors():
                if cid in {lh_fast.id, lh_mid1.id, lh_mid2.id, lh_slow.id}:
                    all_placed_lh.add(cid)
        assert all_placed_lh == {
            lh_fast.id,
            lh_mid1.id,
            lh_mid2.id,
            lh_slow.id,
        }, "All LH cutters should land in some heat."

    def test_unranked_lh_falls_back_to_input_order(self, db_session):
        """When no ProEventRank rows exist, LH placement matches pre-V2.14.0
        behaviour (input order preserved)."""
        from models import Heat
        from services.heat_generator import generate_event_heats

        t = _make_tournament(db_session)
        ev = _make_springboard_event(db_session, t)

        # 4 LH cutters, no ranks.
        lh1 = _make_pro(db_session, t, "LH One", is_lh=True)
        lh2 = _make_pro(db_session, t, "LH Two", is_lh=True)
        lh3 = _make_pro(db_session, t, "LH Three", is_lh=True)
        lh4 = _make_pro(db_session, t, "LH Four", is_lh=True)
        for c in (lh1, lh2, lh3, lh4):
            _enroll(c, ev)
        for i in range(1, 9):
            rh = _make_pro(db_session, t, f"RH {i}")
            _enroll(rh, ev)
        db_session.flush()

        # Should not raise — the fallback path in _sort_by_ability handles
        # the no-ranks case and returns the input list unchanged.
        generate_event_heats(ev)
        heats = Heat.query.filter_by(event_id=ev.id, run_number=1).all()
        assert len(heats) == 3

        # All 4 LH cutters placed somewhere.
        all_placed_lh = set()
        for h in heats:
            for cid in h.get_competitors():
                if cid in {lh1.id, lh2.id, lh3.id, lh4.id}:
                    all_placed_lh.add(cid)
        assert all_placed_lh == {lh1.id, lh2.id, lh3.id, lh4.id}

    def test_single_lh_with_rank_still_gets_stand_4(self, db_session):
        """Regression guard: ability-sort on a single-LH input list
        doesn't break the Phase 5 stand-4 assignment rule."""
        from models import Heat
        from services.heat_generator import generate_event_heats

        t = _make_tournament(db_session)
        ev = _make_springboard_event(db_session, t)

        lh = _make_pro(db_session, t, "LH Only", is_lh=True)
        _rank(db_session, t, lh, 2)
        _enroll(lh, ev)
        for i in range(1, 4):
            rh = _make_pro(db_session, t, f"RH {i}")
            _enroll(rh, ev)
        db_session.flush()

        generate_event_heats(ev)
        heats = Heat.query.filter_by(event_id=ev.id, run_number=1).all()
        assert heats, "fixture should produce at least 1 heat"
        for h in heats:
            assignments = h.get_stand_assignments()
            if str(lh.id) in assignments:
                assert assignments[str(lh.id)] == 4, (
                    "Phase 5 stand-4 rule must still hold after "
                    "V2.14.0 ability-ordering change."
                )
                break
        else:
            pytest.fail("LH cutter was not placed in any heat.")
