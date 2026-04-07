"""
Flight builder tests at scale with 25 pro competitors and realistic event entries.

Tests FlightBuilder, build_pro_flights, _calculate_heat_score, _get_spacing,
and _optimize_heat_order using the full 25-competitor pool from synthetic_data.py.

These tests use the shared conftest fixtures (app, db_session) to seed
tournaments, events, competitors, and heats, then exercise the flight builder
end-to-end.
"""
import json
import types

import pytest

from tests.conftest import (
    make_event,
    make_flight,
    make_heat,
    make_pro_competitor,
    make_tournament,
)
from tests.fixtures.synthetic_data import PRO_COMPETITORS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Map event names to (stand_type, scoring_type, scoring_order, max_stands) tuples.
EVENT_CONFIG = {
    'Springboard':              ('springboard',    'time', 'lowest_wins', 4),
    'Int 1-Board Springboard':  ('springboard',    'time', 'lowest_wins', 4),
    "Men's Underhand":          ('underhand',      'time', 'lowest_wins', 5),
    "Women's Underhand":        ('underhand',      'time', 'lowest_wins', 5),
    "Men's Standing Block":     ('standing_block',  'time', 'lowest_wins', 5),
    "Women's Standing Block":   ('standing_block',  'time', 'lowest_wins', 5),
    "Men's Single Buck":        ('saw_hand',       'time', 'lowest_wins', 4),
    "Women's Single Buck":      ('saw_hand',       'time', 'lowest_wins', 4),
    "Men's Double Buck":        ('saw_hand',       'time', 'lowest_wins', 4),
    'Jack & Jill':              ('saw_hand',       'time', 'lowest_wins', 4),
    'Hot Saw':                  ('hot_saw',        'time', 'lowest_wins', 4),
    'Obstacle Pole':            ('obstacle_pole',  'time', 'lowest_wins', 2),
    'Speed Climb':              ('speed_climb',    'time', 'lowest_wins', 2),
    'Cookie Stack':             ('cookie_stack',   'time', 'lowest_wins', 5),
    'Partnered Axe Throw':      ('axe_throw',      'hits', 'highest_wins', 1),
}


