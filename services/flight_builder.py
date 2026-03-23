"""
Flight builder service for pro competition scheduling.
Builds flights with event variety for crowd engagement.
Ensures competitors have maximum rest between their events using tiered spacing:
  Tier 1 (springboard):  min=6, target=8 heats between appearances
  Tier 2 (saw_hand):     min=5, target=7 heats between appearances
  Tier 3 (all others):   min=4, target=5 heats between appearances
"""
import logging
import math
import json
from collections import defaultdict

from database import db
from models import Tournament, Event, Heat, HeatAssignment, Flight

logger = logging.getLogger(__name__)


# Global fallback spacing (used for unknown stand types and college heats)
MIN_HEAT_SPACING = 4
TARGET_HEAT_SPACING = 5

PARTNERED_AXE_EVENT_NAME = 'Partnered Axe Throw'
PARTNERED_AXE_SHOW_TEAM_COUNT = 4

# How many independent greedy passes to run; best result is kept.
N_OPTIMIZATION_PASSES = 5

# Per-stand-type spacing tiers: (min_spacing, target_spacing).
# Springboard is the most physically demanding (long axe strokes, 3 boards per cut).
# Saw events are moderately demanding. Everything else uses the global minimum.
EVENT_SPACING_TIERS: dict[str, tuple[int, int]] = {
    'springboard':    (6, 8),
    'saw_hand':       (5, 7),
    'underhand':      (4, 5),
    'standing_block': (4, 5),
    'cookie_stack':   (4, 5),
    'obstacle_pole':  (4, 5),
    'hot_saw':        (4, 5),
    'speed_climb':    (4, 5),
    'stock_saw':      (4, 5),
}

_CONFLICTING_STANDS: dict[str, str] = {
    'standing_block': 'cookie_stack',
    'cookie_stack': 'standing_block',
}
# Minimum gap between conflicting stand types (approximately one flight block)
_STAND_CONFLICT_GAP = 8


def _get_spacing(event: Event | None) -> tuple[int, int]:
    """Return (min_spacing, target_spacing) for this event's stand type."""
    st = getattr(event, 'stand_type', None) or ''
    return EVENT_SPACING_TIERS.get(st, (MIN_HEAT_SPACING, TARGET_HEAT_SPACING))


