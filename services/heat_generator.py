"""
Heat generation service using snake draft distribution.
Adapted from STRATHEX tournament_ui.py patterns.
"""
import logging
import math

from database import db
from models import Event, Heat, HeatAssignment, EventResult
from models.competitor import CollegeCompetitor, ProCompetitor
import config
from config import LIST_ONLY_EVENT_NAMES, event_rank_category as _rank_category_for_event
from services.gear_sharing import competitors_share_gear_for_event

logger = logging.getLogger(__name__)
# LIST_ONLY_EVENT_NAMES and _rank_category_for_event imported from config above.


def _sort_by_ability(competitors: list, event: Event) -> list:
    """
    Sort competitors by their ProEventRank before the snake draft.

    Ranked competitors (rank 1 = best) are placed first in ascending order.
    Competitors with no rank record sort to the end of the list so they are
    still distributed by snake draft among the unranked group.

    Falls back to the original list order when:
    - event is None or event_type is not 'pro'
    - the event has no ranked category
    - no ProEventRank rows exist for this tournament + category
    """
    if event is None or getattr(event, 'event_type', None) != 'pro':
        return competitors

    category = _rank_category_for_event(event)
    if category is None:
        return competitors

    # Local import to avoid circular imports (established project pattern).
    from models.pro_event_rank import ProEventRank

    rows = ProEventRank.query.filter_by(
        tournament_id=event.tournament_id,
        event_category=category,
    ).all()

    if not rows:
        return competitors  # No ranks set — fall back to registration order.

    rank_map = {row.competitor_id: row.rank for row in rows}
    # Secondary sort by name ensures unranked competitors (float('inf')) are
    # ordered alphabetically for reproducibility (#23).
    return sorted(
        competitors,
        key=lambda c: (rank_map.get(c['id'], float('inf')), c.get('name', '')),
    )


def generate_event_heats(event: Event) -> int:
    """
    Generate heats for an event using snake draft distribution.

    Snake draft ensures balanced skill distribution across heats:
    - Heat 1: A (best), F, K, P (worst)
    - Heat 2: B, G, J, O
    - Heat 3: C, H, I, N
    - etc.

    Args:
        event: Event to generate heats for

    Returns:
        Number of heats generated
    """
    logger.info('heat_generator: generate_event_heats event_id=%s name=%r type=%s',
                event.id, event.name, event.event_type)
    # Clear the per-tournament event cache so it refreshes each generate call.
    _get_tournament_events._cache = {}
    # Get competitors for this event
    competitors = _get_event_competitors(event)

    if not competitors:
        raise ValueError(f"No competitors entered for {event.display_name}")

    # OPEN/CLOSED-list events are tracked as signups only, without heats.
    if _is_list_only_event(event):
        _delete_event_heats(event.id)
        event.status = 'in_progress'
        db.session.flush()  # Caller is responsible for commit — preserves atomic transactions.
        return 0

    # Get stand configuration; event.max_stands is authoritative when set
    stand_config = config.STAND_CONFIGS.get(event.stand_type, {})
    max_per_heat = event.max_stands if event.max_stands is not None else stand_config.get('total', 4)

    # Calculate number of heats needed
    num_heats = math.ceil(len(competitors) / max_per_heat)

    # Clear existing heats
    _delete_event_heats(event.id)

    # Apply special constraints
    if event.stand_type == 'springboard':
        heats = _generate_springboard_heats(competitors, num_heats, max_per_heat, stand_config, event=event)
    elif event.stand_type in ['saw_hand']:
        heats = _generate_saw_heats(competitors, num_heats, max_per_heat, stand_config, event=event)
    else:
        heats = _generate_standard_heats(competitors, num_heats, max_per_heat, event=event)

    # Create Heat objects
    stand_numbers = _stand_numbers_for_event(event, max_per_heat, stand_config)
    created_heats = []
    for heat_num, heat_competitors in enumerate(heats, start=1):
        heat = Heat(
            event_id=event.id,
            heat_number=heat_num,
            run_number=1
        )
        heat.set_competitors([c['id'] for c in heat_competitors])

        # Assign stands
        for i, comp in enumerate(heat_competitors):
            stand_num = stand_numbers[i]
            heat.set_stand_assignment(comp['id'], stand_num)

        db.session.add(heat)
        created_heats.append(heat)

    # For dual-run events, create second run heats
    if event.requires_dual_runs:
        run2_stands = list(reversed(stand_numbers))
        for heat_num, heat_competitors in enumerate(heats, start=1):
            heat = Heat(
                event_id=event.id,
                heat_number=heat_num,
                run_number=2
            )
            heat.set_competitors([c['id'] for c in heat_competitors])

            # Swap stand assignments for run 2 (e.g., Course 1 <-> Course 2)
            for i, comp in enumerate(heat_competitors):
                heat.set_stand_assignment(comp['id'], run2_stands[i])

            db.session.add(heat)
            created_heats.append(heat)

    event.status = 'in_progress'
    db.session.flush()

    comp_type = event.event_type  # 'pro' or 'college'
    for heat in created_heats:
        heat.sync_assignments(comp_type)

    # Flush but do NOT commit — the calling route owns the transaction boundary and
    # will commit (or roll back) after all scheduling actions are complete.  This
    # prevents partial state if a later step in the same request fails.
    db.session.flush()

    return num_heats