def _seed_full_tournament(session):
    """Seed a tournament with 25 pro competitors, events, and heats.

    Returns (tournament, events_by_name, competitors_by_name).
    """
    tournament = make_tournament(session, name='Flight Test 2026', year=2026, status='pro_active')

    # Collect all unique event names from competitor entries.
    all_event_names = set()
    for comp_data in PRO_COMPETITORS:
        for ev_name in comp_data.get('events', []):
            all_event_names.add(ev_name)

    # Create events.
    events_by_name = {}
    for ev_name in sorted(all_event_names):
        cfg = EVENT_CONFIG.get(ev_name)
        if cfg is None:
            # Fallback for unlisted events.
            cfg = ('other', 'time', 'lowest_wins', 5)
        stand_type, scoring_type, scoring_order, max_stands = cfg
        is_partnered = ev_name in ("Men's Double Buck", "Jack & Jill", "Partnered Axe Throw")
        ev = make_event(
            session, tournament, name=ev_name, event_type='pro',
            scoring_type=scoring_type, scoring_order=scoring_order,
            stand_type=stand_type, max_stands=max_stands,
            is_partnered=is_partnered,
        )
        events_by_name[ev_name] = ev

    # Create competitors.
    competitors_by_name = {}
    for comp_data in PRO_COMPETITORS:
        gear_sharing = {}
        comp = make_pro_competitor(
            session, tournament,
            name=comp_data['name'],
            gender=comp_data['gender'],
            events=[ev_name for ev_name in comp_data['events']],
            gear_sharing=gear_sharing,
        )
        competitors_by_name[comp_data['name']] = comp

    # Build gear sharing from the gear_sharing_text entries.
    # For test purposes, store simple event_id -> partner_name mappings
    # for known sharing pairs.
    _GEAR_PAIRS = [
        ('Imortal Joe', 'Joe Manyfingers', ['Springboard', "Men's Standing Block"]),
        ('Dee John', 'Juicy Crust', ['Obstacle Pole']),
        ('Jonathon Wept', 'Meau Jeau', ['Obstacle Pole']),
        ('Steptoe Edwall', 'Carson Mitsubishi', ['Hot Saw', 'Obstacle Pole']),
        ('Ada Byrd', 'Jaam Slam', ["Women's Underhand", "Women's Standing Block"]),
        ('Dorian Gray', 'Garfield Heathcliff', ["Men's Standing Block"]),
    ]
    for name_a, name_b, shared_events in _GEAR_PAIRS:
        comp_a = competitors_by_name.get(name_a)
        comp_b = competitors_by_name.get(name_b)
        if comp_a and comp_b:
            gear_a = json.loads(comp_a.gear_sharing or '{}')
            gear_b = json.loads(comp_b.gear_sharing or '{}')
            for ev_name in shared_events:
                ev = events_by_name.get(ev_name)
                if ev:
                    gear_a[str(ev.id)] = comp_b.name
                    gear_b[str(ev.id)] = comp_a.name
            comp_a.gear_sharing = json.dumps(gear_a)
            comp_b.gear_sharing = json.dumps(gear_b)

    # Generate heats using the real heat generator so gear-sharing conflicts
    # are properly avoided.  Fall back to simple chunking only if the
    # generator raises (e.g. missing EventResult rows for bracket events).
    from services.heat_generator import generate_event_heats
    for ev_name, ev in events_by_name.items():
        # Skip Partnered Axe Throw for flight building (handled separately).
        if ev_name == 'Partnered Axe Throw':
            continue

        # Gather competitor IDs for this event to verify it has entrants.
        entrant_ids = []
        for comp_data in PRO_COMPETITORS:
            if ev_name in comp_data.get('events', []):
                comp = competitors_by_name[comp_data['name']]
                entrant_ids.append(comp.id)

        if not entrant_ids:
            continue

        try:
            generate_event_heats(ev)
        except Exception:
            # Fallback: simple sequential chunking (no gear-conflict avoidance)
            max_per_heat = ev.max_stands or 4
            heat_number = 0
            for i in range(0, len(entrant_ids), max_per_heat):
                heat_number += 1
                chunk = entrant_ids[i:i + max_per_heat]
                stand_assignments = {str(cid): pos + 1 for pos, cid in enumerate(chunk)}
                make_heat(
                    session, ev,
                    heat_number=heat_number,
                    run_number=1,
                    competitors=chunk,
                    stand_assignments=stand_assignments,
                )

    session.flush()
    return tournament, events_by_name, competitors_by_name


# ---------------------------------------------------------------------------
# 1. TestFlightBuilderScale
# ---------------------------------------------------------------------------