def build_pro_flights(tournament: Tournament, num_flights: int = None) -> int:
    """
    Build flights for pro competition with event variety and competitor spacing.

    Flights mix heats from different events to keep crowd engaged while
    ensuring competitors have adequate rest between their events.

    Springboard heats naturally open each flight via a large scoring bonus.
    Hot Saw heats receive a bonus when placed as the flight closer.
    The greedy algorithm runs N_OPTIMIZATION_PASSES times and keeps the best result.

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

    # Collect all non-axe heats with their competitor information.
    # Batch-load all heats for non-axe events in a single query to avoid N+1.
    non_axe_events = [e for e in pro_events
                      if not (partnered_axe_event and e.id == partnered_axe_event.id)]
    non_axe_event_ids = [e.id for e in non_axe_events]
    event_by_id = {e.id: e for e in non_axe_events}

    batched_heats = (
        Heat.query
        .filter(Heat.event_id.in_(non_axe_event_ids), Heat.run_number == 1)
        .order_by(Heat.event_id, Heat.heat_number)
        .all()
    ) if non_axe_event_ids else []
    logger.debug('flight_builder: loaded %d non-axe heats for %d events',
                 len(batched_heats), len(non_axe_event_ids))

    all_heats = []
    for heat in batched_heats:
        event = event_by_id.get(heat.event_id)
        if event:
            all_heats.append({
                'heat': heat,
                'event': event,
                'competitors': set(heat.get_competitors()),
            })

    if not all_heats and not partnered_axe_heats:
        return 0

    # Derive heats_per_flight from caller-supplied num_flights, or fall back to default of 8.
    total_non_axe = len(all_heats)
    if num_flights and num_flights > 0 and total_non_axe > 0:
        target_flights = int(num_flights)
        heats_per_flight = math.ceil(total_non_axe / target_flights)
    else:
        heats_per_flight = 8
        target_flights = math.ceil(total_non_axe / heats_per_flight) if total_non_axe else 0

    # Pre-compute gear-sharing conflict pairs for adjacency penalty.
    gear_conflict_pairs: dict[int, set[int]] = {}
    try:
        from services.gear_sharing import build_gear_conflict_pairs
        gear_conflict_pairs = build_gear_conflict_pairs(tournament)
    except Exception:
        logger.warning('flight_builder: could not load gear conflict pairs', exc_info=True)

    # Build optimized heat order using multi-pass greedy algorithm.
    # Springboard opener and Hot Saw closer bonuses are baked into the scoring.
    ordered_heats = _optimize_heat_order(all_heats, heats_per_flight, N_OPTIMIZATION_PASSES,
                                         gear_conflict_pairs=gear_conflict_pairs)
    total_heats = len(ordered_heats)

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

    # Insert partnered axe heats with deterministic flight placement.
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
    """
    Assign partnered axe heats across flights in deterministic order.

    Heats are sorted by heat_number (which reflects prelim ranking) and
    distributed one-per-flight in flight_number order. This is deterministic
    and reproducible — no random shuffle.
    """
    if not flights or not axe_heats:
        return

    sorted_flights = sorted(flights, key=lambda f: f.flight_number)
    sorted_heats = sorted(axe_heats, key=lambda h: h.heat_number)

    for idx, heat in enumerate(sorted_heats):
        flight = sorted_flights[idx % len(sorted_flights)]
        heat.flight_id = flight.id
        heat.flight_position = _next_flight_position(flight.id)


def _next_flight_position(flight_id: int) -> int:
    """Return next 1-based display position within a flight."""
    max_pos = db.session.query(db.func.max(Heat.flight_position)).filter(
        Heat.flight_id == flight_id
    ).scalar()
    return int(max_pos or 0) + 1


def _optimize_heat_order(all_heats: list, heats_per_flight: int = 8,
                         n_passes: int = N_OPTIMIZATION_PASSES,
                         gear_conflict_pairs: dict[int, set[int]] | None = None) -> list:
    """
    Optimize heat order using a multi-pass greedy algorithm.

    Runs the greedy n_passes times, each time rotating the initial event order to
    explore different orderings. Keeps the run with the highest quality score.

    Within each pass:
    - Only the NEXT unplaced heat from each event is eligible (sequential guarantee).
    - Scoring uses per-event tiered spacing, springboard opener bonus, Hot Saw
      closer bonus, and event recency bonus to encourage flight-block variety.
    - Tie-breaking: prefer the event with the most remaining unplaced heats.

    Args:
        all_heats: List of heat data dicts with 'heat', 'event', 'competitors'
        heats_per_flight: Size of each flight block (used for opener/closer bonuses)
        n_passes: Number of independent greedy passes to run

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

    best_ordered: list = []
    best_score = float('-inf')

    actual_passes = min(n_passes, max(1, len(event_ids)))
    for pass_num in range(actual_passes):
        # Rotate event_ids to create different greedy starting conditions.
        rotated = event_ids[pass_num:] + event_ids[:pass_num]
        candidate = _single_pass_optimize(event_queues, rotated, heats_per_flight,
                                          gear_conflict_pairs=gear_conflict_pairs)
        score = _score_ordering(candidate, heats_per_flight,
                                gear_conflict_pairs=gear_conflict_pairs)
        if score > best_score:
            best_score = score
            best_ordered = candidate

    return best_ordered


