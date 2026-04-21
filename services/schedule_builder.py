"""
Day schedule builder for Friday/Saturday show planning.
"""
from __future__ import annotations

import json
import os
import re

import config
from config import DAY_SPLIT_EVENT_NAMES
from models import Event, Flight, Tournament


def _load_college_saturday_priority() -> list[tuple[str, str]]:
    path = config.get_config().EVENT_ORDER_CONFIG_PATH
    if path and os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as fh:
                payload = json.load(fh)
            raw = payload.get('college_saturday_priority', [])
            parsed = []
            for row in raw:
                if not isinstance(row, list | tuple) or len(row) != 2:
                    continue
                parsed.append((str(row[0]), str(row[1])))
            if parsed:
                return parsed
        except Exception:
            pass
    return list(config.COLLEGE_SATURDAY_PRIORITY_DEFAULT)


COLLEGE_SATURDAY_PRIORITY = _load_college_saturday_priority()


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

    # Check for custom ordering in schedule_config
    sched_cfg = tournament.get_schedule_config()
    friday_custom = sched_cfg.get('friday_event_order')
    saturday_custom = sched_cfg.get('saturday_event_order')

    friday_day = _build_friday_day_block(friday_college, custom_order=friday_custom)
    friday_feature = _build_friday_feature_block(friday_feature_college, friday_feature_pro)
    saturday_show, saturday_source = _build_saturday_show_block(
        tournament, friday_show_pro, saturday_college, custom_order=saturday_custom
    )
    saturday_show = _add_mandatory_day_split_run2(saturday_show, college_events)

    return {
        'friday_day': friday_day,
        'friday_feature': friday_feature,
        'saturday_show': saturday_show,
        'saturday_source': saturday_source,
    }


def _extract_collegiate_feature_events(friday_college: list[Event]):
    """Move collegiate 1-board to Friday night feature when available."""
    feature_names = {'1-Board Springboard'}
    feature_events = [e for e in friday_college if e.name in feature_names]
    remaining = [e for e in friday_college if e.name not in feature_names]
    return remaining, feature_events


def _build_friday_day_block(events: list[Event], custom_order: list[int] | None = None) -> list[dict]:
    if custom_order:
        ordered = _apply_custom_order(events, custom_order)
    else:
        ordered = sorted(events, key=_college_friday_sort_key)
    return _to_schedule_entries(ordered, start_slot=1)


def _build_friday_feature_block(college_events: list[Event], pro_events: list[Event]) -> list[dict]:
    ordered_college = sorted(college_events, key=_college_friday_sort_key)
    ordered_pro = sorted(pro_events, key=_pro_sort_key)
    hot_saw_first = [e for e in ordered_pro if _normalize_name(e.name) == _normalize_name('Hot Saw')]
    other_pro = [e for e in ordered_pro if _normalize_name(e.name) != _normalize_name('Hot Saw')]
    ordered = _apply_friday_springboard_ordering(hot_saw_first + ordered_college + other_pro)
    return _to_schedule_entries(ordered, start_slot=1)


def _apply_friday_springboard_ordering(events: list[Event]) -> list[Event]:
    """Apply Missoula springboard sequencing rules for Friday blocks."""
    if not events:
        return events

    springboard = _normalize_name('Springboard')
    intermediate = _normalize_name('Pro 1-Board')
    college_one_board = _normalize_name('1-Board Springboard')
    jigger = _normalize_name('3-Board Jigger')

    normalized = {_normalize_name(event.name) for event in events}
    if not normalized.intersection({springboard, intermediate, jigger}):
        return events

    sequence = ['1-Board Springboard', 'Pro 1-Board']
    if springboard in normalized:
        sequence = ['Springboard', 'Pro 1-Board', '1-Board Springboard']

    remaining = list(events)
    ordered_sequence = []
    for target_name in sequence:
        target_norm = _normalize_name(target_name)
        for idx, event in enumerate(remaining):
            if _normalize_name(event.name) == target_norm:
                ordered_sequence.append(event)
                remaining.pop(idx)
                break

    return remaining + ordered_sequence