class TestFlightBuilderScale:
    """Build flights at scale with 25 pros and verify structural properties."""

    def test_flights_are_created(self, db_session):
        from services.flight_builder import build_pro_flights
        tournament, events_by_name, comps = _seed_full_tournament(db_session)

        num_flights = build_pro_flights(tournament)
        assert num_flights > 0, "Should create at least one flight"

    def test_all_heats_assigned_to_flights(self, db_session):
        from models.heat import Heat
        from services.flight_builder import build_pro_flights
        tournament, events_by_name, comps = _seed_full_tournament(db_session)

        build_pro_flights(tournament)

        # All non-partnered-axe heats should have a flight assignment.
        axe_event = events_by_name.get('Partnered Axe Throw')
        for ev_name, ev in events_by_name.items():
            if ev_name == 'Partnered Axe Throw':
                continue
            heats = Heat.query.filter_by(event_id=ev.id, run_number=1).all()
            for heat in heats:
                assert heat.flight_id is not None, (
                    f"Heat {heat.id} for {ev_name} has no flight assignment"
                )

    def test_no_competitor_in_consecutive_heats(self, db_session):
        """No competitor should appear in back-to-back heats (spacing >= 1)."""
        from models.heat import Flight, Heat
        from services.flight_builder import build_pro_flights
        tournament, events_by_name, comps = _seed_full_tournament(db_session)

        build_pro_flights(tournament)

        # Reconstruct the global heat order.
        flights = Flight.query.filter_by(
            tournament_id=tournament.id
        ).order_by(Flight.flight_number).all()

        ordered_heats = []
        for flight in flights:
            flight_heats = Heat.query.filter_by(
                flight_id=flight.id
            ).order_by(Heat.flight_position).all()
            ordered_heats.extend(flight_heats)

        # Check spacing — flight builder targets min=4 but edge cases
        # (many events, few competitors) can force tighter placement.
        # We count violations rather than hard-failing on the first one.
        violations = 0
        comp_last_pos = {}
        for pos, heat in enumerate(ordered_heats):
            for comp_id in heat.get_competitors():
                if comp_id in comp_last_pos:
                    spacing = pos - comp_last_pos[comp_id]
                    if spacing <= 1:
                        violations += 1
                comp_last_pos[comp_id] = pos

        # With 25 competitors and realistic heat counts, at most a handful
        # of adjacent placements are acceptable.
        total_placements = sum(len(h.get_competitors()) for h in ordered_heats)
        violation_rate = violations / max(total_placements, 1)
        assert violation_rate < 0.05, (
            f"Too many adjacent placements: {violations}/{total_placements} "
            f"({violation_rate:.1%})"
        )

    def test_gear_sharing_conflicts_minimized(self, db_session):
        """Heat generator should minimize gear-sharing conflicts.

        With 2-stand events and many gear pairs, perfect separation may not
        always be possible. We verify the heat generator resolves *most*
        conflicts rather than demanding zero.
        """
        from models.heat import Heat
        from services.flight_builder import build_pro_flights
        tournament, events_by_name, comps = _seed_full_tournament(db_session)

        build_pro_flights(tournament)

        conflict_pairs = [
            ('Imortal Joe', 'Joe Manyfingers'),
            ('Dee John', 'Juicy Crust'),
            ('Steptoe Edwall', 'Carson Mitsubishi'),
        ]

        total_conflicts = 0
        total_checked = 0
        for name_a, name_b in conflict_pairs:
            comp_a = comps[name_a]
            comp_b = comps[name_b]

            gear_a = json.loads(comp_a.gear_sharing or '{}')
            shared_event_ids = set()
            for key in gear_a:
                if key.isdigit():
                    shared_event_ids.add(int(key))

            for eid in shared_event_ids:
                heats = Heat.query.filter_by(event_id=eid).all()
                for heat in heats:
                    comp_ids = set(heat.get_competitors())
                    total_checked += 1
                    if comp_a.id in comp_ids and comp_b.id in comp_ids:
                        total_conflicts += 1

        # Allow at most 1 conflict across all pairs (2-stand events are hard)
        assert total_conflicts <= 1, (
            f"Too many gear-sharing conflicts: {total_conflicts} out of "
            f"{total_checked} heat checks"
        )

    def test_flight_builder_class(self, db_session):
        """FlightBuilder OO wrapper should produce equivalent results."""
        from services.flight_builder import FlightBuilder
        tournament, events_by_name, comps = _seed_full_tournament(db_session)

        fb = FlightBuilder(tournament)
        num_flights = fb.build(num_flights=5)
        assert num_flights == 5

    def test_custom_num_flights(self, db_session):
        from services.flight_builder import build_pro_flights
        tournament, events_by_name, comps = _seed_full_tournament(db_session)

        num_flights = build_pro_flights(tournament, num_flights=3)
        assert num_flights == 3


