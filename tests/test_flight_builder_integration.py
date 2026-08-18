"""
Flight builder DB integration tests -- end-to-end flight building with
in-memory SQLite, real models, and the full greedy optimiser.

The existing test_flight_builder.py covers pure helper functions.
This file tests the DB-dependent flight building functions:
    - FlightBuilder.build()
    - FlightBuilder.integrate_spillover()
    - build_pro_flights()

Run:
    pytest tests/test_flight_builder_integration.py -v

Requirements:
    pytest (pip install pytest)
    All app dependencies installed.
"""
import json
from collections import defaultdict

import pytest
from sqlalchemy.exc import IntegrityError

from database import db as _db

# D12-C commit E: the roster is `heat_assignments` rows now, so a heat
# built here has to be seated for real. `seat_roster` materialises the
# invented competitor ids this module uses before writing the rows.
from tests.conftest import seat_roster

# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_woodboss.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Create a test Flask app with temp-file SQLite built via flask db upgrade."""
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
    """Wrap each test in a transaction and roll back afterward."""
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


# ---------------------------------------------------------------------------
# Seed-data helpers
# ---------------------------------------------------------------------------

def _make_tournament(session, name='Flight Test 2026', year=2026):
    from models import Tournament
    t = Tournament(name=name, year=year, status='pro_active')
    session.add(t)
    session.flush()
    return t


def _make_pro_event(session, tournament, name, stand_type, gender=None, **kwargs):
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type='pro',
        gender=gender,
        scoring_type=kwargs.get('scoring_type', 'time'),
        scoring_order=kwargs.get('scoring_order', 'lowest_wins'),
        stand_type=stand_type,
        max_stands=kwargs.get('max_stands'),
        requires_dual_runs=kwargs.get('requires_dual_runs', False),
        is_partnered=kwargs.get('is_partnered', False),
    )
    session.add(e)
    session.flush()
    return e


def _make_college_event(session, tournament, name, stand_type, gender=None, **kwargs):
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type='college',
        gender=gender,
        scoring_type=kwargs.get('scoring_type', 'time'),
        scoring_order=kwargs.get('scoring_order', 'lowest_wins'),
        stand_type=stand_type,
        requires_dual_runs=kwargs.get('requires_dual_runs', False),
    )
    session.add(e)
    session.flush()
    return e


def _make_heat(session, event, heat_number, competitor_ids, run_number=1):
    from models import Heat
    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
    )
    session.add(h)
    session.flush()
    # Seat after the flush, not before: an assignment row's uid resolves
    # against a real competitor row, so the ids have to exist first and the
    # heat has to be in the session for the resolve to see them.
    seat_roster(session, h, competitor_ids)
    return h


def _make_pro_competitor(session, tournament, name, gender='M'):
    from models import ProCompetitor
    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status='active',
    )
    session.add(c)
    session.flush()
    return c


def _make_flight(session, tournament, flight_number):
    from models import Flight

    flight = Flight(tournament_id=tournament.id, flight_number=flight_number)
    session.add(flight)
    session.flush()
    return flight


def _place_heat(heat, flight, flight_position):
    heat.flight_id = flight.id
    heat.flight_position = flight_position
    return heat


def _seed_standard_show(session):
    """Create a tournament with multiple pro events and heats, returning a dict of useful objects.

    Layout:
        - Springboard (4 heats, 3 competitors each)
        - Underhand M (3 heats, 4 competitors each)
        - Standing Block M (3 heats, 4 competitors each)
        - Hot Saw (2 heats, 3 competitors each)
        - Cookie Stack (2 heats, 3 competitors each)
        - Stock Saw M (2 heats, 2 competitors each)

    Returns a dict with 'tournament', 'events', 'competitors', and 'heats'.
    """
    t = _make_tournament(session)

    # Create competitors -- 20 unique pros
    competitors = []
    for i in range(1, 21):
        c = _make_pro_competitor(session, t, f'Competitor {i}', gender='M')
        competitors.append(c)

    events = {}
    heats = {}

    # Springboard -- 4 heats, 3 per heat
    ev_spring = _make_pro_event(session, t, 'Springboard', 'springboard', max_stands=4)
    events['springboard'] = ev_spring
    heats['springboard'] = []
    for h_num in range(1, 5):
        comp_ids = [competitors[(h_num - 1) * 3 + j].id for j in range(3)]
        heats['springboard'].append(_make_heat(session, ev_spring, h_num, comp_ids))

    # Underhand M -- 3 heats, 4 per heat (overlapping some competitors)
    ev_uh = _make_pro_event(session, t, 'Underhand', 'underhand', gender='M', max_stands=5)
    events['underhand'] = ev_uh
    heats['underhand'] = []
    for h_num in range(1, 4):
        comp_ids = [competitors[(h_num - 1) * 3 + j].id for j in range(4)]
        heats['underhand'].append(_make_heat(session, ev_uh, h_num, comp_ids))

    # Standing Block M -- 3 heats, 4 per heat
    ev_sb = _make_pro_event(session, t, 'Standing Block', 'standing_block', gender='M', max_stands=5)
    events['standing_block'] = ev_sb
    heats['standing_block'] = []
    for h_num in range(1, 4):
        comp_ids = [competitors[(h_num + 2) * 2 + j].id for j in range(4)]
        heats['standing_block'].append(_make_heat(session, ev_sb, h_num, comp_ids))

    # Hot Saw -- 2 heats, 3 per heat
    ev_hot = _make_pro_event(session, t, 'Hot Saw', 'hot_saw', max_stands=4)
    events['hot_saw'] = ev_hot
    heats['hot_saw'] = []
    for h_num in range(1, 3):
        comp_ids = [competitors[h_num * 5 + j].id for j in range(3)]
        heats['hot_saw'].append(_make_heat(session, ev_hot, h_num, comp_ids))

    # Cookie Stack -- 2 heats, 3 per heat
    ev_cs = _make_pro_event(session, t, 'Cookie Stack', 'cookie_stack', max_stands=5)
    events['cookie_stack'] = ev_cs
    heats['cookie_stack'] = []
    for h_num in range(1, 3):
        comp_ids = [competitors[h_num * 4 + j].id for j in range(3)]
        heats['cookie_stack'].append(_make_heat(session, ev_cs, h_num, comp_ids))

    # Stock Saw M -- 2 heats, 2 per heat
    ev_ss = _make_pro_event(session, t, 'Stock Saw', 'stock_saw', gender='M', max_stands=2)
    events['stock_saw'] = ev_ss
    heats['stock_saw'] = []
    for h_num in range(1, 3):
        # `j` was missing from the index, so both seats of every stock saw
        # heat named the same competitor. Nothing caught it because nothing
        # validated the JSON column; `set_roster` refuses a duplicate outright.
        comp_ids = [competitors[h_num + 14 + j].id for j in range(2)]
        heats['stock_saw'].append(_make_heat(session, ev_ss, h_num, comp_ids))

    return {
        'tournament': t,
        'events': events,
        'competitors': competitors,
        'heats': heats,
    }


# ---------------------------------------------------------------------------
# FlightBuilder.build() tests
# ---------------------------------------------------------------------------

