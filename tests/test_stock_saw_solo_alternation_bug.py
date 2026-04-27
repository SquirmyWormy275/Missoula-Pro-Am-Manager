"""
Regression tests for Men's Stock Saw solo-heat stand alternation.

Race-day bug (2026-04-23): printed Friday schedule showed 6 consecutive solo
heats all parked on stand 8, three pair heats correctly using 7+8. The
V2.14.13 `rebalance_stock_saw_solo_stands` service IS called at the end of
`generate_event_heats`, but the live printout still showed the regression,
so these tests reproduce end-to-end generation via the real entry point.

Tests here exercise the full `generate_event_heats(event)` path — they do NOT
directly call `rebalance_stock_saw_solo_stands`. That's what the existing
`tests/test_stock_saw_stand_rebalance.py` covers. The point here is to catch
any future drift where initial generation produces same-stand consecutive
solos (either because rebalance stopped being wired, or the initial
assignment breaks before rebalance runs, or a mutation wipes the rebalance
result).
"""

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

    t = Tournament(name="Stock Saw Solo Alternation 2026", year=2026, status="setup")
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture()
def team(db_session, tournament):
    from models import Team

    t = Team(
        tournament_id=tournament.id,
        team_code="CSU-A",
        school_name="Colorado State",
        school_abbreviation="CSU",
    )
    db_session.add(t)
    db_session.flush()
    return t


def _make_stock_saw_event(db_session, tournament, gender="M"):
    from models import Event

    # Name MUST match config: `"Stock Saw"` (gender stored separately).
    e = Event(
        tournament_id=tournament.id,
        name="Stock Saw",
        event_type="college",
        gender=gender,
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="stock_saw",
        max_stands=2,
    )
    db_session.add(e)
    db_session.flush()
    return e


def _make_college_competitor(db_session, tournament, team, name, gender="M"):
    """Create a college competitor enrolled in Stock Saw.

    `events_entered` is a JSON list of event NAMES (the app's convention, see
    CLAUDE.md §4 CollegeCompetitor).
    """
    import json

    from models import CollegeCompetitor

    c = CollegeCompetitor(
        tournament_id=tournament.id,
        team_id=team.id,
        name=name,
        gender=gender,
        events_entered=json.dumps(["Stock Saw"]),
        status="active",
    )
    db_session.add(c)
    db_session.flush()
    return c


def _read_solo_stand_list(event):
    """Return the list of stand numbers for solo heats, in heat order."""
    from models import Heat

    heats = (
        Heat.query.filter_by(event_id=event.id, run_number=1)
        .order_by(Heat.heat_number)
        .all()
    )
    solo_stands = []
    for h in heats:
        assignments = h.get_stand_assignments()
        if len(assignments) == 1:
            (stand,) = assignments.values()
            solo_stands.append(int(stand))
    return solo_stands


def _read_all_stands(event):
    """Return list of (heat_number, sorted stand list) for every run-1 heat."""
    from models import Heat

    heats = (
        Heat.query.filter_by(event_id=event.id, run_number=1)
        .order_by(Heat.heat_number)
        .all()
    )
    return [
        (h.heat_number, sorted(int(v) for v in h.get_stand_assignments().values()))
        for h in heats
    ]