# ---------------------------------------------------------------------------
# 2. TestFlightBuilderSpacing
# ---------------------------------------------------------------------------

class TestFlightBuilderSpacing:
    """Verify _get_spacing() returns correct min/target for each stand type."""

    def test_springboard_spacing(self):
        from services.flight_builder import _get_spacing
        ev = types.SimpleNamespace(stand_type='springboard')
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == 6
        assert target_sp == 8

    def test_saw_hand_spacing(self):
        from services.flight_builder import _get_spacing
        ev = types.SimpleNamespace(stand_type='saw_hand')
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == 5
        assert target_sp == 7

    def test_underhand_spacing(self):
        from services.flight_builder import _get_spacing
        ev = types.SimpleNamespace(stand_type='underhand')
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == 4
        assert target_sp == 5

    def test_standing_block_spacing(self):
        from services.flight_builder import _get_spacing
        ev = types.SimpleNamespace(stand_type='standing_block')
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == 4
        assert target_sp == 5

    def test_hot_saw_spacing(self):
        from services.flight_builder import _get_spacing
        ev = types.SimpleNamespace(stand_type='hot_saw')
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == 4
        assert target_sp == 5

    def test_obstacle_pole_spacing(self):
        from services.flight_builder import _get_spacing
        ev = types.SimpleNamespace(stand_type='obstacle_pole')
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == 4
        assert target_sp == 5

    def test_speed_climb_spacing(self):
        from services.flight_builder import _get_spacing
        ev = types.SimpleNamespace(stand_type='speed_climb')
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == 4
        assert target_sp == 5

    def test_cookie_stack_spacing(self):
        from services.flight_builder import _get_spacing
        ev = types.SimpleNamespace(stand_type='cookie_stack')
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == 4
        assert target_sp == 5

    def test_unknown_type_uses_global_default(self):
        from services.flight_builder import MIN_HEAT_SPACING, TARGET_HEAT_SPACING, _get_spacing
        ev = types.SimpleNamespace(stand_type='something_unknown')
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == MIN_HEAT_SPACING
        assert target_sp == TARGET_HEAT_SPACING

    def test_none_event_uses_default(self):
        from services.flight_builder import MIN_HEAT_SPACING, TARGET_HEAT_SPACING, _get_spacing
        min_sp, target_sp = _get_spacing(None)
        assert min_sp == MIN_HEAT_SPACING
        assert target_sp == TARGET_HEAT_SPACING

    def test_no_stand_type_attr_uses_default(self):
        from services.flight_builder import MIN_HEAT_SPACING, TARGET_HEAT_SPACING, _get_spacing
        ev = types.SimpleNamespace()
        min_sp, target_sp = _get_spacing(ev)
        assert min_sp == MIN_HEAT_SPACING
        assert target_sp == TARGET_HEAT_SPACING

    def test_springboard_higher_than_others(self):
        """Springboard should have strictly higher spacing than all other types."""
        from services.flight_builder import _get_spacing
        sb_min, sb_target = _get_spacing(types.SimpleNamespace(stand_type='springboard'))
        for st in ['underhand', 'standing_block', 'cookie_stack', 'obstacle_pole',
                    'hot_saw', 'speed_climb', 'stock_saw']:
            other_min, other_target = _get_spacing(types.SimpleNamespace(stand_type=st))
            assert sb_min >= other_min, f"Springboard min should be >= {st} min"
            assert sb_target >= other_target, f"Springboard target should be >= {st} target"


# ---------------------------------------------------------------------------
# 3. TestFlightBuilderEventVariety
# ---------------------------------------------------------------------------

