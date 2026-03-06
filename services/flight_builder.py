"""
Flight builder service for pro competition scheduling.
Builds flights with event variety for crowd engagement.
Ensures competitors have maximum rest between their events (target: 5+ heats, minimum: 4).
"""
from database import db
from models import Tournament, Event, Heat, HeatAssignment, Flight
import math
import json
import random


# Minimum number of heats between a competitor's appearances
MIN_HEAT_SPACING = 4
TARGET_HEAT_SPACING = 5
PARTNERED_AXE_EVENT_NAME = 'Partnered Axe Throw'
PARTNERED_AXE_SHOW_TEAM_COUNT = 4


def build_pro_flights(tournament: Tournament, num_flights: int = None) -> int:
    """
    Build flights for pro competition with event variety and competitor spacing.

    Flights mix heats from different events to keep crowd engaged while
    ensuring competitors have adequate rest between their events.

    Args:
        tournament: Tournament to build flights for
        num_flights: Total number of flights to create. When provided, heats are
                     distributed evenly across that many flights. When omitted,
                     defaults to distributing in blocks of 8 heats per flight.

    Returns:
        Number of flights created
    """
    # Clear existing flights (null out Heat.flight_id first to satisfy FK constraints)
    existing_flight_ids = [
        f.id for f in Flight.query.filter_by(tournament_id=tournament.id).with_entities(Flight.id).all()
    ]
    if existing_flight_ids:
        Heat.query.filter(Heat.flight_id.in_(existing_flight_ids)).update(
            {'flight_id': None, 'flight_position': None}, synchronize_session=False
        )
    Flight.query.filter_by(tournament_id=tournament.id).delete(synchronize_session=False)

    # Get all pro event heats
    pro_events = tournament.events.filter_by(event_type='pro').all()
    partnered_axe_event = next(
        (event for event in pro_events if event.name == PARTNERED_AXE_EVENT_NAME),
        None
    )
    partnered_axe_heats = _prepare_partnered_axe_show_heats(partnered_axe_event)

    # Collect all non-axe heats with their competitor information
    all_heats = []
    for event in pro_events:
        if partnered_axe_event and event.id == partnered_axe_event.id:
            continue
        event_heats = event.heats.filter_by(run_number=1).order_by(Heat.heat_number).all()
        for heat in event_heats:
            all_heats.append({
                'heat': heat,
                'event': event,
                'competitors': set(heat.get_competitors())
            })

    if not all_heats and not partnered_axe_heats:
        return 0

    # Build optimized heat order using competitor spacing algorithm
    ordered_heats = _optimize_heat_order(all_heats)
    total_heats = len(ordered_heats)

    # Derive heats_per_flight from caller-supplied num_flights, or fall back to default of 8.
    if num_flights and num_flights > 0 and total_heats > 0:
        target_flights = int(num_flights)
        heats_per_flight = math.ceil(total_heats / target_flights)
    else:
        heats_per_flight = 8
        target_flights = math.ceil(total_heats / heats_per_flight) if total_heats else 0

    # Promote the first pro springboard heat in each flight block to position 0
    # of that block so every flight opens with a springboard cut.
    ordered_heats = _promote_springboard_to_flight_start(ordered_heats, heats_per_flight)

    # Partnered axe requires one heat per flight, so ensure enough flights.
    if target_flights == 0 and partnered_axe_heats:
        target_flights = 1

    # Create flights and assign non-axe heats
    flights_created = 0
    heat_index = 0
    created_flights: list[Flight] = []

    for flight_num in range(1, target_flights + 1):
        flight = Flight(
            tournament_id=tournament.id,
            flight_number=flight_num
        )
        db.session.add(flight)
        db.session.flush()
        created_flights.append(flight)

        heats_in_flight = 0
        while heats_in_flight < heats_per_flight and heat_index < total_heats:
            heat_data = ordered_heats[heat_index]
            heat_data['heat'].flight_id = flight.id
            heat_data['heat'].flight_position = heats_in_flight + 1
            heat_index += 1
            heats_in_flight += 1

        flights_created += 1

    # Insert partnered axe heats with random flight placement across flights.
    _insert_partnered_axe_heats(created_flights, partnered_axe_heats)

    db.session.commit()
    return flights_created