def _single_pass_optimize(event_queues: dict, event_id_order: list,
                           heats_per_flight: int,
                           gear_conflict_pairs: dict[int, set[int]] | None = None) -> list:
    """
    Execute a single greedy pass through the event queues.

    At each step, the next unplaced heat from each event is scored and the
    highest-scoring candidate is selected. Tie-breaking prefers the event
    with the most remaining heats (encourages balanced distribution).
    """
    event_ptrs: dict[int, int] = {eid: 0 for eid in event_id_order}
    ordered: list = []
    competitor_last_heat: dict[int, int] = {}
    stand_type_last_position: dict[str, int] = {}
    # Track which flight block each event last appeared in (for recency bonus).
    event_last_block: dict[int, int] = {}

    while True:
        candidates = [
            (eid, event_queues[eid][event_ptrs[eid]])
            for eid in event_id_order
            if event_ptrs[eid] < len(event_queues[eid])
        ]
        if not candidates:
            break

        current_position = len(ordered)
        remaining_counts = {
            eid: len(event_queues[eid]) - event_ptrs[eid]
            for eid in event_id_order
        }

        # Score all candidates.
        scored = [
            (
                _calculate_heat_score(
                    hd['competitors'],
                    competitor_last_heat,
                    current_position,
                    hd['event'],
                    stand_type_last_position,
                    heats_per_flight,
                    event_last_block,
                    gear_conflict_pairs=gear_conflict_pairs,
                    previous_heat_comps=ordered[-1]['competitors'] if ordered else set(),
                ),
                remaining_counts[eid],   # tie-break: more remaining = preferred
                eid,
                hd,
            )
            for eid, hd in candidates
        ]

        best_score, _, best_eid, best_heat_data = max(scored, key=lambda x: (x[0], x[1]))

        # If every candidate is blocked by a stand conflict, re-score ignoring it.
        if best_score < 0:
            scored_nc = [
                (
                    _calculate_heat_score(
                        hd['competitors'],
                        competitor_last_heat,
                        current_position,
                        hd['event'],
                        None,  # disable stand conflict check
                        heats_per_flight,
                        event_last_block,
                    ),
                    remaining_counts[eid],
                    eid,
                    hd,
                )
                for eid, hd in candidates
            ]
            _, _, best_eid, best_heat_data = max(scored_nc, key=lambda x: (x[0], x[1]))

        ordered.append(best_heat_data)
        event_ptrs[best_eid] += 1

        pos = len(ordered) - 1
        for comp_id in best_heat_data['competitors']:
            competitor_last_heat[comp_id] = pos
        stand_type = getattr(best_heat_data['event'], 'stand_type', None)
        if stand_type:
            stand_type_last_position[stand_type] = pos
        event_id = best_heat_data['heat'].event_id
        current_block = pos // heats_per_flight if heats_per_flight > 0 else 0
        event_last_block[event_id] = current_block

    return ordered


def _score_ordering(ordered: list, heats_per_flight: int,
                    gear_conflict_pairs: dict[int, set[int]] | None = None) -> float:
    """
    Compute a quality score for a complete heat ordering. Higher is better.

    Used to compare multiple greedy passes and select the best result.
    Rewards adequate competitor spacing, penalizes spacing violations,
    and gives a small bonus for event variety within each flight block.
    """
    if not ordered:
        return 0.0

    competitor_last: dict[int, int] = {}
    event_blocks_seen: dict[tuple, bool] = {}  # (block, event_id) -> seen
    total = 0.0

    for pos, hd in enumerate(ordered):
        event = hd['event']
        min_sp, target_sp = _get_spacing(event)
        block = pos // heats_per_flight if heats_per_flight > 0 else 0

        for cid in hd['competitors']:
            if cid in competitor_last:
                spacing = pos - competitor_last[cid]
                if spacing < min_sp:
                    total -= (min_sp - spacing) * 50  # heavy violation penalty
                elif spacing >= target_sp:
                    total += 20                        # target spacing bonus
                else:
                    total += spacing * 2               # linear partial bonus
            competitor_last[cid] = pos

        # Variety bonus: first time this event appears in this flight block
        block_key = (block, hd['heat'].event_id)
        if block_key not in event_blocks_seen:
            total += 10
            event_blocks_seen[block_key] = True

    # Gear adjacency penalty across the full ordering.
    if gear_conflict_pairs:
        for pos in range(1, len(ordered)):
            prev_comps = ordered[pos - 1]['competitors']
            curr_comps = ordered[pos]['competitors']
            for cid in curr_comps:
                partner_ids = gear_conflict_pairs.get(cid)
                if partner_ids:
                    overlap = partner_ids & prev_comps
                    if overlap:
                        total -= 30 * len(overlap)

    return total