def _build_saturday_show_block(
    tournament: Tournament,
    pro_events: list[Event],
    college_spillover: list[Event],
    custom_order: list[int] | None = None,
) -> tuple[list[dict], str]:
    """Build Saturday show from pro flights when available; fallback to event order."""
    allowed_event_ids = {event.id for event in pro_events}
    allowed_event_ids.update(event.id for event in college_spillover)
    # Include all day-split events so their Run 2 can appear on Saturday
    for e in tournament.events.filter_by(event_type='college').all():
        if e.name in DAY_SPLIT_EVENT_NAMES:
            allowed_event_ids.add(e.id)

    flight_entries = _build_saturday_from_flights(tournament, allowed_event_ids)
    if flight_entries:
        return _append_college_spillover(flight_entries, college_spillover), 'flights'

    fallback_entries = _build_saturday_from_event_order(pro_events, college_spillover, custom_order=custom_order)
    return fallback_entries, 'events'


def _build_saturday_from_flights(tournament: Tournament, allowed_event_ids: set[int]) -> list[dict]:
    """Flatten built flights/heats into schedule slots."""
    flights = Flight.query.filter_by(tournament_id=tournament.id).order_by(Flight.flight_number).all()
    if not flights:
        return []

    entries = []
    slot = 1
    for flight in flights:
        heats = flight.get_heats_ordered()
        for heat in heats:
            event = heat.event
            if not event:
                continue
            if allowed_event_ids and event.id not in allowed_event_ids:
                continue
            run_suffix = f' Run {heat.run_number}' if event.requires_dual_runs else ''
            entries.append({
                'slot': slot,
                'event_id': event.id,
                'label': f'Flight {flight.flight_number}: {event.display_name} - Heat {heat.heat_number}{run_suffix}',
                'event_type': event.event_type,
                'stand_type': event.stand_type,
                'flight_number': flight.flight_number,
                'heat_id': heat.id,
            })
            slot += 1

    return entries


def _append_college_spillover(existing_entries: list[dict], college_spillover: list[Event]) -> list[dict]:
    """Append selected college spillover events after flight-based schedule."""
    entries = list(existing_entries)
    existing_ids = {entry.get('event_id') for entry in entries}
    slot = len(entries) + 1
    for event in sorted(college_spillover, key=_spillover_sort_key):
        if event.id in existing_ids:
            continue
        entries.append({
            'slot': slot,
            'event_id': event.id,
            'label': f'{event.display_name} (College Spillover)',
            'event_type': event.event_type,
            'stand_type': event.stand_type,
        })
        slot += 1
    return entries


def _build_saturday_from_event_order(
    pro_events: list[Event],
    college_spillover: list[Event],
    custom_order: list[int] | None = None,
) -> list[dict]:
    """Intermix Saturday college spillover events into pro show order."""
    if custom_order:
        all_events = pro_events + college_spillover
        return _to_schedule_entries(_apply_custom_order(all_events, custom_order), start_slot=1)
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
    # OPEN events run first.
    # Chokerman's Race Run 1 goes at end of day, BEFORE Birling.
    # Birling is always the absolute last event on Friday.
    is_birling = 2 if 'birling' in event.name.lower() else 0
    is_chokerman = 1 if "chokerman" in event.name.lower() else 0
    end_of_day = max(is_birling, is_chokerman)
    open_rank = 0 if event.is_open else 1
    event_rank = _college_name_rank(event.name)
    gender_rank = _gender_rank(event.gender)
    return (end_of_day, open_rank, event_rank, gender_rank)