def _prepare_partnered_axe_show_heats(event: Event | None) -> list[Heat]:
    """
    Return partnered axe heats to place into the show.

    If prelim standings are available, rebuild partnered axe to the top
    PARTNERED_AXE_SHOW_TEAM_COUNT pairs.
    """
    if not event:
        return []

    qualifier_pairs = _get_partnered_axe_qualifier_pairs(event, PARTNERED_AXE_SHOW_TEAM_COUNT)
    if not qualifier_pairs:
        return event.heats.filter_by(run_number=1).order_by(Heat.heat_number).all()

    heat_ids = [h.id for h in Heat.query.filter_by(event_id=event.id).with_entities(Heat.id).all()]
    if heat_ids:
        HeatAssignment.query.filter(HeatAssignment.heat_id.in_(heat_ids)).delete(synchronize_session=False)
    Heat.query.filter_by(event_id=event.id).delete(synchronize_session=False)

    created = []
    for idx, pair in enumerate(qualifier_pairs, start=1):
        comp1 = pair.get('competitor1', {}) or {}
        comp2 = pair.get('competitor2', {}) or {}
        comp_ids = []
        if isinstance(comp1.get('id'), int):
            comp_ids.append(comp1['id'])
        if isinstance(comp2.get('id'), int):
            comp_ids.append(comp2['id'])

        heat = Heat(
            event_id=event.id,
            heat_number=idx,
            run_number=1
        )
        heat.set_competitors(comp_ids)
        for comp_id in comp_ids:
            # Partnered axe pair shares one target.
            heat.set_stand_assignment(comp_id, 1)
        db.session.add(heat)
        created.append(heat)

    db.session.flush()
    for heat in created:
        heat.sync_assignments('pro')
    return created


def _get_partnered_axe_qualifier_pairs(event: Event, count: int) -> list[dict]:
    """Read prelim standings from partnered axe event state and return top N pairs."""
    try:
        state = json.loads(event.payouts or '{}')
    except Exception:
        return []

    prelim_results = state.get('prelim_results')
    if not isinstance(prelim_results, list):
        prelim_results = []

    if not prelim_results:
        pairs = state.get('pairs', [])
        if isinstance(pairs, list):
            prelim_results = [p for p in pairs if p.get('prelim_score') is not None]
            prelim_results.sort(key=lambda x: x.get('prelim_score', 0), reverse=True)

    valid_pairs = []
    for pair in prelim_results:
        comp1 = pair.get('competitor1', {}) or {}
        comp2 = pair.get('competitor2', {}) or {}
        if not isinstance(comp1.get('id'), int) or not isinstance(comp2.get('id'), int):
            continue
        valid_pairs.append(pair)

    return valid_pairs[:count]


def _insert_partnered_axe_heats(flights: list[Flight], axe_heats: list[Heat]) -> None:
    """Assign partnered axe heats across flights with random distribution."""
    if not flights or not axe_heats:
        return

    flight_pool = list(flights)
    random.shuffle(flight_pool)

    shuffled_heats = list(axe_heats)
    random.shuffle(shuffled_heats)

    # If there are more axe heats than flights, loop and allow double-ups.
    for idx, heat in enumerate(shuffled_heats):
        flight = flight_pool[idx % len(flight_pool)]
        heat.flight_id = flight.id
        heat.flight_position = _next_flight_position(flight.id)


def _next_flight_position(flight_id: int) -> int:
    """Return next 1-based display position within a flight."""
    max_pos = db.session.query(db.func.max(Heat.flight_position)).filter(
        Heat.flight_id == flight_id
    ).scalar()
    return int(max_pos or 0) + 1