def _get_event_competitors(event: Event) -> list:
    """Get list of competitors entered in this event with their info."""
    competitors = []

    # Get from event results (if already assigned)
    for result in event.results.all():
        if event.event_type == 'college':
            comp = CollegeCompetitor.query.get(result.competitor_id)
        else:
            comp = ProCompetitor.query.get(result.competitor_id)

        if comp and comp.status == 'active' and _competitor_entered_event(event, comp.get_events_entered()):
            competitors.append({
                'id': comp.id,
                'name': comp.name,
                'gender': comp.gender,
                'is_left_handed': getattr(comp, 'is_left_handed_springboard', False),
                'gear_sharing': comp.get_gear_sharing() if hasattr(comp, 'get_gear_sharing') else {}
            })

    # If no results yet, get from competitor event entries
    if not competitors:
        if event.event_type == 'college':
            all_comps = CollegeCompetitor.query.filter_by(
                tournament_id=event.tournament_id,
                status='active'
            ).all()

            # Filter by gender if gendered event
            if event.gender:
                all_comps = [c for c in all_comps if c.gender == event.gender]

            for comp in all_comps:
                if not _competitor_entered_event(event, comp.get_events_entered()):
                    continue
                # Create result entry for tracking
                result = EventResult(
                    event_id=event.id,
                    competitor_id=comp.id,
                    competitor_type='college',
                    competitor_name=comp.name
                )
                db.session.add(result)

                competitors.append({
                    'id': comp.id,
                    'name': comp.name,
                    'gender': comp.gender,
                    'is_left_handed': False,
                    'gear_sharing': comp.get_gear_sharing() if hasattr(comp, 'get_gear_sharing') else {},
                    'partner_name': _get_partner_name_for_event(comp, event)
                })
        else:
            all_comps = ProCompetitor.query.filter_by(
                tournament_id=event.tournament_id,
                status='active'
            ).all()

            # Filter by gender if gendered event
            if event.gender:
                all_comps = [c for c in all_comps if c.gender == event.gender]

            for comp in all_comps:
                if not _competitor_entered_event(event, comp.get_events_entered()):
                    continue
                result = EventResult(
                    event_id=event.id,
                    competitor_id=comp.id,
                    competitor_type='pro',
                    competitor_name=comp.name
                )
                db.session.add(result)

                competitors.append({
                    'id': comp.id,
                    'name': comp.name,
                    'gender': comp.gender,
                    'is_left_handed': comp.is_left_handed_springboard,
                    'is_slow_springboard': bool(getattr(comp, 'springboard_slow_heat', False)),
                    'gear_sharing': comp.get_gear_sharing(),
                    'partner_name': _get_partner_name_for_event(comp, event)
                })

    db.session.flush()
    return competitors