class TestStockSawSoloAlternationOnGeneration:
    """End-to-end: generate heats for Men's Stock Saw via the real service and
    confirm consecutive solo heats alternate between stands 7 and 8."""

    def test_nine_solo_competitors_alternate_7_8(self, db_session, tournament, team):
        """Odd-sized all-solo field — no pairs at all. Every heat is solo, and
        the solo stand must flip every heat."""
        from services.heat_generator import generate_event_heats

        ev = _make_stock_saw_event(db_session, tournament)
        # 9 men, max_stands = 2 → but we want all solos. Force by giving each
        # competitor a unique competitor id and letting snake-draft handle it.
        # With 9 people and max 2 per heat → 5 heats total of sizes [2,2,2,2,1].
        # That's 4 pairs + 1 solo, not all-solos. Use max_stands=1 instead to
        # force 9 solo heats, which is the pure alternation stress test.
        ev.max_stands = 1
        for i in range(1, 10):
            _make_college_competitor(db_session, tournament, team, f"SoloM {i}")
        db_session.flush()

        generate_event_heats(ev)
        db_session.flush()

        solo_stands = _read_solo_stand_list(ev)
        assert (
            len(solo_stands) == 9
        ), f"expected 9 solo heats with max_stands=1; got {len(solo_stands)}"
        # Consecutive heats MUST differ. This is the bug guard.
        for i in range(1, len(solo_stands)):
            assert solo_stands[i] != solo_stands[i - 1], (
                f"consecutive solo heats {i} and {i+1} both on stand "
                f"{solo_stands[i]}; full list: {solo_stands}"
            )
        # And only stands 7 and 8 should appear.
        assert set(solo_stands) <= {
            7,
            8,
        }, f"solo stands must be 7 or 8; got {solo_stands}"

    def test_odd_field_partial_solo_closes_event(self, db_session, tournament, team):
        """7 competitors / 2 per heat = 3 pair heats + 1 solo heat (last).
        The lone solo must be on stand 7 OR 8 (either is fine — alternation
        rule applies across solos, and there's only one solo here).
        """
        from services.heat_generator import generate_event_heats

        ev = _make_stock_saw_event(db_session, tournament)
        for i in range(1, 8):
            _make_college_competitor(db_session, tournament, team, f"OddM {i}")
        db_session.flush()

        generate_event_heats(ev)
        db_session.flush()

        stands = _read_all_stands(ev)
        # Pair heats must use [7, 8].
        pairs = [s for hn, s in stands if len(s) == 2]
        for s in pairs:
            assert s == [7, 8], f"pair heat on wrong stands: {s}"
        # Solo heat must use stand 7 or 8 (no other stands valid).
        solos = [s for hn, s in stands if len(s) == 1]
        assert len(solos) == 1
        assert solos[0][0] in (7, 8)

    def test_race_day_scenario_all_solos_six_heats(self, db_session, tournament, team):
        """Direct match for the race-day complaint: six solo heats in a row
        must alternate 7/8 (or 8/7), never park on the same stand twice in a
        row. Uses max_stands=1 to force all-solo layout."""
        from services.heat_generator import generate_event_heats

        ev = _make_stock_saw_event(db_session, tournament)
        ev.max_stands = 1
        for i in range(1, 7):
            _make_college_competitor(db_session, tournament, team, f"RaceDay {i}")
        db_session.flush()

        generate_event_heats(ev)
        db_session.flush()

        solo_stands = _read_solo_stand_list(ev)
        assert len(solo_stands) == 6
        assert set(solo_stands) <= {7, 8}
        # Must alternate — no two consecutive on the same stand. This is the
        # printed-schedule regression: [8, 8, 8, 8, 8, 8] must never happen.
        distinct_pairs = [
            (solo_stands[i], solo_stands[i + 1]) for i in range(len(solo_stands) - 1)
        ]
        for a, b in distinct_pairs:
            assert (
                a != b
            ), f"consecutive solos on same stand {a}; full list: {solo_stands}"

    def test_after_scratch_solos_re_alternate(self, db_session, tournament, team):
        """Simulate the MEMORY.md race-day bug path: start with 12 competitors
        in 6 pair heats; scratch the stand-7 seat from heats 3-6 so those
        become solos parked on 8; confirm rebalance runs and re-alternates.

        This verifies rebalance is reachable via the actual mutation path
        (scratch), not just via the initial generate."""
        import json

        from models import EventResult
        from services.heat_generator import (
            generate_event_heats,
            rebalance_stock_saw_solo_stands,
        )

        ev = _make_stock_saw_event(db_session, tournament)
        comps = []
        for i in range(1, 13):
            c = _make_college_competitor(db_session, tournament, team, f"Race {i}")
            comps.append(c)
        db_session.flush()

        generate_event_heats(ev)
        db_session.flush()

        # Simulate scratches on heats 3, 4, 5, 6 — remove one competitor each.
        # Use pair heats 3-6. Their stand-7 seat is whoever is first in the
        # competitor list (generator-determined); doesn't matter who we pick,
        # the rebalance must alternate surviving solos across 7/8.
        from models import Heat

        run1_heats = (
            Heat.query.filter_by(event_id=ev.id, run_number=1)
            .order_by(Heat.heat_number)
            .all()
        )
        # Take any 4 pair heats and remove one competitor from each, leaving
        # the remaining one on whatever stand they had.
        target_heats = [h for h in run1_heats if len(h.get_competitors()) == 2][:4]
        assert (
            len(target_heats) == 4
        ), "need at least 4 pair heats to simulate the race-day scratch path"
        for h in target_heats:
            ids = h.get_competitors()
            # Remove the stand-7 seat (whichever competitor is assigned stand 7).
            assignments = h.get_stand_assignments()
            victim = next(
                (int(cid) for cid, stand in assignments.items() if int(stand) == 7),
                ids[0],
            )
            h.remove_competitor(victim)
            stripped = h.get_stand_assignments()
            stripped.pop(str(victim), None)
            h.stand_assignments = json.dumps(stripped)
            # Also mark their EventResult scratched like the real route does.
            result = EventResult.query.filter_by(
                event_id=ev.id, competitor_id=victim, competitor_type="college"
            ).first()
            if result:
                result.status = "scratched"
        db_session.flush()

        # Simulate the mutation hook the scratch route runs after commit.
        rebalance_stock_saw_solo_stands(ev)
        db_session.flush()

        # Check: every survivor-solo heat must alternate 7/8 in heat order.
        # (Pair heats keep 7+8.)
        stands = _read_all_stands(ev)
        solo_stands = [s[0] for hn, s in stands if len(s) == 1]
        assert solo_stands, "expected some solo heats after scratches"
        for i in range(1, len(solo_stands)):
            assert solo_stands[i] != solo_stands[i - 1], (
                f"consecutive survivor solos on same stand after scratch; "
                f"got {solo_stands}"
            )
        assert set(solo_stands) <= {
            7,
            8,
        }, f"survivor solos not on stands 7/8: {solo_stands}"

    def test_rebalance_does_not_mutate_completed_heats(self, db_session, tournament, team):
        """CODEX P2: a mid-event scratch must NOT silently rewrite stands on
        heats that already ran. Score sheets are keyed to the stand recorded
        at run time, so mutating a completed heat's stand assignment after
        the fact corrupts the historical record.

        This test seeds a stock saw event where heats 1-3 are completed
        (with intentionally "wrong" stand 8 assignments — what the race-day
        bug would have produced) and heats 4-6 are pending. After rebalance,
        completed heats must be byte-for-byte unchanged; only pending heats
        get re-alternated.
        """
        import json

        from models import Heat
        from services.heat_generator import (
            generate_event_heats,
            rebalance_stock_saw_solo_stands,
        )

        ev = _make_stock_saw_event(db_session, tournament)
        for i in range(1, 7):
            _make_college_competitor(db_session, tournament, team, f"Solo {i}")
        db_session.flush()

        generate_event_heats(ev)
        db_session.flush()

        run1_heats = (
            Heat.query.filter_by(event_id=ev.id, run_number=1)
            .order_by(Heat.heat_number)
            .all()
        )
        # Force the first 3 heats to look like the race-day bug: all on
        # stand 8, status='completed'. Capture the snapshot for comparison.
        completed_snapshots = {}
        for h in run1_heats[:3]:
            comp_ids = h.get_competitors()
            if not comp_ids:
                continue
            h.stand_assignments = json.dumps({str(comp_ids[0]): 8})
            h.status = "completed"
            completed_snapshots[h.id] = (h.stand_assignments, h.status)
        db_session.flush()

        # Run rebalance — this is the path that scratch_cascade triggers.
        rebalance_stock_saw_solo_stands(ev)
        db_session.flush()

        # Every completed heat must be unchanged.
        for h in Heat.query.filter(Heat.id.in_(completed_snapshots)).all():
            assert (h.stand_assignments, h.status) == completed_snapshots[h.id], (
                f"completed heat {h.id} mutated by rebalance — historical "
                f"stand record corrupted. Before: {completed_snapshots[h.id]}, "
                f"after: ({h.stand_assignments}, {h.status})"
            )

        # Pending heats CAN be touched; alternation should still be correct
        # for the segment after the completed block. (next_solo_stand counter
        # advances through completed heats so pending heats pick up the
        # correct next stand without breaking the 7/8 alternation invariant.)
        pending_solo_stands = []
        for h in run1_heats[3:]:
            comp_ids = h.get_competitors()
            if len(comp_ids) == 1:
                pending_solo_stands.append(
                    int(h.get_stand_assignments().get(str(comp_ids[0])) or 0)
                )
        if len(pending_solo_stands) >= 2:
            for i in range(1, len(pending_solo_stands)):
                assert pending_solo_stands[i] != pending_solo_stands[i - 1], (
                    f"pending solos still piled on same stand: {pending_solo_stands}"
                )