def _promote_springboard_to_flight_start(ordered_heats: list, heats_per_flight: int) -> list:
    """
    Move the first pro springboard heat in each flight block to position 0 of that block.

    Each flight should open with a springboard cut when one is available.
    Only the first springboard heat in a block is moved — this preserves the
    sequential heat-number constraint because that heat is already the next
    sequential springboard heat in the global order.
    """
    result = list(ordered_heats)
    total = len(result)

    for block_start in range(0, total, heats_per_flight):
        block_end = min(block_start + heats_per_flight, total)

        # Find the first springboard heat in this block (skip position 0 — already correct).
        sb_idx = None
        for i in range(block_start + 1, block_end):
            hd = result[i]
            if (getattr(hd['event'], 'event_type', '') == 'pro' and
                    getattr(hd['event'], 'stand_type', '') == 'springboard'):
                sb_idx = i
                break

        if sb_idx is not None:
            sb_heat = result.pop(sb_idx)
            result.insert(block_start, sb_heat)

    return result


def _optimize_heat_order(all_heats: list) -> list:
    """
    Optimize heat order using a per-event sequential queue.

    At each step only the NEXT unplaced heat from each event is eligible.
    This guarantees that within any event, heats appear in ascending heat_number
    order across the show (Heat 1 before Heat 2 before Heat 3 ...).
    The greedy pick at each step still maximises competitor spacing across events.

    Args:
        all_heats: List of heat data dicts with 'heat', 'event', 'competitors'

    Returns:
        Ordered list of heat data dicts
    """
    if not all_heats:
        return []

    from collections import defaultdict

    # Build a sorted queue for each event (by heat_number then run_number).
    event_queues: dict[int, list] = defaultdict(list)
    for heat_data in all_heats:
        event_queues[heat_data['heat'].event_id].append(heat_data)
    for eid in event_queues:
        event_queues[eid].sort(
            key=lambda h: (h['heat'].heat_number, h['heat'].run_number)
        )
    event_ids = list(event_queues.keys())
    # Pointer to the next unplaced heat in each event's queue.
    event_ptrs: dict[int, int] = {eid: 0 for eid in event_ids}

    ordered: list = []
    competitor_last_heat: dict[int, int] = {}
    stand_type_last_position: dict[str, int] = {}

    while True:
        # Collect candidates: front of each non-exhausted event queue.
        candidates = [
            (eid, event_queues[eid][event_ptrs[eid]])
            for eid in event_ids
            if event_ptrs[eid] < len(event_queues[eid])
        ]
        if not candidates:
            break

        current_position = len(ordered)

        # Score every candidate with stand-conflict enforcement.
        scored = [
            (
                _calculate_heat_score(
                    hd['competitors'],
                    competitor_last_heat,
                    current_position,
                    hd['event'],
                    stand_type_last_position,
                ),
                eid,
                hd,
            )
            for eid, hd in candidates
        ]

        best_score, best_eid, best_heat_data = max(scored, key=lambda x: x[0])

        # If every candidate is blocked by a stand conflict, re-score ignoring it.
        if best_score < 0:
            scored_no_conflict = [
                (
                    _calculate_heat_score(
                        hd['competitors'],
                        competitor_last_heat,
                        current_position,
                        hd['event'],
                        None,  # disable stand conflict check
                    ),
                    eid,
                    hd,
                )
                for eid, hd in candidates
            ]
            _, best_eid, best_heat_data = max(scored_no_conflict, key=lambda x: x[0])

        ordered.append(best_heat_data)
        event_ptrs[best_eid] += 1

        pos = len(ordered) - 1
        for comp_id in best_heat_data['competitors']:
            competitor_last_heat[comp_id] = pos
        stand_type = getattr(best_heat_data['event'], 'stand_type', None)
        if stand_type:
            stand_type_last_position[stand_type] = pos

    return ordered