def _generate_standard_heats(competitors: list, num_heats: int, max_per_heat: int, event: Event = None) -> list:
    """
    Generate heats using snake draft distribution.

    Snake draft ensures each heat has a mix of skill levels.
    """
    heats = [[] for _ in range(num_heats)]
    competitors = _sort_by_ability(competitors, event)
    units = _build_partner_units(competitors, event)
    # Re-sort partner units by composite rank so paired competitors enter the
    # snake draft in the right ability order (#22).
    units = _sort_units_by_ability(units, event)

    # Snake draft distribution
    direction = 1
    heat_idx = 0

    for unit in units:
        placed = False

        # First pass: look for a heat with capacity and no gear-sharing conflict.
        for _ in range(num_heats):
            if (
                (len(heats[heat_idx]) + len(unit)) <= max_per_heat and
                not any(_has_gear_sharing_conflict(comp, heats[heat_idx], event) for comp in unit)
            ):
                heats[heat_idx].extend(unit)
                placed = True
                break
            heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

        # Fallback: place despite conflict if every heat conflicts/full.
        if not placed:
            for _ in range(num_heats):
                if (len(heats[heat_idx]) + len(unit)) <= max_per_heat:
                    heats[heat_idx].extend(unit)
                    placed = True
                    break
                heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

        heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

    return heats


def _build_partner_units(competitors: list, event: Event) -> list:
    """Build assignment units; partnered events keep recognized pairs together."""
    if not event or not event.is_partnered:
        return [[c] for c in competitors]

    by_name = {_norm_name(c.get('name')): c for c in competitors}
    used = set()
    units = []

    for comp in competitors:
        comp_id = comp['id']
        if comp_id in used:
            continue

        partner_name = _norm_name(comp.get('partner_name'))
        partner = by_name.get(partner_name) if partner_name else None

        if partner and partner['id'] not in used:
            # Pair if either side references the other.
            partner_ref = _norm_name(partner.get('partner_name'))
            if partner_ref == _norm_name(comp.get('name')) or partner_name == _norm_name(partner.get('name')):
                units.append([comp, partner])
                used.add(comp_id)
                used.add(partner['id'])
                continue

        units.append([comp])
        used.add(comp_id)

    return units


def _sort_units_by_ability(units: list, event: Event) -> list:
    """
    Sort partner units by composite ability rank for the snake draft (#22).

    A unit's rank is the minimum rank of its members (best member drives position).
    Unranked units sort after all ranked units, with alphabetical secondary sort.
    Falls back to the input order when no ranks are configured.
    """
    if event is None or getattr(event, 'event_type', None) != 'pro':
        return units

    category = _rank_category_for_event(event)
    if category is None:
        return units

    from models.pro_event_rank import ProEventRank

    rows = ProEventRank.query.filter_by(
        tournament_id=event.tournament_id,
        event_category=category,
    ).all()

    if not rows:
        return units

    rank_map = {row.competitor_id: row.rank for row in rows}
    return sorted(
        units,
        key=lambda unit: (
            min(rank_map.get(c['id'], float('inf')) for c in unit),
            min(c.get('name', '') for c in unit),
        ),
    )


def _norm_name(value) -> str:
    return str(value or '').strip().lower()


def _get_partner_name_for_event(competitor, event: Event) -> str:
    """Get competitor's partner name for this event, if provided."""
    if not hasattr(competitor, 'get_partners'):
        return ''
    partners = competitor.get_partners()
    if not isinstance(partners, dict):
        return ''

    candidates = [
        str(event.id),
        event.name,
        event.display_name,
        event.name.lower(),
        event.display_name.lower()
    ]
    for key in candidates:
        if key in partners and str(partners.get(key)).strip():
            return str(partners.get(key)).strip()
    return ''