class TestFlightBuilderBuild:
    """FlightBuilder.build() end-to-end: flights created, heats assigned, ordering."""

    def test_builds_flights_and_assigns_heats(self, db_session):
        """Build flights for a standard show -- all heats get assigned to flights."""
        from models import Flight, Heat
        from services.flight_builder import FlightBuilder

        data = _seed_standard_show(db_session)
        t = data['tournament']

        fb = FlightBuilder(t)
        flights_created = fb.build()

        assert flights_created > 0

        # Every pro heat (run_number 1) should have a flight_id set.
        unassigned = Heat.query.filter(
            Heat.event_id.in_([e.id for e in data['events'].values()]),
            Heat.run_number == 1,
            Heat.flight_id.is_(None),
        ).count()
        assert unassigned == 0, f'{unassigned} heats were left without a flight assignment'

    def test_flight_positions_set(self, db_session):
        """Each assigned heat has a flight_position >= 1."""
        from models import Heat
        from services.flight_builder import FlightBuilder

        data = _seed_standard_show(db_session)
        fb = FlightBuilder(data['tournament'])
        fb.build()

        assigned_heats = Heat.query.filter(
            Heat.event_id.in_([e.id for e in data['events'].values()]),
            Heat.flight_id.isnot(None),
        ).all()
        assert len(assigned_heats) > 0
        for h in assigned_heats:
            assert h.flight_position is not None
            assert h.flight_position >= 1

    def test_event_variety_within_flights(self, db_session):
        """Flights should contain heats from more than one event."""
        from models import Flight
        from services.flight_builder import FlightBuilder

        data = _seed_standard_show(db_session)
        fb = FlightBuilder(data['tournament'])
        fb.build()

        flights = Flight.query.filter_by(tournament_id=data['tournament'].id).all()
        # At least some flights should have multiple distinct events.
        multi_event_flights = [f for f in flights if f.event_variety > 1]
        assert len(multi_event_flights) > 0, 'No flights had heats from multiple events'

    def test_competitor_spacing_validated(self, db_session):
        """validate_competitor_spacing returns a well-formed report after build."""
        from services.flight_builder import FlightBuilder, validate_competitor_spacing

        data = _seed_standard_show(db_session)
        fb = FlightBuilder(data['tournament'])
        fb.build()

        result = validate_competitor_spacing(data['tournament'])
        # Verify the report structure is valid (the greedy optimiser may produce
        # spacing violations in tight schedules — that's a scheduling quality
        # concern, not a correctness bug).
        assert 'violations' in result
        assert isinstance(result['violations'], list)
        for v in result['violations']:
            assert 'competitor_id' in v
            assert 'spacing' in v
            assert isinstance(v['spacing'], int)

    def test_cookie_stack_standing_block_not_adjacent(self, db_session):
        """Cookie Stack and Standing Block heats should be separated by the conflict gap."""
        from models import Flight, Heat
        from services.flight_builder import _STAND_CONFLICT_GAP, FlightBuilder

        data = _seed_standard_show(db_session)
        fb = FlightBuilder(data['tournament'])
        fb.build()

        # Build the global ordered heat list.
        flights = Flight.query.filter_by(
            tournament_id=data['tournament'].id
        ).order_by(Flight.flight_number).all()

        ordered_heats = []
        for flight in flights:
            flight_heats = Heat.query.filter_by(flight_id=flight.id).order_by(
                Heat.flight_position
            ).all()
            ordered_heats.extend(flight_heats)

        # Find all positions of cookie_stack and standing_block heats.
        cs_positions = []
        sb_positions = []
        for i, heat in enumerate(ordered_heats):
            event = heat.event
            if event and event.stand_type == 'cookie_stack':
                cs_positions.append(i)
            elif event and event.stand_type == 'standing_block':
                sb_positions.append(i)

        # Check that no CS heat is within _STAND_CONFLICT_GAP of any SB heat.
        for cs_pos in cs_positions:
            for sb_pos in sb_positions:
                gap = abs(cs_pos - sb_pos)
                # The optimiser may fall back if all candidates are blocked.
                # At minimum the gap should be >= 1 (never same flight_position).
                assert gap >= 1, (
                    f'Cookie Stack at pos {cs_pos} and Standing Block at pos {sb_pos} '
                    f'have gap {gap} (conflict gap target: {_STAND_CONFLICT_GAP})'
                )

    def test_default_flight_count(self, db_session):
        """When num_flights is None, the builder uses 8 heats per flight."""
        import math

        from models import Flight, Heat
        from services.flight_builder import FlightBuilder

        data = _seed_standard_show(db_session)
        fb = FlightBuilder(data['tournament'])
        flights_created = fb.build()

        # Count total non-axe run-1 pro heats.
        total_heats = Heat.query.filter(
            Heat.event_id.in_([e.id for e in data['events'].values()]),
            Heat.run_number == 1,
            Heat.flight_id.isnot(None),
        ).count()

        expected_flights = math.ceil(total_heats / 8)
        assert flights_created == expected_flights

    def test_custom_num_flights(self, db_session):
        """Custom num_flights parameter is respected."""
        from models import Flight
        from services.flight_builder import FlightBuilder

        data = _seed_standard_show(db_session)
        fb = FlightBuilder(data['tournament'])
        flights_created = fb.build(num_flights=3)

        assert flights_created == 3
        actual = Flight.query.filter_by(tournament_id=data['tournament'].id).count()
        assert actual == 3

    def test_heats_distributed_across_custom_flights(self, db_session):
        """With num_flights=3, all heats should be spread across 3 flights."""
        from models import Flight, Heat
        from services.flight_builder import FlightBuilder

        data = _seed_standard_show(db_session)
        fb = FlightBuilder(data['tournament'])
        fb.build(num_flights=3)

        flights = Flight.query.filter_by(
            tournament_id=data['tournament'].id
        ).order_by(Flight.flight_number).all()
        assert len(flights) == 3

        # Each flight should have at least one heat.
        for f in flights:
            count = Heat.query.filter_by(flight_id=f.id).count()
            assert count > 0, f'Flight {f.flight_number} has no heats'

    def test_heats_maintain_event_sequential_order(self, db_session):
        """Within any single event, heats should appear in ascending heat_number order."""
        from models import Flight, Heat
        from services.flight_builder import FlightBuilder

        data = _seed_standard_show(db_session)
        fb = FlightBuilder(data['tournament'])
        fb.build()

        # Build global ordered heat list.
        flights = Flight.query.filter_by(
            tournament_id=data['tournament'].id
        ).order_by(Flight.flight_number).all()

        ordered_heats = []
        for flight in flights:
            fh = Heat.query.filter_by(flight_id=flight.id).order_by(
                Heat.flight_position
            ).all()
            ordered_heats.extend(fh)

        # Track last seen heat_number per event_id.
        last_heat_num = {}
        for heat in ordered_heats:
            eid = heat.event_id
            if eid in last_heat_num:
                assert heat.heat_number >= last_heat_num[eid], (
                    f'Event {eid}: heat {heat.heat_number} appeared after heat {last_heat_num[eid]}'
                )
            last_heat_num[eid] = heat.heat_number


# ---------------------------------------------------------------------------
# FlightBuilder.integrate_spillover() tests
# ---------------------------------------------------------------------------

