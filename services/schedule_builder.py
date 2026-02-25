"""
Day schedule builder for Friday/Saturday show planning.
"""
from __future__ import annotations

import re
from models import Event, Tournament


COLLEGE_SATURDAY_PRIORITY = [
    ('Standing Block Speed', 'M'),
    ('Standing Block Hard Hit', 'M'),
    ('Standing Block Speed', 'F'),
    ('Standing Block Hard Hit', 'F'),
    ('Obstacle Pole', 'M'),
]


def build_day_schedule(
    tournament: Tournament,
    friday_pro_event_ids: list[int] | None = None,
    saturday_college_event_ids: list[int] | None = None
) -> dict:
    """Build Friday/Saturday schedule blocks from configured events and user options."""
    friday_pro_ids = set(friday_pro_event_ids or [])
    saturday_college_ids = set(saturday_college_event_ids or [])

    college_events = tournament.events.filter_by(event_type='college').all()
    pro_events = tournament.events.filter_by(event_type='pro').all()

    # Friday Night Feature defaults from requirements.
    friday_feature_names = {'Pro 1-Board', '3-Board Jigger'}

    friday_feature_pro = [e for e in pro_events if e.id in friday_pro_ids]
    friday_show_pro = [e for e in pro_events if e.id not in friday_pro_ids]

    # If user did not choose specific pro events for Friday feature, auto-include common feature events.
    if not friday_feature_pro:
        friday_feature_pro = [e for e in pro_events if e.name in friday_feature_names]
        friday_show_pro = [e for e in pro_events if e.id not in {e.id for e in friday_feature_pro}]

    saturday_college = [e for e in college_events if e.id in saturday_college_ids]
    friday_college = [e for e in college_events if e.id not in saturday_college_ids]

    friday_college, friday_feature_college = _extract_collegiate_feature_events(friday_college)

    friday_day = _build_friday_day_block(friday_college)
    friday_feature = _build_friday_feature_block(friday_feature_college, friday_feature_pro)
    saturday_show = _build_saturday_show_block(friday_show_pro, saturday_college)
    saturday_show = _add_mandatory_chokerman_run2(saturday_show, college_events)

    return {
        'friday_day': friday_day,
        'friday_feature': friday_feature,
        'saturday_show': saturday_show,
    }


def _extract_collegiate_feature_events(friday_college: list[Event]):
    """Move collegiate 1-board to Friday night feature when available."""
    feature_names = {'1-Board Springboard'}
    feature_events = [e for e in friday_college if e.name in feature_names]
    remaining = [e for e in friday_college if e.name not in feature_names]
    return remaining, feature_events


def _build_friday_day_block(events: list[Event]) -> list[dict]:
    ordered = sorted(events, key=_college_friday_sort_key)
    return _to_schedule_entries(ordered, start_slot=1)


def _build_friday_feature_block(college_events: list[Event], pro_events: list[Event]) -> list[dict]:
    ordered_college = sorted(college_events, key=_college_friday_sort_key)
    ordered_pro = sorted(pro_events, key=_pro_sort_key)
    ordered = ordered_college + ordered_pro
    return _to_schedule_entries(ordered, start_slot=1)


def _build_saturday_show_block(pro_events: list[Event], college_spillover: list[Event]) -> list[dict]:
    """Intermix Saturday college spillover events into pro show order."""
    ordered_pro = sorted(pro_events, key=_pro_sort_key)
    ordered_spillover = sorted(college_spillover, key=_spillover_sort_key)

    merged = []
    spillover_idx = 0
    for idx, event in enumerate(ordered_pro, start=1):
        merged.append(event)
        # Insert one spillover event every 3 pro events.
        if idx % 3 == 0 and spillover_idx < len(ordered_spillover):
            merged.append(ordered_spillover[spillover_idx])
            spillover_idx += 1

    # Append any remaining spillover events.
    if spillover_idx < len(ordered_spillover):
        merged.extend(ordered_spillover[spillover_idx:])

    return _to_schedule_entries(merged, start_slot=1)


def _to_schedule_entries(events: list[Event], start_slot: int = 1) -> list[dict]:
    entries = []
    slot = start_slot
    for event in events:
        entries.append({
            'slot': slot,
            'event_id': event.id,
            'label': event.display_name,
            'event_type': event.event_type,
            'stand_type': event.stand_type,
        })
        slot += 1
    return entries


def _college_friday_sort_key(event: Event):
    # OPEN events run first, birling always at the end of college day.
    is_birling = 1 if 'birling' in event.name.lower() else 0
    open_rank = 0 if event.is_open else 1
    event_rank = _college_name_rank(event.name)
    gender_rank = _gender_rank(event.gender)
    return (is_birling, open_rank, event_rank, gender_rank)


def _spillover_sort_key(event: Event):
    priority_lookup = {
        ('Standing Block Speed', 'M'): 1,
        ('Standing Block Hard Hit', 'M'): 2,
        ('Standing Block Speed', 'F'): 3,
        ('Standing Block Hard Hit', 'F'): 4,
        ('Obstacle Pole', 'M'): 5,
    }
    return (priority_lookup.get((event.name, event.gender), 999), _gender_rank(event.gender))


def _pro_sort_key(event: Event):
    return (_pro_name_rank(event.name), _gender_rank(event.gender))


def _college_name_rank(name: str) -> int:
    ordered = [
        'Axe Throw',
        'Peavey Log Roll',
        'Caber Toss',
        'Pulp Toss',
        'Underhand Hard Hit',
        'Underhand Speed',
        'Standing Block Hard Hit',
        'Standing Block Speed',
        'Single Buck',
        'Double Buck',
        'Jack & Jill Sawing',
        'Stock Saw',
        'Speed Climb',
        'Obstacle Pole',
        "Chokerman's Race",
        '1-Board Springboard',
        'Birling',
    ]
    return _lookup_rank(name, ordered)


def _pro_name_rank(name: str) -> int:
    ordered = [
        'Springboard',
        'Underhand',
        'Standing Block',
        'Stock Saw',
        'Hot Saw',
        'Single Buck',
        'Double Buck',
        'Jack & Jill Sawing',
        'Obstacle Pole',
        'Cookie Stack',
        'Pole Climb',
        'Partnered Axe Throw',
        'Pro 1-Board',
        '3-Board Jigger',
    ]
    return _lookup_rank(name, ordered)


def _lookup_rank(name: str, ordered: list[str]) -> int:
    target = _normalize_name(name)
    for idx, candidate in enumerate(ordered):
        if _normalize_name(candidate) == target:
            return idx
    return len(ordered) + 100


def _normalize_name(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (value or '').lower())


def _gender_rank(gender: str | None) -> int:
    if gender == 'M':
        return 0
    if gender == 'F':
        return 1
    return 2


def _add_mandatory_chokerman_run2(schedule_entries: list[dict], college_events: list[Event]) -> list[dict]:
    """Always include Chokerman's Race run 2 on Saturday when event is configured."""
    chokerman = next(
        (
            e for e in college_events
            if e.name == "Chokerman's Race" and e.event_type == 'college'
        ),
        None
    )
    if not chokerman:
        return schedule_entries

    updated = list(schedule_entries)
    updated.append({
        'slot': len(updated) + 1,
        'event_id': chokerman.id,
        'label': f"{chokerman.display_name} (Run 2)",
        'event_type': chokerman.event_type,
        'stand_type': chokerman.stand_type,
    })
    return updated