def _generate_springboard_heats(competitors: list, num_heats: int,
                                 max_per_heat: int, stand_config: dict, event: Event = None) -> list:
    """
    Generate springboard heats with left-handed cutter grouping.

    Left-handed cutters need to be grouped into the same heat.
    """
    heats = [[] for _ in range(num_heats)]

    # Dedicated springboard buckets:
    # - Left-handed cutters must stay together in one heat.
    # - Slow-heat cutters should be grouped into the dedicated slow heat.
    left_handed = [c for c in competitors if c.get('is_left_handed', False)]
    slow_heat = [c for c in competitors if c.get('is_slow_springboard', False)]

    left_heat_idx = 0 if left_handed else None
    slow_heat_idx = (num_heats - 1) if slow_heat else None
    if left_heat_idx is not None and slow_heat_idx is not None and num_heats == 1:
        slow_heat_idx = left_heat_idx

    assigned_ids = set()

    def _place_group(group: list, preferred_idx: int | None):
        if not group:
            return
        remaining = [g for g in group if g['id'] not in assigned_ids]
        if not remaining:
            return

        # Prefer one dedicated heat; overflow stays grouped into adjacent heats.
        idx = preferred_idx if preferred_idx is not None else 0
        while remaining:
            candidate = None
            for probe in list(range(idx, num_heats)) + list(range(0, idx)):
                if len(heats[probe]) < max_per_heat:
                    candidate = probe
                    break
            if candidate is None:
                break
            idx = candidate
            capacity = max_per_heat - len(heats[idx])
            take = remaining[:max(0, capacity)]
            heats[idx].extend(take)
            for comp in take:
                assigned_ids.add(comp['id'])
            remaining = remaining[len(take):]
            idx += 1

    _place_group(left_handed, left_heat_idx)
    _place_group(slow_heat, slow_heat_idx)

    # Fill the remaining cutters with snake draft while respecting capacity.
    # Sort by ability rank before the snake draft so each heat gets a skill mix.
    remaining = _sort_by_ability(
        [c for c in competitors if c['id'] not in assigned_ids], event
    )
    if not remaining:
        return heats

    heat_idx = 0
    direction = 1
    for comp in remaining:
        attempts = 0
        while attempts < num_heats and len(heats[heat_idx]) >= max_per_heat:
            heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)
            attempts += 1
        if attempts >= num_heats:
            break
        heats[heat_idx].append(comp)
        heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

    return heats


def _generate_saw_heats(competitors: list, num_heats: int,
                        max_per_heat: int, stand_config: dict, event: Event = None) -> list:
    """
    Generate saw heats respecting stand group constraints.

    Saw stands are in groups of 4. One group runs while the other sets up.
    """
    # Standard snake draft, but limit to 4 per heat for saw events
    actual_max = min(max_per_heat, 4)  # Saw groups are 4 each
    num_heats = math.ceil(len(competitors) / actual_max)

    return _generate_standard_heats(competitors, num_heats, actual_max, event=event)


def _advance_snake_index(heat_idx: int, direction: int, num_heats: int):
    """Advance heat index in snake-draft pattern."""
    heat_idx += direction
    if heat_idx >= num_heats:
        direction = -1
        heat_idx = num_heats - 1
    elif heat_idx < 0:
        direction = 1
        heat_idx = 0
    return heat_idx, direction


def _normalize_name(value: str) -> str:
    return ''.join(ch for ch in str(value or '').lower() if ch.isalnum())