def _calculate_heat_score(competitors: set, competitor_last_heat: dict,
                           current_position: int, event: Event,
                           stand_type_last_position: dict | None,
                           heats_per_flight: int = 8,
                           event_last_block: dict | None = None,
                           gear_conflict_pairs: dict[int, set[int]] | None = None,
                           previous_heat_comps: set | None = None) -> float:
    """
    Calculate a score for placing a heat at the current position.

    Higher score = better placement. Components:
    - Stand conflict enforcement (cookie_stack / standing_block mutual exclusion) → -1 if violated
    - Per-event tiered spacing (Tier 1=springboard, Tier 2=saw, Tier 3=others)
    - Rebalanced formula: min_spacing × 5 + avg_spacing × 5
    - Springboard opener bonus: +500 when at the start of a flight block
    - Hot Saw closer bonus: +300 when at the end of a flight block
    - Event recency bonus: +30 when this event hasn't appeared yet in the current block

    Args:
        competitors: Set of competitor IDs in this heat
        competitor_last_heat: Dict of competitor_id -> last heat index
        current_position: Current position in the ordered list
        event: The event this heat belongs to
        stand_type_last_position: Dict of stand_type -> last position (None = disabled)
        heats_per_flight: Flight block size for positional bonuses
        event_last_block: Dict of event_id -> last block number appeared in

    Returns:
        Score (higher is better), or -1 if blocked by stand conflict
    """
    stand_type = getattr(event, 'stand_type', None)

    # Enforce stand type conflict: cookie_stack and standing_block share physical stands
    if stand_type and stand_type in _CONFLICTING_STANDS and stand_type_last_position is not None:
        conflict_type = _CONFLICTING_STANDS[stand_type]
        last_conflict = stand_type_last_position.get(conflict_type)
        if last_conflict is not None and (current_position - last_conflict) < _STAND_CONFLICT_GAP:
            return -1.0

    min_sp, target_sp = _get_spacing(event)

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

    # All competitors are new — great placement
    if competitor_count == 0:
        score = 1000.0
    elif min_spacing < min_sp:
        # Below minimum spacing — penalize but don't hard-reject
        penalty = (min_sp - min_spacing) * 100
        score = max(0.0, 50.0 - penalty)
    else:
        avg_spacing = total_spacing / competitor_count
        # Rebalanced formula (#13): equal weight to min and average spacing
        score = min_spacing * 5 + avg_spacing * 5
        if min_spacing >= target_sp:
            score += 50

    # Springboard opener bonus (#5): strongly prefer springboard at the start of every
    # flight block so each flight opens with a springboard cut (crowd favourite).
    if stand_type == 'springboard' and heats_per_flight > 0:
        if current_position % heats_per_flight == 0:
            score += 500

    # Hot Saw closer bonus (#7): Hot Saw is a dramatic crowd-pleaser — give it a bonus
    # when it would be placed as the last heat of a flight block.
    if stand_type == 'hot_saw' and heats_per_flight > 0:
        if (current_position + 1) % heats_per_flight == 0:
            score += 300

    # Event recency bonus (#11): encourage variety within each flight block by rewarding
    # placing an event that hasn't appeared yet in the current block.
    if event_last_block is not None and heats_per_flight > 0:
        current_block = current_position // heats_per_flight
        event_id = getattr(event, 'id', None)
        if event_id is not None:
            last_block = event_last_block.get(event_id)
            if last_block is None or last_block < current_block:
                score += 30

    # Gear adjacency penalty: penalize placing a heat immediately after one that
    # contains a gear-sharing partner.  This gives equipment time to be moved
    # between stands.  Soft penalty (-30 per conflict) — does not hard-block.
    if gear_conflict_pairs and previous_heat_comps:
        for comp_id in competitors:
            partner_ids = gear_conflict_pairs.get(comp_id)
            if partner_ids:
                overlap = partner_ids & previous_heat_comps
                if overlap:
                    score -= 30 * len(overlap)

    return score