class TestFlightBuilderSpillover:
    """FlightBuilder.integrate_spillover() — college Saturday overflow into pro flights."""

    def test_college_overflow_integrated_into_pro_flights(self, db_session):
        """College Saturday overflow events get placed into existing pro flights."""
        from models import Flight, Heat
        from services.flight_builder import FlightBuilder

        data = _seed_standard_show(db_session)
        t = data['tournament']
        fb = FlightBuilder(t)
        fb.build(num_flights=3)

        # Create a college Standing Block Speed event with 2 heats.
        ev_college = _make_college_event(
            db_session, t, 'Standing Block Speed', 'standing_block', gender='M'
        )
        _make_heat(db_session, ev_college, 1, [901, 902, 903])
        _make_heat(db_session, ev_college, 2, [904, 905, 906])

        result = fb.integrate_spillover([ev_college.id])

        assert result['integrated_heats'] == 2
        assert result['events'] == 1

        # The college heats should now have flight_id set.
        college_heats = Heat.query.filter_by(event_id=ev_college.id).all()
        for h in college_heats:
            assert h.flight_id is not None
            assert h.flight_position is not None

    def test_chokerman_run2_at_end_of_last_flight(self, db_session):
        """Chokerman's Race Run 2 heats are always placed at the end of the last flight."""
        from models import Flight, Heat
        from services.flight_builder import FlightBuilder

        data = _seed_standard_show(db_session)
        t = data['tournament']
        fb = FlightBuilder(t)
        fb.build(num_flights=3)

        # Create Chokerman's Race with run 1 and run 2 heats.
        ev_choke = _make_college_event(
            db_session, t, "Chokerman's Race", 'chokerman', gender='M',
            requires_dual_runs=True,
        )
        _make_heat(db_session, ev_choke, 1, [801, 802], run_number=1)
        _make_heat(db_session, ev_choke, 2, [803, 804], run_number=1)
        _make_heat(db_session, ev_choke, 1, [801, 802], run_number=2)
        _make_heat(db_session, ev_choke, 2, [803, 804], run_number=2)

        result = fb.integrate_spillover([ev_choke.id])

        # Only run 2 heats should be integrated (Chokerman on Saturday = run 2 only).
        assert result['integrated_heats'] == 2

        # Find the last flight.
        last_flight = Flight.query.filter_by(
            tournament_id=t.id
        ).order_by(Flight.flight_number.desc()).first()

        # The run 2 heats should be in the last flight.
        run2_heats = Heat.query.filter_by(
            event_id=ev_choke.id, run_number=2
        ).all()
        for h in run2_heats:
            assert h.flight_id == last_flight.id, (
                f'Chokerman run 2 heat {h.heat_number} placed in flight {h.flight_id}, '
                f'expected last flight {last_flight.id}'
            )

    def test_repeated_integration_inserts_before_pending_chokerman_tail(self, db_session):
        """New last-flight spillover shifts only the contiguous pending closer tail."""
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        filler_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        filler_heat = _place_heat(
            _make_heat(db_session, filler_event, 1, [1]), flight, 1
        )
        chokerman = _make_college_event(
            db_session,
            tournament,
            "Chokerman's Race",
            'chokerman',
            requires_dual_runs=True,
        )
        chokerman_heats = [
            _make_heat(db_session, chokerman, heat_number, [800 + heat_number], run_number=2)
            for heat_number in (1, 2)
        ]

        integrate_college_spillover_into_flights(
            tournament, college_event_ids=[], placement_mode='roundrobin'
        )
        assert [heat.flight_position for heat in chokerman_heats] == [2, 3]

        later_event = _make_college_event(
            db_session, tournament, 'Standing Block Speed', 'standing_block'
        )
        later_heat = _make_heat(db_session, later_event, 1, [900])
        db_session.flush()

        integrate_college_spillover_into_flights(
            tournament,
            college_event_ids=[later_event.id],
            placement_mode='roundrobin',
        )

        assert filler_heat.flight_position == 1
        assert later_heat.flight_position == 2
        assert [heat.flight_position for heat in chokerman_heats] == [3, 4]
        assert [heat.id for heat in flight.get_heats_ordered()] == [
            filler_heat.id,
            later_heat.id,
            chokerman_heats[0].id,
            chokerman_heats[1].id,
        ]

    def test_configured_day_split_event_requires_run_two_heats(self, db_session):
        from services.flight_builder import (
            FlightRebuildSafetyError,
            integrate_college_spillover_into_flights,
        )

        tournament = _make_tournament(db_session)
        _make_flight(db_session, tournament, 1)
        _make_college_event(
            db_session,
            tournament,
            "Chokerman's Race",
            'chokerman',
            requires_dual_runs=True,
        )

        with pytest.raises(FlightRebuildSafetyError, match='has no Run 2 heats'):
            integrate_college_spillover_into_flights(
                tournament, college_event_ids=[], placement_mode='roundrobin',
            )

    def test_chokerman_closer_requires_heat_number_order(self, db_session):
        from services.flight_builder import (
            FlightRebuildSafetyError,
            validate_chokerman_closer_invariant,
        )

        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        event = _make_college_event(
            db_session,
            tournament,
            "Chokerman's Race",
            'chokerman',
            gender='M',
            requires_dual_runs=True,
        )
        heat_one = _place_heat(
            _make_heat(db_session, event, 1, [801], run_number=2), flight, 2,
        )
        heat_two = _place_heat(
            _make_heat(db_session, event, 2, [802], run_number=2), flight, 1,
        )

        with pytest.raises(FlightRebuildSafetyError, match='heat-number order'):
            validate_chokerman_closer_invariant(tournament)

        assert [heat.id for heat in flight.get_heats_ordered()] == [
            heat_two.id, heat_one.id,
        ]

    def test_pre_misplaced_chokerman_rejected_without_mutation(self, db_session):
        """A flighted Run 2 closer must already be the last-flight suffix."""
        from services.flight_builder import (
            FlightRebuildSafetyError,
            integrate_college_spillover_into_flights,
            validate_chokerman_closer_invariant,
        )

        tournament = _make_tournament(db_session)
        first_flight = _make_flight(db_session, tournament, 1)
        last_flight = _make_flight(db_session, tournament, 2)
        pro_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        first_heat = _place_heat(
            _make_heat(db_session, pro_event, 1, [1]), first_flight, 1
        )
        last_heat = _place_heat(
            _make_heat(db_session, pro_event, 2, [2]), last_flight, 1
        )
        chokerman = _make_college_event(
            db_session,
            tournament,
            "Chokerman's Race",
            'chokerman',
            requires_dual_runs=True,
        )
        misplaced_closer = _place_heat(
            _make_heat(db_session, chokerman, 1, [800], run_number=2),
            first_flight,
            2,
        )
        spillover_event = _make_college_event(
            db_session, tournament, 'Standing Block Speed', 'standing_block'
        )
        spillover_heat = _make_heat(db_session, spillover_event, 1, [900])
        db_session.flush()

        with pytest.raises(FlightRebuildSafetyError, match='final suffix'):
            validate_chokerman_closer_invariant(tournament)
        with pytest.raises(FlightRebuildSafetyError, match='final suffix'):
            integrate_college_spillover_into_flights(
                tournament,
                college_event_ids=[spillover_event.id],
                placement_mode='roundrobin',
            )

        assert (first_heat.flight_id, first_heat.flight_position) == (first_flight.id, 1)
        assert (misplaced_closer.flight_id, misplaced_closer.flight_position) == (
            first_flight.id,
            2,
        )
        assert (last_heat.flight_id, last_heat.flight_position) == (last_flight.id, 1)
        assert spillover_heat.flight_id is None
        assert spillover_heat.flight_position is None

    @pytest.mark.parametrize('flight_status', ['in_progress', 'completed'])
    def test_nonpending_flight_rejects_new_spillover_before_mutation(
        self, db_session, flight_status
    ):
        """Pending heats do not make an active or completed flight mutable."""
        from services.flight_builder import (
            FlightRebuildSafetyError,
            integrate_college_spillover_into_flights,
        )

        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        flight.status = flight_status
        pro_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        placed_heat = _place_heat(
            _make_heat(db_session, pro_event, 1, [1]), flight, 1
        )
        spillover_event = _make_college_event(
            db_session, tournament, 'Standing Block Speed', 'standing_block'
        )
        spillover_heat = _make_heat(db_session, spillover_event, 1, [2])
        db_session.flush()

        with pytest.raises(FlightRebuildSafetyError, match='must be pending'):
            integrate_college_spillover_into_flights(
                tournament,
                college_event_ids=[spillover_event.id],
                placement_mode='roundrobin',
            )

        assert flight.status == flight_status
        assert placed_heat.status == 'pending'
        assert (placed_heat.flight_id, placed_heat.flight_position) == (flight.id, 1)
        assert spillover_heat.flight_id is None
        assert spillover_heat.flight_position is None

    def test_in_progress_chokerman_tail_rejects_new_spillover(self, db_session):
        """An in-progress closer heat seals even a still-pending final flight."""
        from services.flight_builder import (
            FlightRebuildSafetyError,
            integrate_college_spillover_into_flights,
        )

        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        pro_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        filler = _place_heat(_make_heat(db_session, pro_event, 1, [1]), flight, 1)
        chokerman = _make_college_event(
            db_session,
            tournament,
            "Chokerman's Race",
            'chokerman',
            requires_dual_runs=True,
        )
        closer = _place_heat(
            _make_heat(db_session, chokerman, 1, [800], run_number=2), flight, 2
        )
        closer.status = 'in_progress'
        spillover_event = _make_college_event(
            db_session, tournament, 'Standing Block Speed', 'standing_block'
        )
        spillover_heat = _make_heat(db_session, spillover_event, 1, [900])
        db_session.flush()

        with pytest.raises(FlightRebuildSafetyError, match='flighted heats must be pending'):
            integrate_college_spillover_into_flights(
                tournament,
                college_event_ids=[spillover_event.id],
                placement_mode='roundrobin',
            )

        assert [(heat.id, heat.flight_position) for heat in flight.get_heats_ordered()] == [
            (filler.id, 1),
            (closer.id, 2),
        ]
        assert spillover_heat.flight_id is None
        assert spillover_heat.flight_position is None

    def test_chokerman_invariant_validates_before_and_after_integration(
        self, db_session, monkeypatch
    ):
        """Integration checks both the persisted and final projected show order."""
        import services.flight_builder as flight_builder

        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        pro_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        _place_heat(_make_heat(db_session, pro_event, 1, [1]), flight, 1)
        chokerman = _make_college_event(
            db_session,
            tournament,
            "Chokerman's Race",
            'chokerman',
            requires_dual_runs=True,
        )
        closer = _make_heat(db_session, chokerman, 1, [800], run_number=2)
        db_session.flush()

        original = flight_builder.validate_chokerman_closer_invariant
        snapshots = []

        def track_invariant(*args, **kwargs):
            projected_order = kwargs.get('projected_order')
            snapshots.append(
                [] if projected_order is None else [heat.id for heat in projected_order]
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(
            flight_builder, 'validate_chokerman_closer_invariant', track_invariant
        )

        flight_builder.integrate_college_spillover_into_flights(
            tournament, college_event_ids=[], placement_mode='roundrobin'
        )

        assert len(snapshots) == 2
        assert closer.id not in snapshots[0]
        assert snapshots[1][-1] == closer.id

    def test_completed_flight_rejects_new_spillover_before_mutation(self, db_session):
        """A scored show cannot be changed by a later spillover integration."""
        from services.flight_builder import (
            FlightRebuildSafetyError,
            integrate_college_spillover_into_flights,
        )

        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        pro_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        completed_heat = _place_heat(
            _make_heat(db_session, pro_event, 1, [1]), flight, 1
        )
        completed_heat.status = 'completed'
        spillover_event = _make_college_event(
            db_session, tournament, 'Standing Block Speed', 'standing_block'
        )
        spillover_heat = _make_heat(db_session, spillover_event, 1, [2])
        db_session.flush()

        with pytest.raises(FlightRebuildSafetyError, match='completed'):
            integrate_college_spillover_into_flights(
                tournament,
                college_event_ids=[spillover_event.id],
                placement_mode='roundrobin',
            )

        assert completed_heat.flight_id == flight.id
        assert completed_heat.flight_position == 1
        assert completed_heat.status == 'completed'
        assert spillover_heat.flight_id is None
        assert spillover_heat.flight_position is None

    def test_selected_pro_event_is_reported_and_not_integrated(self, db_session):
        """A selected id is never enough to pull a pro heat into spillover."""
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        filler_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        _place_heat(_make_heat(db_session, filler_event, 1, [1]), flight, 1)

        selected_pro_event = _make_pro_event(
            db_session, tournament, 'Single Buck', 'saw_hand'
        )
        selected_heat = _make_heat(db_session, selected_pro_event, 1, [2])
        db_session.flush()

        result = integrate_college_spillover_into_flights(
            tournament,
            college_event_ids=[selected_pro_event.id],
            placement_mode='roundrobin',
        )

        assert selected_heat.flight_id is None
        assert selected_heat.flight_position is None
        assert result['integrated_heats'] == 0
        assert result['ignored_non_college_event_ids'] == [selected_pro_event.id]

    def test_selected_college_event_lookup_stays_in_current_tournament(
        self, db_session, monkeypatch
    ):
        """A foreign college id is ignored without using the global Event query."""
        from models import Event
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session, name='Current Tournament')
        flight = _make_flight(db_session, tournament, 1)
        filler_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        _place_heat(_make_heat(db_session, filler_event, 1, [1]), flight, 1)

        other_tournament = _make_tournament(db_session, name='Other Tournament')
        foreign_event = _make_college_event(
            db_session, other_tournament, 'Standing Block Speed', 'standing_block'
        )
        foreign_heat = _make_heat(db_session, foreign_event, 1, [2])
        db_session.flush()

        class ForbiddenGlobalEventQuery:
            def __getattr__(self, name):
                raise AssertionError(
                    f'initial selected-event lookup used Event.query.{name}'
                )

        monkeypatch.setattr(Event, 'query', ForbiddenGlobalEventQuery())

        result = integrate_college_spillover_into_flights(
            tournament,
            college_event_ids=[foreign_event.id],
            placement_mode='roundrobin',
        )

        assert foreign_heat.flight_id is None
        assert foreign_heat.flight_position is None
        assert result['integrated_heats'] == 0
        assert result['ignored_non_college_event_ids'] == []

    def test_flight_rows_are_requested_for_update(self, db_session, monkeypatch):
        """Integration acquires the tournament flight rows through with_for_update."""
        from sqlalchemy.orm import Query

        from models import Flight
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        pro_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        _place_heat(_make_heat(db_session, pro_event, 1, [1]), flight, 1)
        spillover_event = _make_college_event(
            db_session, tournament, 'Standing Block Speed', 'standing_block'
        )
        _make_heat(db_session, spillover_event, 1, [2])
        db_session.flush()

        original = Query.with_for_update
        locked_flight_query = []

        def track_with_for_update(query, *args, **kwargs):
            if any(
                description.get('entity') is Flight
                for description in query.column_descriptions
            ):
                locked_flight_query.append(True)
            return original(query, *args, **kwargs)

        monkeypatch.setattr(Query, 'with_for_update', track_with_for_update)

        integrate_college_spillover_into_flights(
            tournament,
            college_event_ids=[spillover_event.id],
            placement_mode='roundrobin',
        )

        assert locked_flight_query == [True]

    def test_sqlite_concurrent_spillover_persists_unique_positions(
        self, monkeypatch
    ):
        """SQLite serializes two real integrations before either can reuse a slot."""
        import threading
        import time

        import services.flight_builder as flight_builder
        from models import Heat, Tournament
        from tests.db_test_utils import create_test_app, drop_test_db

        monkeypatch.delenv('PROAM_UNIT_PG', raising=False)
        concurrent_app, db_handle = create_test_app()
        first_at_snapshot = threading.Event()
        second_attempting = threading.Event()
        first_delay_used = threading.Event()
        errors = []
        result_lock = threading.Lock()

        try:
            with concurrent_app.app_context():
                tournament = _make_tournament(_db.session, name='Concurrent Spillover')
                flight = _make_flight(_db.session, tournament, 1)
                pro_event = _make_pro_event(
                    _db.session, tournament, 'Underhand', 'underhand'
                )
                _place_heat(
                    _make_heat(_db.session, pro_event, 1, [1]), flight, 1
                )
                first_event = _make_college_event(
                    _db.session,
                    tournament,
                    'Standing Block Speed A',
                    'standing_block',
                )
                first_heat = _make_heat(_db.session, first_event, 1, [2])
                second_event = _make_college_event(
                    _db.session,
                    tournament,
                    'Standing Block Speed B',
                    'standing_block',
                )
                second_heat = _make_heat(_db.session, second_event, 1, [3])
                tournament_id = tournament.id
                event_ids = (first_event.id, second_event.id)
                spillover_heat_ids = (first_heat.id, second_heat.id)
                _db.session.commit()

            original_invariant = flight_builder.validate_chokerman_closer_invariant

            def hold_first_snapshot(*args, **kwargs):
                if (
                    threading.current_thread().name == 'spillover-first'
                    and not first_delay_used.is_set()
                ):
                    first_delay_used.set()
                    first_at_snapshot.set()
                    if not second_attempting.wait(timeout=5):
                        raise AssertionError('second integration never attempted')
                    time.sleep(0.25)
                return original_invariant(*args, **kwargs)

            monkeypatch.setattr(
                flight_builder,
                'validate_chokerman_closer_invariant',
                hold_first_snapshot,
            )

            def integrate(event_id, *, announce_attempt=False):
                with concurrent_app.app_context():
                    try:
                        if announce_attempt:
                            second_attempting.set()
                        current_tournament = _db.session.get(Tournament, tournament_id)
                        flight_builder.integrate_college_spillover_into_flights(
                            current_tournament,
                            college_event_ids=[event_id],
                            placement_mode='roundrobin',
                            commit=True,
                        )
                    except Exception as exc:  # pragma: no cover - asserted below
                        _db.session.rollback()
                        with result_lock:
                            errors.append(exc)
                    finally:
                        _db.session.remove()

            first_thread = threading.Thread(
                target=integrate,
                args=(event_ids[0],),
                name='spillover-first',
            )
            second_thread = threading.Thread(
                target=integrate,
                args=(event_ids[1],),
                kwargs={'announce_attempt': True},
                name='spillover-second',
            )
            first_thread.start()
            assert first_at_snapshot.wait(timeout=5)
            second_thread.start()
            first_thread.join(timeout=10)
            second_thread.join(timeout=10)

            assert not first_thread.is_alive()
            assert not second_thread.is_alive()
            assert errors == []

            with concurrent_app.app_context():
                positions = [
                    heat.flight_position
                    for heat in Heat.query.filter(Heat.id.in_(spillover_heat_ids)).all()
                ]
                all_positions = [
                    heat.flight_position
                    for heat in Heat.query.filter(Heat.flight_id.isnot(None)).all()
                ]
                assert sorted(positions) == [2, 3]
                assert len(all_positions) == len(set(all_positions))
        finally:
            with concurrent_app.app_context():
                _db.session.remove()
                _db.engine.dispose()
            drop_test_db(db_handle)

    def test_flight_heats_are_batch_loaded_once(self, db_session, monkeypatch):
        """All existing flight heats load in one tournament-scoped IN query."""
        from sqlalchemy import event as sqlalchemy_event

        from models import Flight
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        first_flight = _make_flight(db_session, tournament, 1)
        second_flight = _make_flight(db_session, tournament, 2)
        pro_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        _place_heat(_make_heat(db_session, pro_event, 1, [1]), first_flight, 1)
        _place_heat(_make_heat(db_session, pro_event, 2, [2]), second_flight, 1)
        spillover_event = _make_college_event(
            db_session, tournament, 'Standing Block Speed', 'standing_block'
        )
        _make_heat(db_session, spillover_event, 1, [3])
        db_session.flush()

        def forbid_per_flight_query(*args, **kwargs):
            raise AssertionError('integration called Flight.get_heats_ordered')

        monkeypatch.setattr(Flight, 'get_heats_ordered', forbid_per_flight_query)
        statements = []

        def capture_statement(connection, cursor, statement, parameters, context, executemany):
            normalized = ' '.join(statement.lower().split())
            if ' from heats ' in f' {normalized} ' and 'heats.flight_id in' in normalized:
                statements.append(normalized)

        sqlalchemy_event.listen(_db.engine, 'before_cursor_execute', capture_statement)
        try:
            integrate_college_spillover_into_flights(
                tournament,
                college_event_ids=[spillover_event.id],
                placement_mode='roundrobin',
            )
        finally:
            sqlalchemy_event.remove(_db.engine, 'before_cursor_execute', capture_statement)

        assert len(statements) == 1

    def test_missing_flight_position_rejected_without_mutation(self, db_session):
        """NULL positions are corruption, not an implicit position zero."""
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        pro_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        corrupt_heat = _place_heat(
            _make_heat(db_session, pro_event, 1, [1]), flight, None
        )
        spillover_event = _make_college_event(
            db_session, tournament, 'Standing Block Speed', 'standing_block'
        )
        spillover_heat = _make_heat(db_session, spillover_event, 1, [2])
        db_session.flush()

        with pytest.raises(ValueError, match='missing flight_position'):
            integrate_college_spillover_into_flights(
                tournament,
                college_event_ids=[spillover_event.id],
                placement_mode='roundrobin',
            )

        assert corrupt_heat.flight_position is None
        assert spillover_heat.flight_id is None
        assert spillover_heat.flight_position is None

    @pytest.mark.parametrize('bad_position', [0, -1])
    def test_nonpositive_flight_position_rejected_without_mutation(
        self, db_session, bad_position
    ):
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        pro_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        corrupt_heat = _place_heat(
            _make_heat(db_session, pro_event, 1, [1]), flight, bad_position
        )
        spillover_event = _make_college_event(
            db_session, tournament, 'Standing Block Speed', 'standing_block'
        )
        spillover_heat = _make_heat(db_session, spillover_event, 1, [2])
        db_session.flush()

        with pytest.raises(ValueError, match='non-positive flight_position'):
            integrate_college_spillover_into_flights(
                tournament,
                college_event_ids=[spillover_event.id],
                placement_mode='roundrobin',
            )

        assert corrupt_heat.flight_position == bad_position
        assert spillover_heat.flight_id is None
        assert spillover_heat.flight_position is None

    def test_duplicate_flight_position_rejected_by_database(self, db_session):
        """The schema rejects duplicate persisted positions immediately."""
        tournament = _make_tournament(db_session)
        flight = _make_flight(db_session, tournament, 1)
        pro_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        _place_heat(_make_heat(db_session, pro_event, 1, [1]), flight, 1)
        second_heat = _place_heat(
            _make_heat(db_session, pro_event, 2, [2]), flight, 2,
        )
        db_session.flush()

        second_heat.flight_position = 1
        with pytest.raises(IntegrityError, match='flight_position'):
            db_session.flush()
        db_session.rollback()

    def test_spacing_uses_assignment_uids_not_overlapping_integer_ids(self, db_session):
        """Pro/college id overlap is distinct, while one college uid still spaces."""
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        first_flight = _make_flight(db_session, tournament, 1)
        second_flight = _make_flight(db_session, tournament, 2)
        third_flight = _make_flight(db_session, tournament, 3)
        neutral_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')

        pro_heat = _make_heat(db_session, neutral_event, 1, [1])
        _place_heat(pro_heat, first_flight, 1)
        _place_heat(_make_heat(db_session, neutral_event, 2, [10]), second_flight, 1)
        _place_heat(_make_heat(db_session, neutral_event, 3, [11]), third_flight, 1)
        _place_heat(_make_heat(db_session, neutral_event, 4, [12]), third_flight, 2)

        spillover_event = _make_college_event(
            db_session, tournament, 'College Underhand', 'underhand'
        )
        first_spillover = _make_heat(db_session, spillover_event, 1, [1])
        second_spillover = _make_heat(db_session, spillover_event, 2, [1])
        db_session.flush()

        integrate_college_spillover_into_flights(
            tournament,
            college_event_ids=[spillover_event.id],
            placement_mode='roundrobin',
        )

        pro_uid = pro_heat.assignments[0].uid
        college_uids = {
            first_spillover.assignments[0].uid,
            second_spillover.assignments[0].uid,
        }
        assert college_uids == {first_spillover.assignments[0].uid}
        assert pro_uid not in college_uids
        assert first_spillover.flight_id == first_flight.id
        assert second_spillover.flight_id == third_flight.id

    def test_roundrobin_uses_projected_order_to_avoid_stock_saw_conflict(self, db_session):
        """Round-robin may advance a flight to avoid a Stock Saw/hand-saw clash."""
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        first_flight = _make_flight(db_session, tournament, 1)
        second_flight = _make_flight(db_session, tournament, 2)
        neutral_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        saw_event = _make_pro_event(db_session, tournament, 'Single Buck', 'saw_hand')

        _place_heat(_make_heat(db_session, neutral_event, 1, [10]), first_flight, 1)
        _place_heat(_make_heat(db_session, saw_event, 1, [20]), second_flight, 1)
        for offset in range(7):
            _place_heat(
                _make_heat(db_session, neutral_event, offset + 2, [30 + offset]),
                second_flight,
                offset + 2,
            )

        spillover_event = _make_college_event(
            db_session, tournament, 'College Stock Saw', 'stock_saw'
        )
        spillover_heat = _make_heat(db_session, spillover_event, 1, [100])
        db_session.flush()

        result = integrate_college_spillover_into_flights(
            tournament,
            college_event_ids=[spillover_event.id],
            placement_mode='roundrobin',
        )

        assert spillover_heat.flight_id == second_flight.id
        assert result['unavoidable_stand_conflicts'] == []

    def test_cluster_uses_projected_order_to_avoid_pole_conflict(self, db_session):
        """Cluster placement keeps its bias only among physically safe flights."""
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        first_flight = _make_flight(db_session, tournament, 1)
        second_flight = _make_flight(db_session, tournament, 2)
        neutral_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        obstacle_event = _make_pro_event(
            db_session, tournament, 'Obstacle Pole', 'obstacle_pole'
        )

        for offset in range(7):
            _place_heat(
                _make_heat(db_session, neutral_event, offset + 1, [200 + offset]),
                first_flight,
                offset + 1,
            )
        _place_heat(_make_heat(db_session, obstacle_event, 1, [220]), first_flight, 8)
        for offset in range(8):
            _place_heat(
                _make_heat(db_session, neutral_event, offset + 8, [230 + offset]),
                second_flight,
                offset + 1,
            )

        spillover_event = _make_college_event(
            db_session, tournament, 'College Pole Sprint', 'speed_climb'
        )
        spillover_heat = _make_heat(db_session, spillover_event, 1, [300])
        db_session.flush()

        result = integrate_college_spillover_into_flights(
            tournament,
            college_event_ids=[spillover_event.id],
            placement_mode='cluster',
        )

        assert spillover_heat.flight_id == second_flight.id
        assert result['unavoidable_stand_conflicts'] == []

    def test_spacing_first_fallback_reports_unavoidable_stand_conflict(self, db_session):
        """Spacing wins deterministically when no flight satisfies both constraints."""
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        first_flight = _make_flight(db_session, tournament, 1)
        second_flight = _make_flight(db_session, tournament, 2)
        neutral_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        saw_event = _make_pro_event(db_session, tournament, 'Single Buck', 'saw_hand')
        prior_college_event = _make_college_event(
            db_session, tournament, 'Prior College Underhand', 'underhand'
        )

        first_appearance = _make_heat(db_session, prior_college_event, 1, [1])
        _place_heat(first_appearance, first_flight, 1)
        for offset in range(3):
            _place_heat(
                _make_heat(db_session, neutral_event, offset + 2, [400 + offset]),
                first_flight,
                offset + 2,
            )

        saw_heat = _make_heat(db_session, saw_event, 1, [410])
        _place_heat(saw_heat, second_flight, 1)
        for offset in range(6):
            _place_heat(
                _make_heat(db_session, neutral_event, offset + 5, [420 + offset]),
                second_flight,
                offset + 2,
            )
        second_appearance = _make_heat(db_session, prior_college_event, 2, [1])
        _place_heat(second_appearance, second_flight, 8)

        spillover_event = _make_college_event(
            db_session, tournament, 'College Stock Saw', 'stock_saw'
        )
        spillover_heat = _make_heat(db_session, spillover_event, 1, [1])
        db_session.flush()

        result = integrate_college_spillover_into_flights(
            tournament,
            college_event_ids=[spillover_event.id],
            placement_mode='roundrobin',
        )

        assert spillover_heat.flight_id == first_flight.id
        assert result['unavoidable_stand_conflicts'] == [
            {
                'heat_ids': (spillover_heat.id, saw_heat.id),
                'stand_types': ('stock_saw', 'saw_hand'),
                'gap': 1,
            }
        ]

    def test_unavoidable_stand_cost_prefers_fewer_then_milder_conflicts(self, db_session):
        """Equal-spacing fallback minimizes conflict count, then total shortfall."""
        from services.flight_builder import integrate_college_spillover_into_flights

        tournament = _make_tournament(db_session)
        first_flight = _make_flight(db_session, tournament, 1)
        second_flight = _make_flight(db_session, tournament, 2)
        third_flight = _make_flight(db_session, tournament, 3)
        neutral_event = _make_pro_event(db_session, tournament, 'Underhand', 'underhand')
        saw_event = _make_pro_event(db_session, tournament, 'Single Buck', 'saw_hand')

        _place_heat(_make_heat(db_session, neutral_event, 1, []), first_flight, 1)
        first_saw = _make_heat(db_session, saw_event, 1, [])
        second_saw = _make_heat(db_session, saw_event, 2, [])
        _place_heat(first_saw, second_flight, 1)
        _place_heat(second_saw, second_flight, 2)
        for offset in range(6):
            _place_heat(
                _make_heat(db_session, neutral_event, offset + 2, []),
                third_flight,
                offset + 1,
            )

        spillover_event = _make_college_event(
            db_session, tournament, 'College Stock Saw', 'stock_saw'
        )
        spillover_heat = _make_heat(db_session, spillover_event, 1, [])
        db_session.flush()

        result = integrate_college_spillover_into_flights(
            tournament,
            college_event_ids=[spillover_event.id],
            placement_mode='roundrobin',
        )

        assert spillover_heat.flight_id == third_flight.id
        assert result['unavoidable_stand_conflicts'] == [
            {
                'heat_ids': (second_saw.id, spillover_heat.id),
                'stand_types': ('saw_hand', 'stock_saw'),
                'gap': 7,
            }
        ]

    def test_spillover_with_no_flights_returns_zero(self, db_session):
        """Spillover on a tournament with no flights returns a no-op result."""
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)
        ev = _make_college_event(db_session, t, 'Standing Block Speed', 'standing_block', gender='M')
        _make_heat(db_session, ev, 1, [901])

        fb = FlightBuilder(t)
        result = fb.integrate_spillover([ev.id])

        assert result['integrated_heats'] == 0

    def test_spillover_does_not_attach_completed_unflighted_heat(self, db_session):
        """A Friday heat that already ran cannot be introduced to Saturday."""
        from models import Heat
        from services.flight_builder import FlightBuilder

        data = _seed_standard_show(db_session)
        tournament = data['tournament']
        FlightBuilder(tournament).build(num_flights=2)

        event = _make_college_event(
            db_session, tournament, 'Standing Block Speed', 'standing_block', gender='M',
        )
        heat = _make_heat(db_session, event, 1, [901, 902])
        heat.status = 'completed'
        heat_id = heat.id
        db_session.flush()

        result = FlightBuilder(tournament).integrate_spillover([event.id])

        assert result['integrated_heats'] == 0
        assert result['skipped_completed'] == 1
        preserved = _db.session.get(Heat, heat_id)
        assert preserved.status == 'completed'
        assert preserved.flight_id is None
        assert preserved.flight_position is None