def _spillover_sort_key(event: Event):
    priority_lookup = {
        ('Standing Block Speed', 'M'): 1,
        ('Standing Block Hard Hit', 'M'): 2,
        ('Standing Block Speed', 'F'): 3,
        ('Standing Block Hard Hit', 'F'): 4,
        ('Obstacle Pole', 'M'): 5,
        ('Obstacle Pole', 'F'): 6,
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


def _apply_custom_order(events: list[Event], custom_order: list[int]) -> list[Event]:
    """Sort events by a custom ID list. Unrecognized events go at the end."""
    order_map = {eid: idx for idx, eid in enumerate(custom_order)}
    fallback = len(custom_order)
    return sorted(events, key=lambda e: order_map.get(e.id, fallback))


def _gender_rank(gender: str | None) -> int:
    if gender == 'M':
        return 0
    if gender == 'F':
        return 1
    return 2


def _add_mandatory_day_split_run2(schedule_entries: list[dict], college_events: list[Event]) -> list[dict]:
    """Always include Run 2 of day-split events on Saturday when configured."""
    existing_event_ids = {entry.get('event_id') for entry in schedule_entries}
    updated = list(schedule_entries)
    for event in college_events:
        if event.name not in DAY_SPLIT_EVENT_NAMES or event.event_type != 'college':
            continue
        if event.id in existing_event_ids:
            continue
        updated.append({
            'slot': len(updated) + 1,
            'event_id': event.id,
            'label': f"{event.display_name} (Run 2)",
            'event_type': event.event_type,
            'stand_type': event.stand_type,
            'is_run2': True,
        })
    return updated


# ---------------------------------------------------------------------------
# Ordered-heats helpers — authoritative per-day run order as Heat rows.
# Pure reads, no side effects. Consumed by services.saw_block_assignment
# and safe to call from any route-time context.
# ---------------------------------------------------------------------------

def get_friday_ordered_heats(tournament: Tournament) -> list:
    """Return all Friday heats in the authoritative run order.

    Respects schedule_config['friday_event_order'] when present, else falls
    back to _college_friday_sort_key default. Within each event, heats are
    ordered by heat_number ascending, run_number=1 only. Excludes dual-run
    run_number=2 heats (those run Saturday).

    Events selected for Saturday spillover (schedule_config
    ['saturday_college_event_ids']) or moved to the Friday Night Feature
    (_extract_collegiate_feature_events) are excluded from the Friday day
    block, matching the existing _build_friday_day_block() scope.
    """
    from models import Heat

    sched_cfg = tournament.get_schedule_config()
    friday_custom = sched_cfg.get('friday_event_order')
    saturday_college_ids = set(sched_cfg.get('saturday_college_event_ids') or [])

    college_events = tournament.events.filter_by(event_type='college').all()
    friday_college = [e for e in college_events if e.id not in saturday_college_ids]
    friday_college, _ = _extract_collegiate_feature_events(friday_college)

    if friday_custom:
        ordered_events = _apply_custom_order(friday_college, friday_custom)
    else:
        ordered_events = sorted(friday_college, key=_college_friday_sort_key)

    heats: list = []
    for event in ordered_events:
        event_heats = (
            Heat.query
            .filter_by(event_id=event.id, run_number=1)
            .order_by(Heat.heat_number)
            .all()
        )
        heats.extend(event_heats)
    return heats


def get_saturday_ordered_heats(tournament: Tournament) -> list:
    """Return all Saturday heats in the authoritative run order.

    When Flight rows exist: iterates flights by flight_number ascending,
    calling flight.get_heats_ordered() within each flight. Day-split
    Run 2 heats are assumed to be attached to flights via the spillover
    integration; any that are not get appended defensively at the end.

    When no flights exist: falls back to pro heats ordered by event_id +
    heat_number (run_number=1), using schedule_config['saturday_event_order']
    when set. Day-split college Run 2 heats are appended at the end
    (mandatory Saturday placement per CLAUDE.md).
    """
    from models import Heat

    flights = (
        Flight.query
        .filter_by(tournament_id=tournament.id)
        .order_by(Flight.flight_number)
        .all()
    )

    heats: list = []
    seen_ids: set[int] = set()

    if flights:
        for flight in flights:
            for heat in flight.get_heats_ordered():
                heats.append(heat)
                seen_ids.add(heat.id)
    else:
        sched_cfg = tournament.get_schedule_config()
        saturday_custom = sched_cfg.get('saturday_event_order')
        pro_events = tournament.events.filter_by(event_type='pro').all()
        if saturday_custom:
            ordered_events = _apply_custom_order(pro_events, saturday_custom)
        else:
            ordered_events = sorted(pro_events, key=lambda e: e.id)
        for event in ordered_events:
            event_heats = (
                Heat.query
                .filter_by(event_id=event.id, run_number=1)
                .order_by(Heat.heat_number)
                .all()
            )
            for h in event_heats:
                heats.append(h)
                seen_ids.add(h.id)

    # Include dual-run Run 2 heats for day-split college events
    # (Chokerman's Race, Speed Climb) — their Run 2 is always Saturday.
    college_events = tournament.events.filter_by(event_type='college').all()
    for event in college_events:
        if event.name not in DAY_SPLIT_EVENT_NAMES:
            continue
        run2_heats = (
            Heat.query
            .filter_by(event_id=event.id, run_number=2)
            .order_by(Heat.heat_number)
            .all()
        )
        for h in run2_heats:
            if h.id not in seen_ids:
                heats.append(h)
                seen_ids.add(h.id)

    return heats