def optimize_flight_for_ability(flight: Flight, event: Event):
    """
    Reorder heats within a flight to group by ability.

    For springboard events, competitors flagged springboard_slow_heat=True are
    consolidated into dedicated heats at the back of the flight block so that
    slow cutters do not dilute faster heats.  Heat assignments are rewritten in
    place; competitors and stand assignments are preserved — only which heat a
    competitor appears in changes.

    For non-springboard events the function is a no-op.  Predicted-time-based
    grouping (STRATHMARK) can be layered in here later.

    Args:
        flight: Flight whose heats will be reordered
        event: Event within that flight to optimise
    """
    if event.stand_type != 'springboard':
        return

    from models.competitor import ProCompetitor

    event_heats = (
        flight.heats.filter_by(event_id=event.id)
        .order_by(Heat.flight_position)
        .all()
    )
    if len(event_heats) <= 1:
        return

    max_per_heat = event.max_stands or 4

    # Collect all competitor IDs from these heats with their slow_heat flag.
    all_comp_ids: list[int] = []
    for heat in event_heats:
        all_comp_ids.extend(heat.get_competitors())

    if not all_comp_ids:
        return

    slow_flag: dict[int, bool] = {}
    for comp in ProCompetitor.query.filter(ProCompetitor.id.in_(all_comp_ids)).all():
        slow_flag[comp.id] = bool(getattr(comp, 'springboard_slow_heat', False))

    normal = [cid for cid in all_comp_ids if not slow_flag.get(cid)]
    slow = [cid for cid in all_comp_ids if slow_flag.get(cid)]

    # Rebuild: normal competitors fill the front heats, slow the back heats.
    reordered: list[list[int]] = []
    for i in range(0, len(normal), max_per_heat):
        reordered.append(normal[i:i + max_per_heat])
    for i in range(0, len(slow), max_per_heat):
        reordered.append(slow[i:i + max_per_heat])

    # If nothing changed there is nothing to write.
    flat_before = all_comp_ids
    flat_after = [cid for group in reordered for cid in group]
    if flat_before == flat_after:
        return

    # Write new compositions back to existing heat rows.
    for idx, heat in enumerate(event_heats):
        if idx < len(reordered):
            group = reordered[idx]
        else:
            group = []
        heat.set_competitors(group)
        for position, comp_id in enumerate(group, start=1):
            heat.set_stand_assignment(comp_id, position)
        heat.sync_assignments('pro')


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
            heat = Heat(
                event_id=axe_event.id,
                heat_number=100 + i,  # High number to indicate finals
                run_number=1,
                flight_id=flights[i].id
            )
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
    Validate that competitor spacing meets tier requirements.

    Returns:
        Dict with validation results and any violations
    """
    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).all()

    all_heats = []
    for flight in flights:
        flight_heats = flight.heats.order_by(Heat.flight_position).all()
        all_heats.extend(flight_heats)

    competitor_appearances = {}
    violations = []

    for i, heat in enumerate(all_heats):
        min_sp, _ = _get_spacing(heat.event)
        competitors = heat.get_competitors()
        for comp_id in competitors:
            if comp_id in competitor_appearances:
                last_appearance = competitor_appearances[comp_id]
                spacing = i - last_appearance
                if spacing < min_sp:
                    violations.append({
                        'competitor_id': comp_id,
                        'heat_1': last_appearance + 1,
                        'heat_2': i + 1,
                        'spacing': spacing,
                        'required': min_sp
                    })
            competitor_appearances[comp_id] = i

    return {
        'valid': len(violations) == 0,
        'total_heats': len(all_heats),
        'violations': violations,
        'violation_count': len(violations)
    }


def build_flight_audit_report(tournament: Tournament) -> dict:
    """
    Build a post-flight-construction audit report.

    Checks:
    1. Sequential heat order within each event (heats must appear in ascending
       heat_number order across the full show).
    2. Competitor spacing violations (actual gap vs tier minimum).
    3. Per-competitor spacing statistics (min, avg, max actual gaps).
    4. Event variety per flight (distinct events per flight block).
    5. Gear sharing adjacency conflicts (gear partners in back-to-back heats).

    Returns a dict suitable for display in the scheduling UI and for storage
    as a JSON audit record.
    """
    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).all()
    if not flights:
        return {'error': 'No flights built yet.'}

    # Build global ordered heat list across all flights in display order.
    all_heat_data = []
    for flight in flights:
        for heat in Heat.query.filter_by(flight_id=flight.id).order_by(Heat.flight_position).all():
            all_heat_data.append({
                'heat': heat,
                'event': heat.event,
                'flight_number': flight.flight_number,
                'flight_position': heat.flight_position,
                'competitors': list(heat.get_competitors()),
            })

    # 1. Sequential order check (#15)
    event_last_heat_num: dict[int, int] = {}
    event_last_flight_pos: dict[int, int] = {}
    sequential_violations: list[dict] = []
    for pos, hd in enumerate(all_heat_data):
        eid = hd['heat'].event_id
        hn = hd['heat'].heat_number
        if eid in event_last_heat_num and hn < event_last_heat_num[eid]:
            sequential_violations.append({
                'event': hd['event'].display_name if hd['event'] else str(eid),
                'heat_number': hn,
                'previous_heat_number': event_last_heat_num[eid],
                'global_position': pos,
                'previous_global_position': event_last_flight_pos[eid],
                'flight': hd['flight_number'],
            })
        event_last_heat_num[eid] = hn
        event_last_flight_pos[eid] = pos

    # 2 + 3. Competitor spacing audit (#16)
    competitor_last: dict[int, int] = {}
    competitor_spacings: dict[int, list[int]] = {}
    spacing_violations: list[dict] = []

    for pos, hd in enumerate(all_heat_data):
        event = hd['event']
        min_sp, _ = _get_spacing(event)
        for cid in hd['competitors']:
            if cid in competitor_last:
                spacing = pos - competitor_last[cid]
                competitor_spacings.setdefault(cid, []).append(spacing)
                if spacing < min_sp:
                    spacing_violations.append({
                        'competitor_id': cid,
                        'position_1': competitor_last[cid],
                        'position_2': pos,
                        'spacing': spacing,
                        'required': min_sp,
                        'event': event.display_name if event else '?',
                    })
            competitor_last[cid] = pos

    # Per-competitor stats
    competitor_stats: list[dict] = []
    for cid, spacings in competitor_spacings.items():
        competitor_stats.append({
            'competitor_id': cid,
            'appearances': len(spacings) + 1,
            'min_spacing': min(spacings),
            'avg_spacing': round(sum(spacings) / len(spacings), 1),
            'max_spacing': max(spacings),
        })
    all_spacings = [s for sl in competitor_spacings.values() for s in sl]
    avg_spacing_overall = round(sum(all_spacings) / len(all_spacings), 2) if all_spacings else 0

    # 4. Event variety per flight
    variety_report: list[dict] = []
    for flight in flights:
        flight_heats = [hd for hd in all_heat_data if hd['flight_number'] == flight.flight_number]
        distinct_events = len({hd['heat'].event_id for hd in flight_heats})
        variety_report.append({
            'flight_number': flight.flight_number,
            'heat_count': len(flight_heats),
            'distinct_events': distinct_events,
        })

    # 5. Gear sharing adjacency check (#18) — warn if gear partners appear in back-to-back heats
    gear_adjacency_warnings: list[dict] = []
    for i in range(len(all_heat_data) - 1):
        curr_comps = set(all_heat_data[i]['competitors'])
        next_comps = set(all_heat_data[i + 1]['competitors'])
        overlap = curr_comps & next_comps
        if not overlap:
            continue
        # Same competitor in consecutive heats — not gear sharing but is a spacing issue
        for cid in overlap:
            gear_adjacency_warnings.append({
                'competitor_id': cid,
                'position': i,
                'next_position': i + 1,
                'type': 'back_to_back',
            })

    return {
        'total_heats': len(all_heat_data),
        'total_flights': len(flights),
        'sequential_violations': sequential_violations,
        'passes_sequential': len(sequential_violations) == 0,
        'spacing_violations': spacing_violations,
        'spacing_violation_count': len(spacing_violations),
        'passes_spacing': len(spacing_violations) == 0,
        'avg_competitor_spacing': avg_spacing_overall,
        'competitor_stats': sorted(competitor_stats, key=lambda x: x['min_spacing']),
        'variety_per_flight': variety_report,
        'gear_adjacency_warnings': gear_adjacency_warnings,
    }


def integrate_college_spillover_into_flights(tournament: Tournament, college_event_ids: list[int] | None = None) -> dict:
    """
    Assign selected college spillover heats into existing Saturday pro flights.

    Chokerman's Race only contributes run 2 per Missoula rules.
    Chokerman heats are always placed at the end of the last flight to serve as
    the show climax — no other heats are inserted after them.
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

    # Build a map of competitor_id -> approximate global heat position from pro heats already
    # placed in flights. Used to enforce MIN_HEAT_SPACING for competitors who appear in both
    # pro heats and college overflow heats.
    competitor_last_position: dict[int, int] = {}
    global_position = 0
    for flight in flights:
        for heat in Heat.query.filter_by(flight_id=flight.id).order_by(Heat.flight_position).all():
            for comp_id in heat.get_competitors():
                competitor_last_position[int(comp_id)] = global_position
            global_position += 1

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
                # Always place at end of last flight (show climax — sealed position).
                heat.flight_id = last_flight.id
                heat.flight_position = _next_flight_position(last_flight.id)
                global_position += 1
            else:
                # Try flights in round-robin order, respecting MIN_HEAT_SPACING for
                # any competitor who also appears in pro heats.
                heat_comp_ids = [int(c) for c in heat.get_competitors()]
                placed = False
                for attempt in range(len(flights)):
                    candidate = flights[(flight_idx + attempt) % len(flights)]
                    candidate_pos = global_position
                    spacing_ok = all(
                        (candidate_pos - competitor_last_position[cid]) >= MIN_HEAT_SPACING
                        for cid in heat_comp_ids
                        if cid in competitor_last_position
                    )
                    if spacing_ok:
                        heat.flight_id = candidate.id
                        heat.flight_position = _next_flight_position(candidate.id)
                        for cid in heat_comp_ids:
                            competitor_last_position[cid] = candidate_pos
                        flight_idx = (flight_idx + attempt + 1) % len(flights)
                        placed = True
                        break

                if not placed:
                    # Fallback: place in original target regardless of spacing.
                    target = flights[flight_idx % len(flights)]
                    heat.flight_id = target.id
                    heat.flight_position = _next_flight_position(target.id)
                    for cid in heat_comp_ids:
                        competitor_last_position[cid] = global_position
                    flight_idx = (flight_idx + 1) % len(flights)

                global_position += 1
            integrated += 1

    db.session.flush()
    return {
        'integrated_heats': integrated,
        'events': per_event,
        'message': 'College spillover heats integrated into flights.',
    }