_CONFLICTING_STANDS: dict[str, str] = {
    'standing_block': 'cookie_stack',
    'cookie_stack': 'standing_block',
}
# Minimum gap between conflicting stand types (approximately one flight)
_STAND_CONFLICT_GAP = 8


def _calculate_heat_score(competitors: set, competitor_last_heat: dict,
                          current_position: int, event: Event,
                          stand_type_last_position: dict | None = None) -> float:
    """
    Calculate a score for placing a heat at the current position.

    Higher score = better placement. Score is based on:
    - Minimum spacing for all competitors (must be >= MIN_HEAT_SPACING or first appearance)
    - Average spacing across all competitors
    - Stand conflict enforcement (cookie_stack / standing_block mutual exclusion)

    Args:
        competitors: Set of competitor IDs in this heat
        competitor_last_heat: Dict of competitor_id -> last heat index
        current_position: Current position in the ordered list
        event: The event this heat belongs to
        stand_type_last_position: Dict of stand_type -> last position (for conflict checks)

    Returns:
        Score (higher is better), or -1 if invalid placement
    """
    # Enforce stand type conflict: cookie_stack and standing_block share physical stands
    stand_type = getattr(event, 'stand_type', None)
    if stand_type and stand_type in _CONFLICTING_STANDS and stand_type_last_position is not None:
        conflict_type = _CONFLICTING_STANDS[stand_type]
        last_conflict = stand_type_last_position.get(conflict_type)
        if last_conflict is not None and (current_position - last_conflict) < _STAND_CONFLICT_GAP:
            return -1.0

    if not competitors:
        return 100.0  # Empty heats can go anywhere

    min_spacing = float('inf')
    total_spacing = 0
    competitor_count = 0

    for comp_id in competitors:
        last_heat = competitor_last_heat.get(comp_id)

        if last_heat is not None:
            spacing = current_position - last_heat
            min_spacing = min(min_spacing, spacing)
            total_spacing += spacing
            competitor_count += 1

    # If all competitors are new, this is a great placement
    if competitor_count == 0:
        return 1000.0

    # Check if minimum spacing requirement is met
    if min_spacing < MIN_HEAT_SPACING:
        # Penalize but don't completely reject - may be necessary
        # Score decreases exponentially as spacing decreases
        penalty = (MIN_HEAT_SPACING - min_spacing) * 100
        return max(0, 50 - penalty)

    # Calculate average spacing bonus
    avg_spacing = total_spacing / competitor_count

    # Score based on minimum spacing (most important) plus average spacing bonus
    score = min_spacing * 10 + avg_spacing

    # Bonus for meeting target spacing
    if min_spacing >= TARGET_HEAT_SPACING:
        score += 50

    return score


def optimize_flight_for_ability(flight: Flight, event: Event):
    """
    Reorder heats within a flight to group by ability.
    Particularly important for springboard to keep similar-speed cutters together.

    Args:
        flight: Flight to optimize
        event: Event to optimize within the flight
    """
    # Get heats for this event in this flight
    event_heats = flight.heats.filter_by(event_id=event.id).all()

    if len(event_heats) <= 1:
        return

    # For now, just ensure they're sequential
    # Future: Could reorder based on predicted times
    pass


def insert_axe_throw_finals(tournament: Tournament, top_teams: list):
    """
    Insert Partnered Axe Throw finals into flights.
    One team throws per flight.

    Args:
        tournament: Tournament
        top_teams: List of top 4 team identifiers from prelims
    """
    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).limit(4).all()

    axe_event = tournament.events.filter_by(name='Partnered Axe Throw', event_type='pro').first()

    if not axe_event or not flights:
        return

    for i, team in enumerate(top_teams[:4]):
        if i < len(flights):
            # Create a finals heat for this team
            heat = Heat(
                event_id=axe_event.id,
                heat_number=100 + i,  # High number to indicate finals
                run_number=1,
                flight_id=flights[i].id
            )
            # Note: team assignment would be handled separately
            db.session.add(heat)

    db.session.commit()


