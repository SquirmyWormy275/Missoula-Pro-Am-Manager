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
        heats = _generate_springboard_heats(competitors, num_heats, max_per_heat, stand_config)
    elif event.stand_type in ['saw_hand']:
        heats = _generate_saw_heats(competitors, num_heats, max_per_heat, stand_config)
    else:
        heats = _generate_standard_heats(competitors, num_heats, max_per_heat)

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
                    'gear_sharing': {}
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


def _generate_standard_heats(competitors: list, num_heats: int, max_per_heat: int) -> list:
    """
    Generate heats using snake draft distribution.

    Snake draft ensures each heat has a mix of skill levels.
    """
    heats = [[] for _ in range(num_heats)]

    # Snake draft distribution
    direction = 1
    heat_idx = 0

    for comp in competitors:
        heats[heat_idx].append(comp)

        # Move to next heat (snake pattern)
        heat_idx += direction

        if heat_idx >= num_heats:
            direction = -1
            heat_idx = num_heats - 1
        elif heat_idx < 0:
            direction = 1
            heat_idx = 0

    return heats


def _generate_springboard_heats(competitors: list, num_heats: int,
                                 max_per_heat: int, stand_config: dict) -> list:
    """
    Generate springboard heats with left-handed cutter grouping.

    Left-handed cutters need to be on the same dummy and spread across heats.
    """
    # Separate left-handed and right-handed cutters
    left_handed = [c for c in competitors if c.get('is_left_handed', False)]
    right_handed = [c for c in competitors if not c.get('is_left_handed', False)]

    # If no left-handed, use standard distribution
    if not left_handed:
        return _generate_standard_heats(competitors, num_heats, max_per_heat)

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
            if heat_idx >= num_heats:
                direction = -1
                heat_idx = num_heats - 1
            elif heat_idx < 0:
                direction = 1
                heat_idx = 0

        heats[heat_idx].append(comp)

        # Move to next heat
        heat_idx += direction
        if heat_idx >= num_heats:
            direction = -1
            heat_idx = num_heats - 1
        elif heat_idx < 0:
            direction = 1
            heat_idx = 0

    return heats


def _generate_saw_heats(competitors: list, num_heats: int,
                        max_per_heat: int, stand_config: dict) -> list:
    """
    Generate saw heats respecting stand group constraints.

    Saw stands are in groups of 4. One group runs while the other sets up.
    """
    # Standard snake draft, but limit to 4 per heat for saw events
    actual_max = min(max_per_heat, 4)  # Saw groups are 4 each
    num_heats = math.ceil(len(competitors) / actual_max)

    return _generate_standard_heats(competitors, num_heats, actual_max)


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