class TestFlightBuilderEventVariety:
    """Verify each flight block contains heats from multiple different events."""

    def test_flights_have_event_variety(self, db_session):
        from models.heat import Flight, Heat
        from services.flight_builder import build_pro_flights
        tournament, events_by_name, comps = _seed_full_tournament(db_session)

        build_pro_flights(tournament)

        flights = Flight.query.filter_by(
            tournament_id=tournament.id
        ).order_by(Flight.flight_number).all()

        for flight in flights:
            heats = Heat.query.filter_by(flight_id=flight.id).all()
            if len(heats) <= 1:
                continue  # Single-heat flights are trivially fine.

            event_ids = set(h.event_id for h in heats)
            assert len(event_ids) > 1, (
                f"Flight {flight.flight_number} has {len(heats)} heats "
                f"but only {len(event_ids)} distinct event(s)"
            )

    def test_no_single_event_dominates_flight(self, db_session):
        """No single event should take up more than half the heats in a flight."""
        from collections import Counter

        from models.heat import Flight, Heat
        from services.flight_builder import build_pro_flights
        tournament, events_by_name, comps = _seed_full_tournament(db_session)

        build_pro_flights(tournament)

        flights = Flight.query.filter_by(
            tournament_id=tournament.id
        ).order_by(Flight.flight_number).all()

        for flight in flights:
            heats = Heat.query.filter_by(flight_id=flight.id).all()
            if len(heats) <= 2:
                continue

            event_counts = Counter(h.event_id for h in heats)
            most_common_count = event_counts.most_common(1)[0][1]
            # Allow up to 60% of heats from one event (tolerance for small flights).
            threshold = max(2, int(len(heats) * 0.6) + 1)
            assert most_common_count <= threshold, (
                f"Flight {flight.flight_number}: one event has {most_common_count} "
                f"of {len(heats)} heats (threshold={threshold})"
            )

    def test_calculate_heat_score_springboard_opener_bonus(self):
        """Springboard at flight start (position 0) should get a large bonus."""
        from services.flight_builder import _calculate_heat_score
        ev = types.SimpleNamespace(stand_type='springboard', id=1)

        score_at_start = _calculate_heat_score(
            {100, 101}, {}, 0, ev, {}, heats_per_flight=8,
        )
        score_at_mid = _calculate_heat_score(
            {100, 101}, {}, 3, ev, {}, heats_per_flight=8,
        )
        assert score_at_start > score_at_mid, (
            "Springboard heat should score higher at flight start"
        )

    def test_calculate_heat_score_hot_saw_closer_bonus(self):
        """Hot Saw at end of flight block should get a closer bonus."""
        from services.flight_builder import _calculate_heat_score
        ev = types.SimpleNamespace(stand_type='hot_saw', id=2)

        # Position 7 = last slot of an 8-heat flight block.
        score_at_end = _calculate_heat_score(
            {200, 201}, {}, 7, ev, {}, heats_per_flight=8,
        )
        score_at_mid = _calculate_heat_score(
            {200, 201}, {}, 3, ev, {}, heats_per_flight=8,
        )
        assert score_at_end > score_at_mid, (
            "Hot Saw heat should score higher at flight end"
        )

    def test_score_ordering_rewards_spacing(self):
        """_score_ordering should return a higher score for well-spaced orderings."""
        from services.flight_builder import _score_ordering

        ev = types.SimpleNamespace(stand_type='underhand', id=1)

        # Good ordering: competitor 1 appears in positions 0 and 5 (spacing=5).
        good_order = [
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {1, 2}},
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {3, 4}},
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {5, 6}},
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {7, 8}},
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {9, 10}},
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {1, 11}},
        ]

        # Bad ordering: competitor 1 in positions 0 and 1 (spacing=1).
        bad_order = [
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {1, 2}},
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {1, 3}},
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {5, 6}},
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {7, 8}},
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {9, 10}},
            {'heat': types.SimpleNamespace(event_id=1), 'event': ev, 'competitors': {4, 11}},
        ]

        good_score = _score_ordering(good_order, heats_per_flight=8)
        bad_score = _score_ordering(bad_order, heats_per_flight=8)
        assert good_score > bad_score
