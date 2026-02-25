"""
Heat generation service using snake draft distribution.
Adapted from STRATHEX tournament_ui.py patterns.
"""
from database import db
from models import Event, Heat, EventResult
from models.competitor import CollegeCompetitor, ProCompetitor
import config
import math

LIST_ONLY_EVENT_NAMES = {
    'axethrow',
    'peaveylogroll',
    'cabertoss',
    'pulptoss',
}


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
    # Get competitors for this event
    competitors = _get_event_competitors(event)

    if not competitors:
        raise ValueError(f"No competitors entered for {event.display_name}")

    # OPEN/CLOSED-list events are tracked as signups only, without heats.
    if _is_list_only_event(event):
        Heat.query.filter_by(event_id=event.id).delete()
        event.status = 'in_progress'
        db.session.commit()
        return 0

    # Get stand configuration
    stand_config = config.STAND_CONFIGS.get(event.stand_type, {})
    max_per_heat = stand_config.get('total', 4)

    # Calculate number of heats needed
    num_heats = math.ceil(len(competitors) / max_per_heat)

    # Clear existing heats
    Heat.query.filter_by(event_id=event.id).delete()

    # Apply special constraints
    if event.stand_type == 'springboard':
        heats = _generate_springboard_heats(competitors, num_heats, max_per_heat, stand_config, event=event)
    elif event.stand_type in ['saw_hand']:
        heats = _generate_saw_heats(competitors, num_heats, max_per_heat, stand_config, event=event)
    else:
        heats = _generate_standard_heats(competitors, num_heats, max_per_heat, event=event)

    # Create Heat objects
    stand_numbers = _stand_numbers_for_event(event, max_per_heat, stand_config)
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

    # For dual-run events, create second run heats
    if event.requires_dual_runs:
        for heat_num, heat_competitors in enumerate(heats, start=1):
            heat = Heat(
                event_id=event.id,
                heat_number=heat_num,
                run_number=2
            )
            heat.set_competitors([c['id'] for c in heat_competitors])

            # Swap stand assignments for run 2 (e.g., Course 1 <-> Course 2)
            run2_stands = list(reversed(stand_numbers))
            for i, comp in enumerate(heat_competitors):
                heat.set_stand_assignment(comp['id'], run2_stands[i])

            db.session.add(heat)

    event.status = 'in_progress'
    db.session.commit()

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

        if comp and comp.status == 'active':
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
    units = _build_partner_units(competitors, event)

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

    Left-handed cutters need to be on the same dummy and spread across heats.
    """
    # Separate left-handed and right-handed cutters
    left_handed = [c for c in competitors if c.get('is_left_handed', False)]
    right_handed = [c for c in competitors if not c.get('is_left_handed', False)]

    # If no left-handed, use standard distribution
    if not left_handed:
        return _generate_standard_heats(competitors, num_heats, max_per_heat, event=event)

    # Create heats ensuring left-handed are spread out
    heats = [[] for _ in range(num_heats)]

    # Distribute left-handed first (one per heat if possible)
    for i, comp in enumerate(left_handed):
        heat_idx = i % num_heats
        heats[heat_idx].append(comp)

    # Then distribute right-handed using snake draft
    remaining_spots = [[max_per_heat - len(h)] for h in heats]
    heat_idx = 0
    direction = 1

    for comp in right_handed:
        # Find next heat with space
        while heats[heat_idx] and len(heats[heat_idx]) >= max_per_heat:
            heat_idx += direction
            heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

        heats[heat_idx].append(comp)

        # Move to next heat
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


def _has_gear_sharing_conflict(comp: dict, heat_competitors: list, event: Event) -> bool:
    """Return True if comp conflicts with anyone already in heat for this event."""
    for other in heat_competitors:
        if _competitors_share_gear_for_event(comp, other, event):
            return True
    return False


def _competitors_share_gear_for_event(comp1: dict, comp2: dict, event: Event) -> bool:
    """Check event-specific gear-sharing conflict between two competitors."""
    sharing1 = comp1.get('gear_sharing', {}) or {}
    sharing2 = comp2.get('gear_sharing', {}) or {}
    name1 = str(comp1.get('name', '')).strip().lower()
    name2 = str(comp2.get('name', '')).strip().lower()

    for key1, value1 in sharing1.items():
        if not _gear_key_matches_event(key1, event):
            continue

        value1_text = str(value1).strip()
        if not value1_text:
            continue

        # Partner-name style rules.
        if value1_text.lower() == name2:
            return True

        # Group-token style rules from team-level gear notes.
        if value1_text.startswith('group:'):
            for key2, value2 in sharing2.items():
                if _gear_key_matches_event(key2, event) and str(value2).strip() == value1_text:
                    return True

    # Symmetric check for partner-name rules set on comp2 only.
    for key2, value2 in sharing2.items():
        if _gear_key_matches_event(key2, event) and str(value2).strip().lower() == name1:
            return True

    return False


def _gear_key_matches_event(key: str, event: Event) -> bool:
    """Match a gear-sharing key against an event."""
    key = str(key).strip().lower()
    event_name = (event.display_name if event else '').lower()

    if not event:
        return False
    if key == str(event.id):
        return True
    if key.startswith('category:'):
        category = key.split(':', 1)[1]
        if category == 'crosscut':
            return event.stand_type == 'saw_hand' or any(token in event_name for token in ['buck', 'saw', 'crosscut'])
        if category == 'chainsaw':
            return any(token in event_name for token in ['stock saw', 'power saw', 'hot saw'])
        return False

    return key in event_name


def check_gear_sharing_conflicts(heats: list) -> list:
    """
    Check for gear sharing conflicts within heats.

    Returns list of conflicts found.
    """
    conflicts = []

    for heat_num, heat in enumerate(heats, start=1):
        for i, comp1 in enumerate(heat):
            for comp2 in heat[i+1:]:
                # Check if either is sharing gear with the other
                sharing1 = comp1.get('gear_sharing', {})
                sharing2 = comp2.get('gear_sharing', {})

                for event_id, partner in sharing1.items():
                    if partner == comp2.get('name'):
                        conflicts.append({
                            'heat': heat_num,
                            'competitor1': comp1['name'],
                            'competitor2': comp2['name'],
                            'type': 'gear_sharing'
                        })

    return conflicts