def get_flight_summary(tournament: Tournament) -> list:
    """
    Get a summary of all flights for display.

    Returns:
        List of flight summaries with event breakdown
    """
    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).all()

    summaries = []
    for flight in flights:
        heats = flight.heats.all()

        # Count heats by event
        event_counts = {}
        for heat in heats:
            event_name = heat.event.display_name if heat.event else 'Unknown'
            event_counts[event_name] = event_counts.get(event_name, 0) + 1

        summaries.append({
            'flight': flight,
            'heat_count': len(heats),
            'event_counts': event_counts,
            'event_variety': len(event_counts),
            'status': flight.status
        })

    return summaries


def validate_competitor_spacing(tournament: Tournament) -> dict:
    """
    Validate that competitor spacing meets requirements.

    Returns:
        Dict with validation results and any violations
    """
    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).all()

    # Build ordered list of all heats
    all_heats = []
    for flight in flights:
        flight_heats = flight.heats.order_by(Heat.id).all()
        all_heats.extend(flight_heats)

    # Track competitor appearances
    competitor_appearances = {}
    violations = []

    for i, heat in enumerate(all_heats):
        competitors = heat.get_competitors()
        for comp_id in competitors:
            if comp_id in competitor_appearances:
                last_appearance = competitor_appearances[comp_id]
                spacing = i - last_appearance
                if spacing < MIN_HEAT_SPACING:
                    violations.append({
                        'competitor_id': comp_id,
                        'heat_1': last_appearance + 1,
                        'heat_2': i + 1,
                        'spacing': spacing,
                        'required': MIN_HEAT_SPACING
                    })
            competitor_appearances[comp_id] = i

    return {
        'valid': len(violations) == 0,
        'total_heats': len(all_heats),
        'violations': violations,
        'violation_count': len(violations)
    }


def integrate_college_spillover_into_flights(tournament: Tournament, college_event_ids: list[int] | None = None) -> dict:
    """
    Assign selected college spillover heats into existing Saturday pro flights.

    Chokerman's Race only contributes run 2 per Missoula rules.
    """
    selected_ids = set(int(v) for v in (college_event_ids or []))
    mandatory = tournament.events.filter_by(event_type='college', name="Chokerman's Race").first()
    if mandatory:
        selected_ids.add(mandatory.id)

    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).all()
    if not flights:
        return {'integrated_heats': 0, 'events': 0, 'message': 'No flights available.'}

    events = tournament.events.filter(Event.id.in_(selected_ids)).all() if selected_ids else []
    if not events:
        return {'integrated_heats': 0, 'events': 0, 'message': 'No selected spillover events.'}

    last_flight = flights[-1]
    integrated = 0
    per_event = 0
    flight_idx = 0
    for event in sorted(events, key=lambda e: (e.name, e.gender or '')):
        if event.name == "Chokerman's Race":
            # Run 2 only on Saturday. All heats group together at the end of
            # the last flight in the same heat-number order as Run 1.
            heats = event.heats.filter_by(run_number=2).order_by(Heat.heat_number).all()
        else:
            heats = event.heats.order_by(Heat.run_number, Heat.heat_number).all()

        if not heats:
            continue
        per_event += 1
        for heat in heats:
            # Keep preexisting placement if already integrated.
            if heat.flight_id is not None:
                continue
            if event.name == "Chokerman's Race":
                # Always place at end of last flight.
                heat.flight_id = last_flight.id
                heat.flight_position = _next_flight_position(last_flight.id)
            else:
                target = flights[flight_idx % len(flights)]
                heat.flight_id = target.id
                heat.flight_position = _next_flight_position(target.id)
                flight_idx += 1
            integrated += 1

    db.session.flush()
    return {
        'integrated_heats': integrated,
        'events': per_event,
        'message': 'College spillover heats integrated into flights.',
    }