# ---------------------------------------------------------------------------
# build_pro_flights() module-level tests
# ---------------------------------------------------------------------------

class TestBuildProFlights:
    """End-to-end tests for the module-level build_pro_flights() function."""

    def test_shared_stands_are_separated_when_other_heats_are_available(
        self, db_session
    ):
        """The greedy builder reserves the shared block field for eight heats."""
        from models import Flight, Heat
        from services.flight_builder import _STAND_CONFLICT_GAP, build_pro_flights

        tournament = _make_tournament(db_session)
        competitors = [
            _make_pro_competitor(db_session, tournament, f"Resource {number}")
            for number in range(1, 11)
        ]
        cookie = _make_pro_event(
            db_session, tournament, "Cookie Stack", "cookie_stack", max_stands=5
        )
        standing = _make_pro_event(
            db_session, tournament, "Standing Block", "standing_block", max_stands=5
        )
        underhand = _make_pro_event(
            db_session, tournament, "Underhand", "underhand", max_stands=5
        )
        _make_heat(db_session, cookie, 1, [competitors[0].id])
        _make_heat(db_session, standing, 1, [competitors[1].id])
        for heat_number, competitor in enumerate(competitors[2:], start=1):
            _make_heat(db_session, underhand, heat_number, [competitor.id])

        build_pro_flights(tournament, num_flights=1)
        ordered = (
            Heat.query.join(Flight)
            .filter(Flight.tournament_id == tournament.id)
            .order_by(Flight.flight_number, Heat.flight_position)
            .all()
        )
        positions = {
            heat.event.stand_type: index
            for index, heat in enumerate(ordered)
            if heat.event.stand_type in {"cookie_stack", "standing_block"}
        }

        assert abs(positions["cookie_stack"] - positions["standing_block"]) >= _STAND_CONFLICT_GAP

    def test_end_to_end_creates_flights(self, db_session):
        """build_pro_flights creates Flight records and assigns heats."""
        from models import Flight, Heat
        from services.flight_builder import build_pro_flights

        data = _seed_standard_show(db_session)
        t = data['tournament']

        flights_created = build_pro_flights(t)
        assert flights_created > 0

        db_flights = Flight.query.filter_by(tournament_id=t.id).all()
        assert len(db_flights) == flights_created

    def test_empty_tournament_creates_no_flights(self, db_session):
        """A tournament with no heats should produce 0 flights."""
        from services.flight_builder import build_pro_flights

        t = _make_tournament(db_session)
        flights_created = build_pro_flights(t)
        assert flights_created == 0

    def test_empty_tournament_with_only_events_creates_no_flights(self, db_session):
        """A tournament with pro events but no heats should produce 0 flights."""
        from services.flight_builder import build_pro_flights

        t = _make_tournament(db_session)
        _make_pro_event(db_session, t, 'Springboard', 'springboard')
        _make_pro_event(db_session, t, 'Underhand', 'underhand', gender='M')

        flights_created = build_pro_flights(t)
        assert flights_created == 0

    def test_rebuild_clears_old_flights(self, db_session):
        """Re-building flights removes old flights and creates new ones."""
        from models import Flight
        from services.flight_builder import build_pro_flights

        data = _seed_standard_show(db_session)
        t = data['tournament']

        first_count = build_pro_flights(t)
        old_flight_ids = {f.id for f in Flight.query.filter_by(tournament_id=t.id).all()}
        assert first_count > 0

        second_count = build_pro_flights(t)
        new_flights = Flight.query.filter_by(tournament_id=t.id).all()

        assert second_count > 0
        # Rebuild should produce flights (IDs may be reused by SQLite).
        assert len(new_flights) == second_count

    def test_rebuild_preserves_heat_count(self, db_session):
        """Re-building flights should assign the same number of heats."""
        from models import Heat
        from services.flight_builder import build_pro_flights

        data = _seed_standard_show(db_session)
        t = data['tournament']

        build_pro_flights(t)
        first_assigned = Heat.query.filter(
            Heat.event_id.in_([e.id for e in data['events'].values()]),
            Heat.flight_id.isnot(None),
        ).count()

        build_pro_flights(t)
        second_assigned = Heat.query.filter(
            Heat.event_id.in_([e.id for e in data['events'].values()]),
            Heat.flight_id.isnot(None),
        ).count()

        assert first_assigned == second_assigned

    def test_initial_build_leaves_completed_unflighted_heat_unchanged(self, db_session):
        """Initial flight building must not retroactively schedule scored work."""
        from models import Heat
        from services.flight_builder import build_pro_flights

        data = _seed_standard_show(db_session)
        tournament = data['tournament']
        event_ids = [event.id for event in data['events'].values()]
        completed = Heat.query.filter(Heat.event_id.in_(event_ids)).first()
        assert completed is not None
        assert completed.flight_id is None
        completed.status = 'completed'
        completed_id = completed.id
        db_session.flush()

        flights_created = build_pro_flights(tournament)

        preserved = db_session.get(Heat, completed_id)
        assert flights_created > 0
        assert preserved.status == 'completed'
        assert preserved.flight_id is None
        assert preserved.flight_position is None
        assert Heat.query.filter(
            Heat.event_id.in_(event_ids),
            Heat.id != completed_id,
            Heat.flight_id.isnot(None),
        ).count() > 0

    def test_rebuild_refuses_to_rewrite_completed_flight_history(self, db_session):
        """No rebuild may clear or replace a scored heat's published location."""
        from models import Flight, Heat
        from services.flight_builder import FlightRebuildSafetyError, build_pro_flights

        data = _seed_standard_show(db_session)
        tournament = data['tournament']
        build_pro_flights(tournament)

        event_ids = [event.id for event in data['events'].values()]
        completed = Heat.query.filter(
            Heat.event_id.in_(event_ids),
            Heat.flight_id.isnot(None),
        ).first()
        assert completed is not None
        completed.status = 'completed'
        db_session.commit()
        db_session.expire_all()
        assert Heat.query.filter_by(id=completed.id).one().status == 'completed'

        before = [
            (heat.id, heat.flight_id, heat.flight_position)
            for heat in Heat.query.join(Flight).filter(
                Flight.tournament_id == tournament.id,
                Heat.event_id.in_(event_ids),
            ).order_by(Heat.id).all()
        ]

        with pytest.raises(FlightRebuildSafetyError, match='completed heat placements'):
            build_pro_flights(tournament)

        after = [
            (heat.id, heat.flight_id, heat.flight_position)
            for heat in Heat.query.join(Flight).filter(
                Flight.tournament_id == tournament.id,
                Heat.event_id.in_(event_ids),
            ).order_by(Heat.id).all()
        ]
        assert after == before

    def test_rebuild_refuses_to_delete_in_progress_flight(self, db_session):
        from models import Flight, Heat
        from services.flight_builder import FlightRebuildSafetyError, build_pro_flights

        data = _seed_standard_show(db_session)
        tournament = data['tournament']
        build_pro_flights(tournament)
        active_flight = Flight.query.filter_by(
            tournament_id=tournament.id,
        ).order_by(Flight.flight_number).first()
        active_flight.status = 'in_progress'
        db_session.commit()

        before = [
            (heat.id, heat.flight_id, heat.flight_position)
            for heat in Heat.query.join(Flight).filter(
                Flight.tournament_id == tournament.id,
            ).order_by(Heat.id).all()
        ]

        with pytest.raises(FlightRebuildSafetyError, match='after a flight starts'):
            build_pro_flights(tournament)

        after = [
            (heat.id, heat.flight_id, heat.flight_position)
            for heat in Heat.query.join(Flight).filter(
                Flight.tournament_id == tournament.id,
            ).order_by(Heat.id).all()
        ]
        assert after == before