# ---------------------------------------------------------------------------
# FlightBuilder class — thin, testable wrapper around the module functions (#12)
# ---------------------------------------------------------------------------

class FlightBuilder:
    """Object-oriented façade for flight building operations.

    Wraps the module-level functions so callers can:
    - Inject a tournament once and call individual steps cleanly.
    - Subclass or mock for unit testing without touching the DB.

    Example::

        fb = FlightBuilder(tournament)
        fb.build(num_flights=5)
        result = fb.integrate_spillover([101, 102])
    """

    def __init__(self, tournament: Tournament):
        self.tournament = tournament

    def build(self, num_flights: int = None) -> int:
        """Build pro flights. Returns number of flights created."""
        logger.info('FlightBuilder.build tournament_id=%s num_flights=%s',
                    self.tournament.id, num_flights)
        return build_pro_flights(self.tournament, num_flights=num_flights)

    def integrate_spillover(self, saturday_college_event_ids: list[int]) -> dict:
        """Integrate college spillover heats into existing Saturday flights."""
        logger.info('FlightBuilder.integrate_spillover tournament_id=%s events=%s',
                    self.tournament.id, saturday_college_event_ids)
        return integrate_college_spillover_into_flights(
            self.tournament, saturday_college_event_ids
        )

    def spacing(self, event) -> tuple[int, int]:
        """Return (min_spacing, target_spacing) for the given event's stand type."""
        return _get_spacing(event)
