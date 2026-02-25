"""
Heat generation service using snake draft distribution.
Adapted from STRATHEX tournament_ui.py patterns.
"""
from database import db
from models import Event, Heat, EventResult
from models.competitor import CollegeCompetitor, ProCompetitor
import config
import math


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
    for heat_num, heat_competitors in enumerate(heats, start=1):
        heat = Heat(
            event_id=event.id,
            heat_number=heat_num,
            run_number=1
        )
        heat.set_competitors([c['id'] for c in heat_competitors])

        # Assign stands
        for i, comp in enumerate(heat_competitors):
            stand_num = i + 1
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
            for i, comp in enumerate(heat_competitors):
                # Swap positions
                swapped_pos = (max_per_heat - 1 - i) % max_per_heat + 1
                heat.set_stand_assignment(comp['id'], swapped_pos)

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
                    'gear_sharing': comp.get_gear_sharing() if hasattr(comp, 'get_gear_sharing') else {}
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
                    'gear_sharing': comp.get_gear_sharing()
                })

    db.session.flush()
    return competitors


def _generate_standard_heats(competitors: list, num_heats: int, max_per_heat: int, event: Event = None) -> list:
    """
    Generate heats using snake draft distribution.

    Snake draft ensures each heat has a mix of skill levels.
    """
    heats = [[] for _ in range(num_heats)]

    # Snake draft distribution
    direction = 1
    heat_idx = 0

    for comp in competitors:
        placed = False

        # First pass: look for a heat with capacity and no gear-sharing conflict.
        for _ in range(num_heats):
            if len(heats[heat_idx]) < max_per_heat and not _has_gear_sharing_conflict(comp, heats[heat_idx], event):
                heats[heat_idx].append(comp)
                placed = True
                break
            heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

        # Fallback: place despite conflict if every heat conflicts/full.
        if not placed:
            for _ in range(num_heats):
                if len(heats[heat_idx]) < max_per_heat:
                    heats[heat_idx].append(comp)
                    placed = True
                    break
                heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

        heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

    return heats


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