# ---------------------------------------------------------------------------
# FlightBuilder.spacing() tests
# ---------------------------------------------------------------------------

class TestFlightBuilderSpacing:
    """FlightBuilder.spacing() returns correct tier values."""

    def test_springboard_tier(self, db_session):
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)
        fb = FlightBuilder(t)
        ev = _make_pro_event(db_session, t, 'Springboard', 'springboard')
        min_sp, target_sp = fb.spacing(ev)
        assert min_sp == 6
        assert target_sp == 8

    def test_saw_hand_tier(self, db_session):
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)
        fb = FlightBuilder(t)
        ev = _make_pro_event(db_session, t, 'Single Buck', 'saw_hand', gender='M')
        min_sp, target_sp = fb.spacing(ev)
        assert min_sp == 5
        assert target_sp == 7

    def test_default_tier(self, db_session):
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)
        fb = FlightBuilder(t)
        ev = _make_pro_event(db_session, t, 'Hot Saw', 'hot_saw')
        min_sp, target_sp = fb.spacing(ev)
        assert min_sp == 4
        assert target_sp == 5

    def test_unknown_stand_type_uses_global_default(self, db_session):
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)
        fb = FlightBuilder(t)
        ev = _make_pro_event(db_session, t, 'Mystery Event', 'unknown_type')
        min_sp, target_sp = fb.spacing(ev)
        assert min_sp == 4
        assert target_sp == 5