def _competitor_entered_event(event: Event, entered_events: list) -> bool:
    entered = entered_events if isinstance(entered_events, list) else []
    target_id = str(event.id)
    target_name = _normalize_name(event.name)
    target_display_name = _normalize_name(event.display_name)
    aliases = {target_name, target_display_name}

    if event.event_type == 'pro':
        if target_name == 'springboard':
            aliases.update({'springboardl', 'springboardr'})
        elif target_name in {'pro1board', '1boardspringboard'}:
            aliases.update({'intermediate1boardspringboard', 'pro1board', '1boardspringboard'})
        elif target_name == 'jackjillsawing':
            aliases.update({'jackjill', 'jackandjill'})
        elif target_name in {'poleclimb', 'speedclimb'}:
            aliases.update({'poleclimb', 'speedclimb'})
        elif target_name == 'partneredaxethrow':
            aliases.update({'partneredaxethrow', 'axethrow'})

    for raw in entered:
        value = str(raw).strip()
        if not value:
            continue
        if value == target_id:
            return True
        normalized = _normalize_name(value)
        if normalized in aliases:
            return True
    return False


def _is_list_only_event(event: Event) -> bool:
    return event.event_type == 'college' and _normalize_name(event.name) in LIST_ONLY_EVENT_NAMES


def _stand_numbers_for_event(event: Event, max_per_heat: int, stand_config: dict) -> list[int]:
    if event.event_type == 'college' and _normalize_name(event.name) == _normalize_name('Stock Saw'):
        # Missoula rule: college stock saw runs only on saw stands 7 and 8.
        return [7, 8][:max_per_heat]

    specific = stand_config.get('specific_stands')
    if specific:
        return list(specific)[:max_per_heat]

    return list(range(1, max_per_heat + 1))


def _get_tournament_events(event: Event) -> list:
    """Return all events for the same tournament (cached per generate call)."""
    if not hasattr(_get_tournament_events, '_cache'):
        _get_tournament_events._cache = {}
    tid = event.tournament_id
    if tid not in _get_tournament_events._cache:
        _get_tournament_events._cache[tid] = Event.query.filter_by(tournament_id=tid).all()
    return _get_tournament_events._cache[tid]


def _has_gear_sharing_conflict(comp: dict, heat_competitors: list, event: Event) -> bool:
    """Return True if comp conflicts with anyone already in heat for this event."""
    for other in heat_competitors:
        if _competitors_share_gear_for_event(comp, other, event):
            return True
    return False


def _competitors_share_gear_for_event(comp1: dict, comp2: dict, event: Event) -> bool:
    """Check event-specific gear-sharing conflict between two competitors.

    Passes all tournament events to enable cascade checking across gear
    families (e.g. sharing an axe for Springboard also conflicts in Underhand).
    """
    return competitors_share_gear_for_event(
        str(comp1.get('name', '')).strip(),
        comp1.get('gear_sharing', {}) or {},
        str(comp2.get('name', '')).strip(),
        comp2.get('gear_sharing', {}) or {},
        event,
        all_events=_get_tournament_events(event),
    )


def _delete_event_heats(event_id: int) -> None:
    """Delete all heats for an event, clearing HeatAssignment rows first to satisfy FK constraints."""
    heat_ids = [h.id for h in Heat.query.filter_by(event_id=event_id).with_entities(Heat.id).all()]
    if heat_ids:
        HeatAssignment.query.filter(HeatAssignment.heat_id.in_(heat_ids)).delete(synchronize_session=False)
    Heat.query.filter_by(event_id=event_id).delete(synchronize_session=False)


def check_gear_sharing_conflicts(heats: list) -> list:
    """
    Check for gear sharing conflicts within heats.

    Returns list of conflicts found.
    """
    conflicts = []

    for heat_num, heat in enumerate(heats, start=1):
        for i, comp1 in enumerate(heat):
            for comp2 in heat[i+1:]:
                if competitors_share_gear_for_event(
                    str(comp1.get('name', '')),
                    comp1.get('gear_sharing', {}) or {},
                    str(comp2.get('name', '')),
                    comp2.get('gear_sharing', {}) or {},
                    None,
                ):
                    conflicts.append({
                        'heat': heat_num,
                        'competitor1': comp1['name'],
                        'competitor2': comp2['name'],
                        'type': 'gear_sharing'
                    })

    return conflicts
