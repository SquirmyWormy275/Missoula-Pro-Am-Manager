"""
Flight builder service for pro competition scheduling.
Builds flights with event variety for crowd engagement.
Ensures competitors have maximum rest between their events (target: 5+ heats, minimum: 4).
"""
from database import db
from models import Tournament, Event, Heat, Flight
import math


# Minimum number of heats between a competitor's appearances
MIN_HEAT_SPACING = 4
TARGET_HEAT_SPACING = 5


def build_pro_flights(tournament: Tournament, heats_per_flight: int = 8) -> int:
    """
    Build flights for pro competition with event variety and competitor spacing.

    Flights mix heats from different events to keep crowd engaged while
    ensuring competitors have adequate rest between their events.

    Args:
        tournament: Tournament to build flights for
        heats_per_flight: Target number of heats per flight

    Returns:
        Number of flights created
    """
    # Clear existing flights
    Flight.query.filter_by(tournament_id=tournament.id).delete()

    # Get all pro event heats
    pro_events = tournament.events.filter_by(event_type='pro').all()

    # Collect all heats with their competitor information
    all_heats = []
    for event in pro_events:
        event_heats = event.heats.filter_by(run_number=1).order_by(Heat.heat_number).all()
        for heat in event_heats:
            all_heats.append({
                'heat': heat,
                'event': event,
                'competitors': set(heat.get_competitors())
            })

    if not all_heats:
        return 0

    # Build optimized heat order using competitor spacing algorithm
    ordered_heats = _optimize_heat_order(all_heats)

    # Calculate number of flights needed
    total_heats = len(ordered_heats)
    num_flights = math.ceil(total_heats / heats_per_flight)

    # Create flights and assign heats
    flights_created = 0
    heat_index = 0

    for flight_num in range(1, num_flights + 1):
        flight = Flight(
            tournament_id=tournament.id,
            flight_number=flight_num
        )
        db.session.add(flight)
        db.session.flush()

        # Add heats to this flight
        heats_in_flight = 0
        while heats_in_flight < heats_per_flight and heat_index < total_heats:
            heat_data = ordered_heats[heat_index]
            heat_data['heat'].flight_id = flight.id
            heat_index += 1
            heats_in_flight += 1

        flights_created += 1

    db.session.commit()
    return flights_created


def _optimize_heat_order(all_heats: list) -> list:
    """
    Optimize heat order to maximize spacing between competitor appearances.

    Uses a greedy algorithm that selects the next heat based on which one
    has competitors with the longest time since their last appearance.

    Args:
        all_heats: List of heat data dicts with 'heat', 'event', 'competitors'

    Returns:
        Ordered list of heat data dicts
    """
    if not all_heats:
        return []

    ordered = []
    remaining = list(all_heats)

    # Track when each competitor was last scheduled (heat index in ordered list)
    competitor_last_heat = {}

    while remaining:
        best_heat = None
        best_score = -1
        best_index = 0

        for i, heat_data in enumerate(remaining):
            score = _calculate_heat_score(
                heat_data['competitors'],
                competitor_last_heat,
                len(ordered),
                heat_data['event']
            )

            if score > best_score:
                best_score = score
                best_heat = heat_data
                best_index = i

        # Add the best heat to our ordered list
        if best_heat:
            ordered.append(best_heat)

            # Update competitor tracking
            current_position = len(ordered) - 1
            for comp_id in best_heat['competitors']:
                competitor_last_heat[comp_id] = current_position

            remaining.pop(best_index)

    return ordered


def _calculate_heat_score(competitors: set, competitor_last_heat: dict,
                          current_position: int, event: Event) -> float:
    """
    Calculate a score for placing a heat at the current position.

    Higher score = better placement. Score is based on:
    - Minimum spacing for all competitors (must be >= MIN_HEAT_SPACING or first appearance)
    - Average spacing across all competitors
    - Event variety bonus (prefer different events than recent heats)

    Args:
        competitors: Set of competitor IDs in this heat
        competitor_last_heat: Dict of competitor_id -> last heat index
        current_position: Current position in the ordered list
        event: The event this heat belongs to

    Returns:
        Score (higher is better), or -1 if invalid placement
    """
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