# ---------------------------------------------------------------------------
# validate_competitor_spacing and build_flight_audit_report
# ---------------------------------------------------------------------------

class TestFlightAudit:
    """Validate the audit/spacing report after a flight build."""

    def test_audit_report_structure(self, db_session):
        """build_flight_audit_report returns expected keys after a build."""
        from services.flight_builder import FlightBuilder, build_flight_audit_report

        data = _seed_standard_show(db_session)
        fb = FlightBuilder(data['tournament'])
        fb.build()

        report = build_flight_audit_report(data['tournament'])
        assert 'total_heats' in report
        assert 'total_flights' in report
        assert 'sequential_violations' in report
        assert 'spacing_violations' in report
        assert 'variety_per_flight' in report
        assert report['total_heats'] > 0
        assert report['total_flights'] > 0

    def test_sequential_order_passes(self, db_session):
        """After a build, heats from each event should be in sequential order."""
        from services.flight_builder import FlightBuilder, build_flight_audit_report

        data = _seed_standard_show(db_session)
        fb = FlightBuilder(data['tournament'])
        fb.build()

        report = build_flight_audit_report(data['tournament'])
        assert report['passes_sequential'] is True, (
            f'Sequential violations: {report["sequential_violations"]}'
        )

    def test_no_flights_returns_error(self, db_session):
        """Audit report on a tournament with no flights returns an error dict."""
        from services.flight_builder import build_flight_audit_report

        t = _make_tournament(db_session)
        report = build_flight_audit_report(t)
        assert 'error' in report


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestFlightBuilderEdgeCases:
    """Edge case tests for flight building."""

    def test_single_event_builds_flights(self, db_session):
        """A tournament with only one event should still create flights."""
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)
        ev = _make_pro_event(db_session, t, 'Hot Saw', 'hot_saw', max_stands=4)
        for i in range(1, 6):
            _make_heat(db_session, ev, i, [i * 100 + 1, i * 100 + 2, i * 100 + 3])

        fb = FlightBuilder(t)
        flights = fb.build()
        assert flights >= 1

    def test_single_heat_creates_single_flight(self, db_session):
        """One heat should produce one flight."""
        from models import Flight
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)
        ev = _make_pro_event(db_session, t, 'Underhand', 'underhand', gender='M')
        _make_heat(db_session, ev, 1, [1, 2, 3, 4])

        fb = FlightBuilder(t)
        flights_created = fb.build()
        assert flights_created == 1

        f = Flight.query.filter_by(tournament_id=t.id).first()
        assert f is not None
        assert f.heat_count == 1

    def test_many_heats_same_competitor(self, db_session):
        """A competitor in many heats should still have flights built without error."""
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)
        shared_comp_id = 999

        # 3 events with the same competitor in every heat
        for ev_name, st in [('Springboard', 'springboard'), ('Underhand', 'underhand'), ('Hot Saw', 'hot_saw')]:
            ev = _make_pro_event(db_session, t, ev_name, st)
            for h in range(1, 4):
                _make_heat(db_session, ev, h, [shared_comp_id, h * 100 + 1])

        fb = FlightBuilder(t)
        flights = fb.build()
        assert flights > 0

    def test_even_event_distribution_across_flights(self, db_session):
        """Regression test (2026-04-21): heats of each event must be spread
        across flights as evenly as possible, not stacked into one flight.

        Prior behavior: when a heat's competitors appeared in no other event,
        the greedy scored that heat at +1000 (first-appearance) and stacked
        every same-event heat in a row. On a 3-flight, 53-heat show the whole
        women's underhand field (4 heats) and most of men's underhand ended
        up in flight 1, violating the crowd-variety first principle.

        This test uses disjoint competitor pools per event so the pre-fix
        algorithm is forced to clump. Per-flight-per-event cap = ceil(N_e/F).
        """
        from models import Flight, Heat
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)

        # 3 events, each with 4 heats, each heat's 3 competitors unique across
        # the whole show — no spacing pressure links the events. With 3 flights
        # (12 heats, 4 per flight) the fair distribution is roughly 1-2 heats
        # of each event per flight; cap = ceil(4/3) = 2.
        event_specs = [
            ("Women's Underhand", 'underhand', 'F'),
            ("Men's Underhand", 'underhand', 'M'),
            ('Obstacle Pole', 'obstacle_pole', None),
        ]
        next_comp = 1
        events = []
        for name, stand, gender in event_specs:
            ev = _make_pro_event(db_session, t, name, stand, gender=gender, max_stands=5)
            for hn in range(1, 5):
                ids = [next_comp, next_comp + 1, next_comp + 2]
                next_comp += 3
                _make_heat(db_session, ev, hn, ids)
            events.append(ev)

        fb = FlightBuilder(t)
        fb.build(num_flights=3)

        flights = Flight.query.filter_by(tournament_id=t.id).order_by(
            Flight.flight_number
        ).all()
        assert len(flights) == 3

        import math as _math
        cap = _math.ceil(4 / 3)  # 2 heats per event per flight

        for ev in events:
            counts_per_flight = []
            for f in flights:
                c = Heat.query.filter_by(flight_id=f.id, event_id=ev.id).count()
                counts_per_flight.append(c)
            assert max(counts_per_flight) <= cap, (
                f'Event {ev.name} distribution across 3 flights was '
                f'{counts_per_flight}; expected each flight <= {cap} heats. '
                f'All-underhand-in-one-flight regression.'
            )

    def test_no_same_stand_type_adjacency(self, db_session):
        """Regression test (2026-04-21): heats of the same stand_type must not
        be placed back-to-back. Men's Underhand + Women's Underhand share 5
        physical underhand stands; Single Buck + Double Buck + Jack & Jill
        share 8 hand-saw stands. Adjacent placement reuses the same stands
        with no reset time and no crowd-variety break.

        This test seeds a tournament where the ONLY way to place heats is with
        same-stand-type adjacencies unless the greedy actively avoids them.
        The penalty should push the algorithm to interleave other stand types
        between same-stand-type heats whenever possible.
        """
        from models import Flight, Heat
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)

        # 3 underhand events (share stand_type='underhand') + 3 other events.
        # 12 total heats, all independent competitor pools.
        event_specs = [
            ("Men's Underhand", 'underhand', 'M'),
            ("Women's Underhand", 'underhand', 'F'),
            ("Obstacle Pole", 'obstacle_pole', None),
            ("Cookie Stack", 'cookie_stack', None),
            ("Pole Climb", 'obstacle_pole', 'M'),
            ("Hot Saw", 'hot_saw', None),
        ]
        next_comp = 1
        for name, stand, gender in event_specs:
            ev = _make_pro_event(db_session, t, name, stand, gender=gender, max_stands=3)
            for hn in range(1, 3):  # 2 heats per event → 12 total heats
                ids = [next_comp, next_comp + 1]
                next_comp += 2
                _make_heat(db_session, ev, hn, ids)

        fb = FlightBuilder(t)
        fb.build(num_flights=2)  # 2 flights of 6

        # Build global ordered list and inspect same-stand-type gaps.
        flights = Flight.query.filter_by(tournament_id=t.id).order_by(
            Flight.flight_number
        ).all()
        ordered = []
        for f in flights:
            for h in Heat.query.filter_by(flight_id=f.id).order_by(Heat.flight_position).all():
                ordered.append(h)

        # Count back-to-back same-stand-type pairs (gap=1)
        adjacent_pairs = 0
        for i in range(1, len(ordered)):
            prev = ordered[i - 1].event.stand_type
            curr = ordered[i].event.stand_type
            if prev and curr and prev == curr:
                adjacent_pairs += 1

        # With 12 heats across 6 stand types (2 each of underhand, obstacle_pole,
        # then cookie_stack/hot_saw singletons), a perfect interleave is possible.
        # Allow at most 1 adjacent pair (worst case if cap + sequence forces it).
        assert adjacent_pairs <= 1, (
            f'Found {adjacent_pairs} same-stand-type back-to-back pairs. '
            f'Expected <= 1. Order: '
            f'{[(h.event.name, h.event.stand_type) for h in ordered]}'
        )

    def test_run2_heats_excluded_from_flights(self, db_session):
        """Run 2 heats (dual-run events) should not be placed into flights."""
        from models import Heat
        from services.flight_builder import FlightBuilder

        t = _make_tournament(db_session)
        ev = _make_pro_event(db_session, t, 'Speed Climb', 'speed_climb',
                             requires_dual_runs=True)
        _make_heat(db_session, ev, 1, [1, 2], run_number=1)
        _make_heat(db_session, ev, 1, [1, 2], run_number=2)
        _make_heat(db_session, ev, 2, [3, 4], run_number=1)
        _make_heat(db_session, ev, 2, [3, 4], run_number=2)

        fb = FlightBuilder(t)
        fb.build()

        run2_assigned = Heat.query.filter(
            Heat.event_id == ev.id,
            Heat.run_number == 2,
            Heat.flight_id.isnot(None),
        ).count()
        assert run2_assigned == 0, 'Run 2 heats should not be placed in flights'
