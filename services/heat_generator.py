"""
Heat generation service using snake draft distribution.
Adapted from STRATHEX tournament_ui.py patterns.
"""
import logging
import math
from collections import Counter
from functools import wraps

import config
from config import LIST_ONLY_EVENT_NAMES
from config import event_rank_category as _rank_category_for_event
from database import db
from models import Event, EventResult, Heat, HeatAssignment
from models.competitor import CollegeCompetitor, ProCompetitor
from services.gear_sharing import (
    competitors_share_gear_for_event,
    event_matches_gear_key,
    get_family_events,
    infer_equipment_categories,
    normalize_event_text,
    normalize_person_name,
    strip_using_prefix,
)

logger = logging.getLogger(__name__)

# The venue owns exactly one left-hand-configured springboard dummy and it is
# physically stand 4. This is a fact about the site, not a preference, so it
# lives here as a named constant instead of being spelled as a bare 4 in the
# middle of the assignment loop, which is how it came to be paired with a
# hardcoded [1, 2, 3] for everyone else.
LH_SPRINGBOARD_STAND = 4
# LIST_ONLY_EVENT_NAMES and _rank_category_for_event imported from config above.

# Compatibility cache for routes that still ask about the old warning-only
# fallback. Successful generation now guarantees this cache remains empty.
_last_gear_violations: dict[int, list[dict]] = {}

# Compatibility cache for the old left-handed overflow warning. Springboard
# generation now expands to one left-handed cutter per heat instead.
_last_lh_overflow_warnings: dict[int, list[dict]] = {}

# Per-event unpaired-partnered-competitor log, populated by
# _build_partner_units() when a partnered-event entrant cannot be paired
# (partner_name blank, unresolved against the event pool, self-reference, or
# nonreciprocal).
# Generation records these details and raises HeatGenerationSafetyError before
# replacing the existing layout.
_last_unpaired_partnered: dict[int, list[dict]] = {}

# Per-event log of entrants dropped by the gendered-event filter in
# _get_event_competitors. Same purpose as the unpaired-partnered log above:
# they are active, they are entered in the event, they will not appear in any
# heat, and somebody has to say so.
#
# The placement validator in generate_event_heats looks like it already covers
# this and does not. The gender filter runs inside _get_event_competitors, so
# the excluded competitor never reaches `competitors`, expected_ids never holds
# their id, `missing` comes out empty, and not even the logger.warning fires.
_last_gender_excluded: dict[int, list[dict]] = {}


class HeatGenerationSafetyError(ValueError):
    """Raised when a requested heat layout cannot be run safely."""


def _serialize_schedule_heat_generation(func):
    """Make direct heat generation participate in the schedule writer lock."""
    @wraps(func)
    def locked(event, *args, **kwargs):
        # Lightweight validation callers use event-shaped stubs and never
        # mutate database rows. Real generation always receives a persisted
        # Event and must join the tournament writer protocol.
        if not isinstance(event, Event):
            return func(event, *args, **kwargs)

        from services.flight_builder import (
            lock_tournament_schedule,
            sqlite_schedule_writer_guard,
        )

        with sqlite_schedule_writer_guard(event.tournament_id):
            lock_tournament_schedule(event.tournament_id)
            db.session.refresh(event)
            return func(event, *args, **kwargs)

    return locked


def assert_heat_regeneration_safe(
    events: Event | list[Event] | tuple[Event, ...],
    *,
    allow_flight_replacement: bool = False,
) -> None:
    """Reject a partial regeneration that would silently detach flighted heats."""
    if allow_flight_replacement:
        return

    event_list = (
        list(events)
        if isinstance(events, (list, tuple, set))
        else [events]
    )
    event_ids = [event.id for event in event_list if event.id is not None]
    if not event_ids:
        return

    flighted_heat = (
        Heat.query
        .filter(
            Heat.event_id.in_(event_ids),
            Heat.flight_id.isnot(None),
        )
        .order_by(Heat.event_id, Heat.flight_id, Heat.flight_position, Heat.id)
        .first()
    )
    if flighted_heat is None:
        return

    event = next(
        candidate for candidate in event_list
        if candidate.id == flighted_heat.event_id
    )
    flight_number = getattr(flighted_heat.flight, 'flight_number', None)
    flight_label = (
        f'Flight {flight_number}' if flight_number is not None else 'a flight'
    )
    raise HeatGenerationSafetyError(
        f'{event.display_name} already has heats assigned to {flight_label}. '
        'Standalone regeneration is blocked because it would detach the live '
        'schedule. Use Generate All / One-Click Generate so heat replacement '
        'and flight rebuilding commit together.'
    )


def get_last_gear_violations(event_id: int) -> list[dict]:
    """Return legacy warning details; safe successful generation leaves none."""
    return list(_last_gear_violations.get(event_id, []))


def get_last_lh_overflow_warnings(event_id: int) -> list[dict]:
    """Return legacy LH warnings; safe generation expands instead."""
    return list(_last_lh_overflow_warnings.get(event_id, []))


def get_last_unpaired_partnered(event_id: int) -> list[dict]:
    """Return the unpaired-partnered-competitor list recorded by the most
    recent generate_event_heats(event) call for this event_id, or an empty
    list. Each entry is a dict with keys: comp_id, comp_name, partner_name
    (raw string from partners JSON, possibly empty), reason ('blank' |
    'unresolved' | 'self_reference' | 'nonreciprocal')."""
    return list(_last_unpaired_partnered.get(event_id, []))


def get_last_gender_excluded(event_id: int) -> list[dict]:
    """Return the wrong-gender entrants dropped by the most recent
    generate_event_heats(event) call for this event_id, or an empty list.
    Each entry is a dict with keys: comp_id, comp_name, comp_gender."""
    return list(_last_gender_excluded.get(event_id, []))


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


def _effective_heat_capacity(
    event: Event,
    configured_capacity: int,
    stand_numbers: list[int],
) -> int:
    """Return the number of stand units that can physically run in one heat."""
    capacity = configured_capacity
    if stand_numbers:
        capacity = min(capacity, len(stand_numbers))
    if getattr(event, 'stand_type', None) == 'saw_hand':
        capacity = min(capacity, 4)
    return capacity


def _format_candidate_names(entries: list[dict], *, limit: int = 5) -> str:
    names = [str(entry.get('name') or entry.get('comp_name') or '').strip()
             for entry in entries]
    names = [name for name in names if name]
    summary = ', '.join(names[:limit])
    if len(names) > limit:
        summary += f' (+{len(names) - limit} more)'
    return summary or 'the affected entrants'


def _event_and_gear_family(event: Event) -> list[Event]:
    all_events = _get_tournament_events(event)
    return [event, *get_family_events(event, all_events)]


def _unmapped_key_targets_event(raw_key: str, relevant_events: list[Event]) -> bool:
    """Recognize a mistyped current-event key without gating unrelated keys."""
    normalized_key = normalize_event_text(raw_key)
    if (
        not normalized_key
        or normalized_key.isdigit()
        or len(normalized_key) < 4
    ):
        return False
    for candidate in relevant_events:
        for label in (candidate.name, candidate.display_name):
            normalized_label = normalize_event_text(label)
            if (
                len(normalized_label) >= 4
                and (
                    normalized_label in normalized_key
                    or normalized_key in normalized_label
                )
            ):
                return True
    return False


def _free_text_targets_event(details: str, relevant_events: list[Event]) -> bool:
    normalized_details = normalize_event_text(details)
    for candidate in relevant_events:
        labels = {
            normalize_event_text(candidate.name),
            normalize_event_text(candidate.display_name),
        }
        if any(label and len(label) >= 4 and label in normalized_details for label in labels):
            return True
    return any(
        event_matches_gear_key(candidate, f'category:{category}')
        for category in infer_equipment_categories(details)
        for candidate in relevant_events
    )


def _validate_event_gear_declarations(
    event: Event,
    competitors: list[dict],
) -> None:
    """Reject incomplete declarations that could hide a current-event conflict."""
    relevant_events = _event_and_gear_family(event)
    model = CollegeCompetitor if event.event_type == 'college' else ProCompetitor
    known_names = {
        normalize_person_name(comp.name)
        for comp in model.query.filter_by(
            tournament_id=event.tournament_id,
            status='active',
        ).all()
    }
    invalid = []

    for competitor in competitors:
        sharing = competitor.get('gear_sharing') or {}
        details = str(competitor.get('gear_sharing_details') or '').strip()
        if details and not sharing and _free_text_targets_event(details, relevant_events):
            invalid.append(competitor)
            continue

        self_name = normalize_person_name(
            competitor.get('base_name') or competitor.get('name') or ''
        )
        for key, raw_partner in sharing.items():
            key_matches = any(
                event_matches_gear_key(candidate, key)
                for candidate in relevant_events
            )
            if not key_matches:
                if _unmapped_key_targets_event(key, relevant_events):
                    invalid.append(competitor)
                    break
                continue

            partner_text = str(raw_partner or '').strip()
            if partner_text.lower().startswith('group:'):
                continue
            partner_name = strip_using_prefix(partner_text)
            partner_norm = normalize_person_name(partner_name)
            if (
                not partner_norm
                or partner_norm == self_name
                or partner_norm not in known_names
            ):
                invalid.append(competitor)
                break

    if invalid:
        raise HeatGenerationSafetyError(
            f'Heat generation blocked for {event.display_name}: a gear declaration '
            f'is incomplete or does not map to this event '
            f'({_format_candidate_names(invalid)}). Resolve it in Preflight; the '
            'previous heat layout was preserved.'
        )


def _validate_candidate_layout(
    event: Event,
    competitors: list[dict],
    heats: list[list[dict]],
    max_units_per_heat: int,
    stand_numbers: list[int],
    *,
    unpaired_log: list[dict] | None = None,
) -> None:
    """Reject any candidate that cannot safely represent the event roster."""
    unpaired = list(unpaired_log or [])
    if unpaired:
        _last_unpaired_partnered[event.id] = unpaired
        raise HeatGenerationSafetyError(
            f'Heat generation blocked for {event.display_name}: partnered '
            f'entrants are not represented ({_format_candidate_names(unpaired)}). '
            'Resolve partner declarations in Preflight before regenerating.'
        )

    gender_excluded = list(_last_gender_excluded.get(event.id, []))
    if gender_excluded:
        raise HeatGenerationSafetyError(
            f'Heat generation blocked for {event.display_name}: entered '
            f'competitors do not match the event gender '
            f'({_format_candidate_names(gender_excluded)}). Correct the entries '
            'before regenerating.'
        )

    if not heats or any(not heat for heat in heats):
        raise HeatGenerationSafetyError(
            f'Heat generation blocked for {event.display_name}: the candidate '
            'contains an empty or missing heat. Correct the roster before regenerating.'
        )

    expected = Counter(comp.get('id') for comp in competitors)
    represented = Counter(comp.get('id') for heat in heats for comp in heat)
    missing_ids = sorted(comp_id for comp_id in expected if represented[comp_id] == 0)
    duplicate_ids = sorted(comp_id for comp_id, count in represented.items() if count != 1)
    unexpected_ids = sorted(comp_id for comp_id in represented if comp_id not in expected)
    if missing_ids or duplicate_ids or unexpected_ids or represented != expected:
        raise HeatGenerationSafetyError(
            f'Heat generation blocked for {event.display_name}: every eligible '
            'entrant must appear exactly once in run 1. The previous heat layout '
            'was preserved.'
        )

    is_partnered = bool(getattr(event, 'is_partnered', False))
    for heat_index, heat in enumerate(heats):
        units = _rebuild_pair_units(heat, event)
        if len(units) > max_units_per_heat:
            raise HeatGenerationSafetyError(
                f'Heat generation blocked for {event.display_name}: candidate '
                f'heat {heat_index + 1} exceeds its physical stand capacity.'
            )
        if is_partnered and any(len(unit) != 2 for unit in units):
            raise HeatGenerationSafetyError(
                f'Heat generation blocked for {event.display_name}: candidate '
                f'heat {heat_index + 1} contains an incomplete partner unit.'
            )
        partner_gender = getattr(event, 'partner_gender_requirement', None)
        if is_partnered and partner_gender in {'mixed', 'same'}:
            invalid_gender_unit = any(
                (partner_gender == 'mixed' and unit[0].get('gender') == unit[1].get('gender'))
                or (partner_gender == 'same' and unit[0].get('gender') != unit[1].get('gender'))
                for unit in units
            )
            if invalid_gender_unit:
                requirement = (
                    'mixed-gender' if partner_gender == 'mixed' else 'same-gender'
                )
                raise HeatGenerationSafetyError(
                    f'Heat generation blocked for {event.display_name}: candidate '
                    f'heat {heat_index + 1} violates the {requirement} partner '
                    'rule. The previous heat layout was preserved.'
                )

        for left_index, left_unit in enumerate(units):
            for right_unit in units[left_index + 1:]:
                conflict = any(
                    _competitors_share_gear_for_event(left, right, event)
                    for left in left_unit
                    for right in right_unit
                )
                if conflict:
                    raise HeatGenerationSafetyError(
                        f'Heat generation blocked for {event.display_name}: '
                        f'candidate heat {heat_index + 1} contains a gear-sharing '
                        'conflict. The previous heat layout was preserved.'
                    )

        if getattr(event, 'stand_type', None) == 'springboard':
            left_handed = [comp for comp in heat if comp.get('is_left_handed')]
            if len(left_handed) > 1:
                raise HeatGenerationSafetyError(
                    f'Heat generation blocked for {event.display_name}: candidate '
                    f'heat {heat_index + 1} requires the left-handed dummy more '
                    'than once.'
                )

    if (
        getattr(event, 'stand_type', None) == 'springboard'
        and any(comp.get('is_left_handed') for comp in competitors)
        and LH_SPRINGBOARD_STAND not in stand_numbers
    ):
        raise HeatGenerationSafetyError(
            f'Heat generation blocked for {event.display_name}: stand '
            f'{LH_SPRINGBOARD_STAND}, the left-handed dummy, is not configured '
            'for this event.'
        )


def _run_one_stands(
    event: Event,
    heat_competitors: list[dict],
    stand_numbers: list[int],
) -> dict[int, int]:
    """Build validated run-one stand assignments for one candidate heat."""
    if getattr(event, 'is_partnered', False):
        stands = {}
        for stand_idx, unit in enumerate(_rebuild_pair_units(heat_competitors, event)):
            stand_num = stand_numbers[stand_idx]
            for comp in unit:
                stands[comp['id']] = stand_num
        return stands

    if getattr(event, 'stand_type', None) == 'springboard':
        left_handed = next(
            (comp for comp in heat_competitors if comp.get('is_left_handed')),
            None,
        )
        if left_handed is not None:
            stands = {left_handed['id']: LH_SPRINGBOARD_STAND}
            right_handed_stands = [
                stand for stand in stand_numbers
                if stand != LH_SPRINGBOARD_STAND
            ]
            right_handed = [
                comp for comp in heat_competitors
                if comp['id'] != left_handed['id']
            ]
            stands.update({
                comp['id']: right_handed_stands[index]
                for index, comp in enumerate(right_handed)
            })
            return stands

    return {
        comp['id']: stand_numbers[index]
        for index, comp in enumerate(heat_competitors)
    }


def _build_candidate_heat_rows(
    event: Event,
    heats: list[list[dict]],
    stand_numbers: list[int],
) -> list[Heat]:
    """Build every Heat and HeatAssignment row before replacing stored heats."""
    candidate_rows = []
    for heat_num, heat_competitors in enumerate(heats, start=1):
        heat = Heat(event_id=event.id, heat_number=heat_num, run_number=1)
        competitor_ids = [comp['id'] for comp in heat_competitors]
        heat.set_roster(
            event.event_type,
            competitor_ids,
            _run_one_stands(event, heat_competitors, stand_numbers),
        )
        candidate_rows.append(heat)

    if not event.requires_dual_runs:
        return candidate_rows

    for heat_num, heat_competitors in enumerate(heats, start=1):
        heat = Heat(event_id=event.id, heat_number=heat_num, run_number=2)
        competitor_ids = [comp['id'] for comp in heat_competitors]
        stands = {}
        if getattr(event, 'is_partnered', False):
            units = _rebuild_pair_units(heat_competitors, event)
            reversed_stands = list(reversed(stand_numbers))
            for unit_index, unit in enumerate(units):
                for comp in unit:
                    stands[comp['id']] = reversed_stands[unit_index]
        else:
            reversed_stands = list(reversed(stand_numbers))
            stands = {
                comp['id']: reversed_stands[index]
                for index, comp in enumerate(heat_competitors)
            }
        heat.set_roster(event.event_type, competitor_ids, stands)
        candidate_rows.append(heat)

    return candidate_rows


@_serialize_schedule_heat_generation
def generate_event_heats(
    event: Event,
    *,
    allow_flight_replacement: bool = False,
) -> int:
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

    # The routes normally reject regeneration after scoring, but this service
    # is also used by bulk and background workflows. Keep every caller from
    # deleting completed score or heat history, including an all-scratch heat
    # that has no completed EventResult.
    if getattr(event, 'is_finalized', False):
        raise HeatGenerationSafetyError(
            f'{event.display_name} is finalized. Heat regeneration is blocked.'
        )
    if getattr(event, 'status', None) == 'completed':
        raise HeatGenerationSafetyError(
            f'{event.display_name} is completed. Heat regeneration is blocked.'
        )
    if getattr(event, 'has_prelims', False):
        from services.partnered_axe import partnered_axe_history_protection_reason

        partnered_axe_reason = partnered_axe_history_protection_reason(event)
        if partnered_axe_reason is not None:
            raise HeatGenerationSafetyError(
                f'{event.display_name} has {partnered_axe_reason}. '
                'Heat regeneration is blocked.'
            )
    elif EventResult.query.filter_by(
        event_id=event.id,
        status='completed',
    ).first() is not None:
        raise HeatGenerationSafetyError(
            f'{event.display_name} has scored results. Heat regeneration is blocked.'
        )
    if Heat.query.filter_by(event_id=event.id, status='completed').first() is not None:
        raise HeatGenerationSafetyError(
            f'{event.display_name} has completed heat history. Heat regeneration is blocked.'
        )
    assert_heat_regeneration_safe(
        event,
        allow_flight_replacement=allow_flight_replacement,
    )

    # Clear the per-tournament event cache so it refreshes each generate call.
    _get_tournament_events._cache = {}
    # This clear is NOT down with its three siblings below. They are cleared
    # after _get_event_competitors returns; this log is WRITTEN inside that
    # call, so clearing it there would erase the record one line after it was
    # made. It has to be cleared before the call, not after.
    _last_gender_excluded.pop(event.id, None)
    # Get competitors for this event
    competitors = _get_event_competitors(event)

    gender_excluded = list(_last_gender_excluded.get(event.id, []))
    if gender_excluded:
        raise HeatGenerationSafetyError(
            f'Heat generation blocked for {event.display_name}: entered '
            f'competitors do not match the event gender '
            f'({_format_candidate_names(gender_excluded)}). Correct the entries '
            'before regenerating.'
        )

    if not competitors:
        raise ValueError(f"No competitors entered for {event.display_name}")

    # OPEN/CLOSED-list events are tracked as signups only, without heats.
    if _is_list_only_event(event):
        _delete_event_heats(event.id)
        event.status = 'in_progress'
        db.session.flush()  # Caller is responsible for commit — preserves atomic transactions.
        return 0

    # Prelim-based events (Partnered Axe Throw) are managed by a dedicated
    # state machine (services.partnered_axe.PartneredAxeThrow), not the
    # standard snake-draft generator. Skip so we don't produce one-pair-per-heat
    # output that bypasses the prelim/final flow.
    if getattr(event, 'has_prelims', False):
        _delete_event_heats(event.id)
        event.status = 'pending'
        db.session.flush()
        return 0

    _validate_event_gear_declarations(event, competitors)

    # Get stand configuration; event.max_stands is authoritative when set
    stand_config = config.STAND_CONFIGS.get(event.stand_type, {})
    max_per_heat = event.max_stands if event.max_stands is not None else stand_config.get('total', 4)
    if max_per_heat is None or int(max_per_heat) <= 0:
        raise ValueError(
            f"{event.display_name} has invalid max_stands={max_per_heat}. "
            'Set max_stands to at least 1 before generating heats.'
        )
    max_per_heat = int(max_per_heat)
    stand_numbers = _stand_numbers_for_event(event, max_per_heat, stand_config)
    effective_capacity = _effective_heat_capacity(
        event,
        max_per_heat,
        stand_numbers,
    )
    if effective_capacity <= 0:
        raise HeatGenerationSafetyError(
            f'{event.display_name} has no usable stands configured. Configure '
            'at least one physical stand before generating heats.'
        )

    # Calculate number of heats needed
    num_heats = math.ceil(len(competitors) / effective_capacity)

    # Helpers expand locally until a gear-safe candidate exists. Build and
    # validate that candidate before deleting an existing layout.
    gear_violations: list[dict] = []
    _last_gear_violations.pop(event.id, None)

    # Kept for compatibility with the helper signature and route getter.
    lh_warnings: list[dict] = []
    _last_lh_overflow_warnings.pop(event.id, None)

    # Per-event unpaired-partnered-competitor list. Populated when a partnered
    # event entrant has a blank, unresolved, self-referential, or nonreciprocal
    # partner_name.
    # The final candidate validator treats any entry here as a hard blocker.
    unpaired_log: list[dict] = []
    _last_unpaired_partnered.pop(event.id, None)

    # Apply special constraints
    if event.stand_type == 'springboard':
        heats = _generate_springboard_heats(competitors, num_heats, effective_capacity, stand_config, event=event,
                                            gear_violations=gear_violations,
                                            lh_warnings=lh_warnings)
    elif event.stand_type in ['saw_hand']:
        heats = _generate_saw_heats(competitors, num_heats, effective_capacity, stand_config, event=event,
                                    gear_violations=gear_violations,
                                    unpaired_log=unpaired_log)
    else:
        heats = _generate_standard_heats(competitors, num_heats, effective_capacity, event=event,
                                         gear_violations=gear_violations,
                                         unpaired_log=unpaired_log)

    _validate_candidate_layout(
        event,
        competitors,
        heats,
        effective_capacity,
        stand_numbers,
        unpaired_log=unpaired_log,
    )
    # Build every HeatAssignment row while the stored layout still exists. Any
    # invalid identity or stand mapping therefore fails before replacement.
    candidate_heats = _build_candidate_heat_rows(event, heats, stand_numbers)

    # No safety blocker remains, so replacing the current layout is safe.
    _delete_event_heats(event.id)

    # Use actual heat count returned by the generator (saw events recalculate internally).
    actual_heat_count = len(heats)
    db.session.add_all(candidate_heats)

    event.status = 'in_progress'
    db.session.flush()

    # Stock Saw: alternate solo-heat stands across 7 and 8 so consecutive
    # solos don't pile onto the same physical stand. Must run after the flush
    # above so the HeatAssignment rows reflect the final layout.
    rebalance_stock_saw_solo_stands(event)

    # Promote LH overflow warnings for springboard events, same pattern.
    if lh_warnings:
        resolved_lh: list[dict] = []
        for w in lh_warnings:
            idx = w.get('heat_index')
            heat_id = None
            heat_number = None
            if isinstance(idx, int) and 0 <= idx < len(candidate_heats):
                heat_id = candidate_heats[idx].id
                heat_number = candidate_heats[idx].heat_number
            resolved_lh.append({
                'type': w.get('type'),
                'heat_id': heat_id,
                'heat_number': heat_number,
                'overflow_count': w.get('overflow_count'),
                'overflow_names': w.get('overflow_names', []),
            })
            logger.warning(
                'LH SPRINGBOARD OVERFLOW: %d cutter(s) overflowed into heat %s — LH dummy contention expected',
                w.get('overflow_count', 0), heat_id,
            )
        _last_lh_overflow_warnings[event.id] = resolved_lh

    # Flush but do NOT commit — the calling route owns the transaction boundary and
    # will commit (or roll back) after all scheduling actions are complete.  This
    # prevents partial state if a later step in the same request fails.
    db.session.flush()

    return actual_heat_count


def _get_event_competitors(event: Event) -> list:
    """Get list of competitors entered in this event with their info.

    Always scans active competitors to discover new registrations that don't
    yet have EventResult rows (fixes silent omission on heat regeneration).
    """
    competitors = []
    seen_ids: set[int] = set()

    # Phase 1: Collect from existing EventResult rows (preserves scored data).
    existing_result_comp_ids: set[int] = set()
    for result in event.results.all():
        existing_result_comp_ids.add(result.competitor_id)

    # Phase 2: Scan ALL active competitors for this event to catch new entrants.
    #
    # O3: ORDER BY id, explicitly. This list is the snake draft's input, and
    # when no ability ranks exist _sort_by_ability_rank returns it unchanged
    # under the comment "fall back to registration order". Without an ORDER BY
    # that was really PHYSICAL ROW ORDER, which Postgres does not guarantee:
    # measured on the 2026 mirror with rows reinserted in reverse, every
    # generated heat roster changed. id order is registration order, so this
    # makes the fallback's comment true instead of coincidental.
    if event.event_type == 'college':
        all_comps = CollegeCompetitor.query.filter_by(
            tournament_id=event.tournament_id,
            status='active'
        ).order_by(CollegeCompetitor.id).all()
    else:
        all_comps = ProCompetitor.query.filter_by(
            tournament_id=event.tournament_id,
            status='active'
        ).order_by(ProCompetitor.id).all()

    # Filter by gender if gendered event.
    # Record who this drops before dropping them. An active competitor who is
    # entered in this event and is the wrong gender for it will not appear in
    # any heat, and the placement validator in generate_event_heats cannot see
    # it: they never reach `competitors`, so `missing` comes out empty. Only
    # entrants are recorded. Everybody else in the tournament is the wrong
    # gender for a gendered event and saying so would be noise.
    if event.gender:
        excluded = [
            c for c in all_comps
            if c.gender != event.gender
            and _competitor_entered_event(event, c.get_events_entered())
        ]
        if excluded:
            _last_gender_excluded[event.id] = [
                {'comp_id': c.id,
                 'comp_name': c.display_name,
                 'comp_gender': c.gender}
                for c in sorted(excluded, key=lambda c: c.id)
            ]
            for entry in _last_gender_excluded[event.id]:
                logger.warning(
                    'heat_generator: competitor %s (%r, gender=%r) is entered in '
                    'event %r (gender=%r) but is excluded from it and will not '
                    'be placed in any heat',
                    entry['comp_id'], entry['comp_name'], entry['comp_gender'],
                    event.display_name, event.gender,
                )
        all_comps = [c for c in all_comps if c.gender == event.gender]

    for comp in all_comps:
        if not _competitor_entered_event(event, comp.get_events_entered()):
            continue
        if comp.id in seen_ids:
            continue
        seen_ids.add(comp.id)

        # Create EventResult row if one doesn't exist yet (new entrant).
        if comp.id not in existing_result_comp_ids:
            result = EventResult(
                event_id=event.id,
                competitor_id=comp.id,
                competitor_type=event.event_type,
                competitor_name=comp.display_name
            )
            db.session.add(result)

        comp_data = {
            'id': comp.id,
            'name': comp.display_name,
            # Bare name (no team-code suffix) used for partner pairing —
            # partner_name on the competitor side stores just "First Last",
            # so we must match against the bare name, not display_name.
            'base_name': getattr(comp, 'name', comp.display_name),
            'gender': comp.gender,
            'is_left_handed': getattr(comp, 'is_left_handed_springboard', False),
            'gear_sharing': comp.get_gear_sharing() if hasattr(comp, 'get_gear_sharing') else {},
            'gear_sharing_details': getattr(comp, 'gear_sharing_details', ''),
            'partner_name': _get_partner_name_for_event(comp, event)
        }
        if event.event_type == 'pro':
            comp_data['is_slow_springboard'] = bool(getattr(comp, 'springboard_slow_heat', False))

        competitors.append(comp_data)

    db.session.flush()
    return competitors


def _move_partial_heats_to_end(heats: list, sizes: list, max_per_heat: int) -> tuple[list, dict[int, int]]:
    """Reorder heats so any short/partial-fill heats run AFTER the full ones.

    Convention (user rule, 2026-04-22): when a field doesn't divide evenly into
    the heat size (e.g. odd N with 2-up stock saw), the leftover competitor or
    partial heat closes out the event rather than starting it. Snake-draft on
    its own leaves the partial in heat 0 because the second pass turns around
    early; this sorts the heats by fill count, largest first, so every short
    heat runs after every fuller one. The sort is STABLE, so heats of equal
    fill keep their draft order and the skill mix is unchanged.

    Ordering by fill count rather than partitioning into full-vs-partial
    matters whenever NO heat reaches `max_per_heat`. Eleven cutters over five
    stands generates 3/4/4: a full-vs-partial split finds no full heat at all
    and leaves the short heat opening the event, which is the case this
    convention exists to prevent. Ordering by size handles it, and handles a
    three-level shape (5/3/4 under a cap of 6) that a threshold of any kind
    cannot.

    `sizes` is parallel to `heats` and reports the *capacity-relevant* fill
    count for each heat — stand-units for partnered events, competitor count
    otherwise — so the ordering matches the generator's own bookkeeping.

    Returns `(reordered_heats, old_to_new)` where `old_to_new[i]` is the new
    index of what used to be heat `i`. Identity mapping when no reorder runs
    (single heat, all heats the same size, or any heat over capacity; the final
    validator reports over-capacity candidates without reordering them first).

    Callers MUST use `old_to_new` to remap any side-channel data that carries
    pre-reorder heat indices (gear_violations, lh_warnings) — otherwise those
    warnings end up pointing at the wrong heat after the reorder.
    """
    identity = {i: i for i in range(len(heats))}
    if len(heats) <= 1:
        return heats, identity
    if any(s > max_per_heat for s in sizes):
        return heats, identity
    new_order = sorted(range(len(heats)), key=lambda i: (-sizes[i], i))
    if new_order == list(range(len(heats))):
        return heats, identity
    old_to_new = {old: new for new, old in enumerate(new_order)}
    return [heats[i] for i in new_order], old_to_new


def _remap_violation_heat_indices(violations: list | None, old_to_new: dict[int, int]) -> None:
    """Update each violation's `heat_index` after a heat reorder so the surfaced
    warning points at the heat the competitor actually landed in. No-op when
    `violations` is None or the mapping is identity."""
    if not violations:
        return
    if all(old == new for old, new in old_to_new.items()):
        return
    for v in violations:
        idx = v.get('heat_index')
        if isinstance(idx, int) and idx in old_to_new:
            v['heat_index'] = old_to_new[idx]


def _try_place_units(
    units: list[list[dict]],
    num_heats: int,
    max_units_per_heat: int,
    event: Event,
) -> tuple[list[list[dict]], list[int]] | None:
    """Try one deterministic snake pass without relaxing gear constraints."""
    heats = [[] for _ in range(num_heats)]
    units_used = [0] * num_heats
    direction = 1
    heat_idx = 0

    for unit in units:
        placed = False
        examined = set()
        while len(examined) < num_heats:
            if heat_idx in examined:
                heat_idx, direction = _advance_snake_index(
                    heat_idx,
                    direction,
                    num_heats,
                )
                continue
            examined.add(heat_idx)
            has_conflict = any(
                _has_gear_sharing_conflict(comp, heats[heat_idx], event)
                for comp in unit
            )
            if units_used[heat_idx] < max_units_per_heat and not has_conflict:
                heats[heat_idx].extend(unit)
                units_used[heat_idx] += 1
                placed = True
                break
            heat_idx, direction = _advance_snake_index(
                heat_idx,
                direction,
                num_heats,
            )

        if not placed:
            return None
        heat_idx, direction = _advance_snake_index(
            heat_idx,
            direction,
            num_heats,
        )

    return heats, units_used


def _generate_standard_heats(competitors: list, num_heats: int, max_per_heat: int, event: Event = None,
                              gear_violations: list | None = None,
                              unpaired_log: list | None = None) -> list:
    """
    Generate heats using snake draft distribution.

    Snake draft ensures each heat has a mix of skill levels.

    For partnered events, each unit (a pair) occupies ONE stand. `max_per_heat`
    therefore counts STANDS, not individual competitors, and num_heats is
    recomputed from unit count so we don't over-allocate empty heats.

    Partnered-event entrants whose partner cannot be resolved are HELD BACK
    (not placed solo) and recorded in ``unpaired_log`` for the route to
    surface. See ``_build_partner_units`` for the matching ladder.
    """
    competitors = _sort_by_ability(competitors, event)
    units = _build_partner_units(competitors, event, unpaired_log=unpaired_log)
    # Re-sort partner units by composite rank so paired competitors enter the
    # snake draft in the right ability order (#22).
    units = _sort_units_by_ability(units, event)

    is_partnered = bool(event and getattr(event, 'is_partnered', False))

    minimum_heats = max(1, math.ceil(len(units) / max_per_heat))
    if not is_partnered:
        minimum_heats = max(minimum_heats, num_heats)

    # One unit per heat is always safe because gear is checked between units,
    # never within a legitimate partner pair. Expand only as far as required.
    for candidate_count in range(minimum_heats, max(minimum_heats, len(units)) + 1):
        candidate = _try_place_units(
            units,
            candidate_count,
            max_per_heat,
            event,
        )
        if candidate is None:
            continue
        heats, stands_used = candidate
        heats, old_to_new = _move_partial_heats_to_end(
            heats,
            stands_used,
            max_per_heat,
        )
        _remap_violation_heat_indices(gear_violations, old_to_new)
        return heats

    raise HeatGenerationSafetyError(
        f'Heat generation blocked for {getattr(event, "display_name", "event")}: '
        'no gear-safe heat layout represents every entrant.'
    )


def _first_token(value: str) -> str:
    """Return the first whitespace-separated token of a normalized name."""
    value = _norm_name(value or '')
    return value.split(' ', 1)[0] if value else ''


def _find_partner(partner_name: str, pool: list, self_comp: dict) -> dict | None:
    """Best-effort partner match against a pool of competitors.

    Three-tier ladder via services.name_match.find_partner_match:
      1. Exact full-name (normalized) match.
      2. First-token (first-name) match — one match only.
      3. Levenshtein ≤ 2 fuzzy match — one match only.

    Tier 3 catches form-typed misspellings like "McKinlay" → "McKinley" that
    used to silently land a competitor solo on a stand. The one-match-only
    rule on tiers 2 and 3 prevents picking the wrong person when the pool
    has several similar names.

    `self_comp` is excluded from the match pool. Returns the matched competitor
    dict or None.
    """
    from services.name_match import find_partner_match

    return find_partner_match(
        partner_name,
        pool,
        name_getter=lambda c: c.get('base_name') or c.get('name'),
        exclude_key=self_comp.get('id') if isinstance(self_comp, dict) else None,
    )


def _rebuild_pair_units(heat_competitors: list, event: Event) -> list:
    """Recover pair units from a flat heat competitor list.

    Partners are stored per-competitor as `partner_name`; this walks the heat's
    comps, pairs up anyone whose partner is also in the heat, and emits one unit
    per stand: `[[c1, c2], [c3, c4], [solo], ...]`.  Stand assignment uses this
    so both halves of a pair share a stand number.
    """
    if not event or not event.is_partnered:
        return [[c] for c in heat_competitors]

    used = set()
    units = []
    for comp in heat_competitors:
        if comp['id'] in used:
            continue
        partner_name = comp.get('partner_name')
        partner = _find_partner(partner_name, heat_competitors, comp)
        if (
            partner
            and partner['id'] not in used
            and _partner_points_back(partner, comp, heat_competitors)
        ):
            units.append([comp, partner])
            used.add(comp['id'])
            used.add(partner['id'])
            continue
        units.append([comp])
        used.add(comp['id'])
    return units


def _partner_points_back(partner: dict, comp: dict, pool: list) -> bool:
    """Return True when partner's partner string resolves back to comp."""
    reciprocal_name = (partner.get('partner_name') or '').strip()
    if not reciprocal_name:
        return False
    reciprocal = _find_partner(reciprocal_name, pool, partner)
    return bool(reciprocal and reciprocal.get('id') == comp.get('id'))


def _build_partner_units(
    competitors: list,
    event: Event,
    *,
    skip_unpaired: bool = True,
    unpaired_log: list | None = None,
) -> list:
    """Build assignment units; partnered events keep recognized pairs together.

    Uses `_find_partner` (which goes through the shared ``services.name_match``
    ladder including Levenshtein ≤ 2 fuzzy matching) so nicknames, first-name-
    only partner strings, AND minor typos pair correctly when unambiguous.

    Partnered-event behaviour change (2026-04-23): when ``skip_unpaired`` is
    True (default), competitors whose partner cannot be resolved are HELD BACK
    rather than placed solo on a stand. Solo placement of a partnered-event
    competitor is wrong by definition — the event needs a pair to function.
    Each held-back entry is appended to ``unpaired_log`` (when supplied) so
    the route can surface them to the operator and Preflight can offer a
    resolution UI before the next generate run.

    Domain-contract tightening (2026-04-27): one-sided partner references are
    not a valid generated pair. A says B only pairs when B's partner string
    resolves back to A. Otherwise A is held back as ``nonreciprocal`` and B is
    evaluated normally.

    When ``skip_unpaired`` is False (legacy behaviour), unpaired competitors
    fall through to a solo unit and the snake-draft places them on their own
    stand. The legacy path is preserved so callers that need it (e.g. unit
    tests asserting old shape) can opt in explicitly.
    """
    if not event or not event.is_partnered:
        return [[c] for c in competitors]

    used = set()
    units = []
    self_norm_lookup = {c['id']: _norm_name(c.get('base_name') or c.get('name'))
                        for c in competitors}

    for comp in competitors:
        comp_id = comp['id']
        if comp_id in used:
            continue

        raw_partner_name = (comp.get('partner_name') or '').strip()
        partner = _find_partner(raw_partner_name, competitors, comp)
        if (
            partner
            and partner['id'] not in used
            and partner['id'] != comp_id
            and _partner_points_back(partner, comp, competitors)
        ):
            units.append([comp, partner])
            used.add(comp_id)
            used.add(partner['id'])
            continue

        # No valid reciprocal partner: classify the failure for the operator log.
        partner_norm = _norm_name(raw_partner_name)
        if not raw_partner_name:
            reason = 'blank'
        elif partner_norm and partner_norm == self_norm_lookup.get(comp_id, ''):
            reason = 'self_reference'
        elif partner and partner['id'] != comp_id:
            reason = 'nonreciprocal'
        else:
            reason = 'unresolved'

        if unpaired_log is not None:
            unpaired_log.append({
                'comp_id': comp_id,
                'comp_name': comp.get('name') or comp.get('base_name') or '',
                'partner_name': raw_partner_name,
                'reason': reason,
            })

        if skip_unpaired:
            # Hold back from heat generation — operator resolves in Preflight.
            used.add(comp_id)
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
                                 max_per_heat: int, stand_config: dict, event: Event = None,
                                 gear_violations: list | None = None,
                                 lh_warnings: list | None = None) -> list:
    """
    Generate springboard heats with left-handed cutter spreading.

    Only one physical left-handed springboard dummy exists on site, so each
    left-handed cutter needs a separate heat. Slow cutters remain clustered in
    the closing heat block. If either gear or dummy capacity prevents a safe
    placement, the event gains one heat and the deterministic pass is retried.
    """
    if not competitors:
        return []

    left_handed = _sort_by_ability(
        [comp for comp in competitors if comp.get('is_left_handed')],
        event,
    )
    slow_cutters = _sort_by_ability(
        [comp for comp in competitors if comp.get('is_slow_springboard')],
        event,
    )
    slow_ids = {comp['id'] for comp in slow_cutters}
    regular_lh = [comp for comp in left_handed if comp['id'] not in slow_ids]
    slow_lh = [comp for comp in left_handed if comp['id'] in slow_ids]

    minimum_heats = max(
        num_heats,
        math.ceil(len(competitors) / max_per_heat),
        len(left_handed),
    )

    for candidate_count in range(minimum_heats, len(competitors) + 1):
        heats = [[] for _ in range(candidate_count)]
        assigned_ids = set()

        # Fast/non-slow LH cutters retain the established front-to-back ability
        # order. Slow LH cutters use the closing slots without sharing a dummy.
        for index, comp in enumerate(regular_lh):
            heats[index].append(comp)
            assigned_ids.add(comp['id'])
        for offset, comp in enumerate(reversed(slow_lh), start=1):
            heats[-offset].append(comp)
            assigned_ids.add(comp['id'])

        failed = False
        for comp in slow_cutters:
            if comp['id'] in assigned_ids:
                continue
            placed = False
            for heat_idx in range(candidate_count - 1, -1, -1):
                if (
                    len(heats[heat_idx]) < max_per_heat
                    and not _has_gear_sharing_conflict(comp, heats[heat_idx], event)
                ):
                    heats[heat_idx].append(comp)
                    assigned_ids.add(comp['id'])
                    placed = True
                    break
            if not placed:
                failed = True
                break
        if failed:
            continue

        remaining = _sort_by_ability(
            [comp for comp in competitors if comp['id'] not in assigned_ids],
            event,
        )
        direction = 1
        heat_idx = 0
        for comp in remaining:
            placed = False
            examined = set()
            while len(examined) < candidate_count:
                if heat_idx in examined:
                    heat_idx, direction = _advance_snake_index(
                        heat_idx,
                        direction,
                        candidate_count,
                    )
                    continue
                examined.add(heat_idx)
                if (
                    len(heats[heat_idx]) < max_per_heat
                    and not _has_gear_sharing_conflict(comp, heats[heat_idx], event)
                ):
                    heats[heat_idx].append(comp)
                    assigned_ids.add(comp['id'])
                    placed = True
                    break
                heat_idx, direction = _advance_snake_index(
                    heat_idx,
                    direction,
                    candidate_count,
                )
            if not placed:
                failed = True
                break
            heat_idx, direction = _advance_snake_index(
                heat_idx,
                direction,
                candidate_count,
            )
        if failed or any(not heat for heat in heats):
            continue

        if not slow_cutters:
            heats, old_to_new = _move_partial_heats_to_end(
                heats,
                [len(heat) for heat in heats],
                max_per_heat,
            )
            _remap_violation_heat_indices(gear_violations, old_to_new)
        return heats

    raise HeatGenerationSafetyError(
        f'Heat generation blocked for {getattr(event, "display_name", "springboard")}: '
        'no gear-safe springboard layout represents every cutter.'
    )


def _generate_saw_heats(competitors: list, num_heats: int,
                        max_per_heat: int, stand_config: dict, event: Event = None,
                        gear_violations: list | None = None,
                        unpaired_log: list | None = None) -> list:
    """
    Generate saw heats respecting stand group constraints.

    Saw stands are in groups of 4. One group runs while the other sets up.

    For partnered saw events (Jack & Jill, Double Buck) the unpaired_log
    captures any entrant whose partner cannot be resolved so the operator
    can fix it in Preflight before regenerating.
    """
    # Standard snake draft, but limit to 4 per heat for saw events
    actual_max = min(max_per_heat, 4)  # Saw groups are 4 each
    num_heats = math.ceil(len(competitors) / actual_max)

    return _generate_standard_heats(competitors, num_heats, actual_max, event=event,
                                    gear_violations=gear_violations,
                                    unpaired_log=unpaired_log)


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
    # Missoula rule (DOMAIN_CONTRACT): ALL stock saw — pro and college — runs
    # on saw stands 7 and 8 only.
    if _normalize_name(event.name) == _normalize_name('Stock Saw'):
        return [7, 8][:max_per_heat]

    specific = stand_config.get('specific_stands')
    if specific:
        return list(specific)[:max_per_heat]

    return list(range(1, max_per_heat + 1))


def _is_stock_saw(event: Event) -> bool:
    return _normalize_name(event.name) == _normalize_name('Stock Saw')


def rebalance_stock_saw_solo_stands(event: Event) -> int:
    """Normalize Stock Saw stand assignments: pairs always use 7+8, solos
    alternate 7, 8, 7, 8... across the event in heat_number order so the
    off-stand can be set up while the on-stand runs.

    Scope (DOMAIN_CONTRACT): ALL Stock Saw — pro and college — runs on
    stands 7 and 8 only. Called at the end of heat generation and after any
    mutation (scratch, move, add). Also repairs the `_next_stand` bug in the
    flight-builder move route which starts counting from 1 instead of 7 — any
    competitor mis-assigned to a stand outside [7, 8] is pulled back onto
    7 or 8 here.

    Walks heats per (run_number, heat_number). Flips the next-solo stand each
    time a solo is encountered. Runs are balanced independently so run 2
    alternation starts fresh.

    Returns the number of heats whose stand assignment changed.
    """
    if not _is_stock_saw(event):
        return 0

    heats = Heat.query.filter_by(event_id=event.id).order_by(
        Heat.run_number, Heat.heat_number,
    ).all()

    changed_heats = set()
    current_run = None
    next_solo_stand = 7  # flips per solo within a run
    for heat in heats:
        if heat.run_number != current_run:
            current_run = heat.run_number
            next_solo_stand = 7  # reset alternation at run boundary

        comp_ids = heat.get_competitors()
        if not comp_ids:
            continue

        # Completed heats are historical record — their stands match what was
        # actually run, and the score sheet is already keyed to those stands.
        # Walk through them so the alternation counter advances correctly for
        # the NEXT pending heat, but never mutate them. Without this guard a
        # mid-event scratch via services/scratch_cascade.py silently rewrites
        # past stand assignments. (Codex P2 finding, V2.14.15.)
        is_locked = (getattr(heat, 'status', None) or '').lower() == 'completed'

        # Edited in place and written back once at the bottom of the loop body,
        # rather than pushed into the heat a stand at a time.  Same reason as
        # the build loop: a roster write is a rows write now, and this branch
        # can make two of them for one heat.
        assignments = heat.get_stand_assignments()
        heat_changed = False

        if len(comp_ids) == 1:
            sole_id = comp_ids[0]
            try:
                current_stand = int(assignments.get(str(sole_id)) or 0)
            except (TypeError, ValueError):
                current_stand = 0
            if not is_locked and current_stand != next_solo_stand:
                assignments[str(sole_id)] = next_solo_stand
                heat_changed = True
            next_solo_stand = 8 if next_solo_stand == 7 else 7
        else:
            # Pair (or larger): stock saw only has 2 stands, so the first two
            # competitors go to 7 and 8 in comp-order. Extras (shouldn't
            # happen, but don't crash) fall through to the old behaviour.
            desired = [7, 8]
            for i, cid in enumerate(comp_ids[:2]):
                try:
                    current_stand = int(assignments.get(str(cid)) or 0)
                except (TypeError, ValueError):
                    current_stand = 0
                if not is_locked and current_stand != desired[i]:
                    assignments[str(cid)] = desired[i]
                    heat_changed = True

        if heat_changed:
            heat.set_roster(event.event_type, comp_ids, assignments)
            changed_heats.add(heat.id)

    if changed_heats:
        # The rows were written above.  This flush is what sends them, and the
        # `sync_assignments` pass that used to follow it is gone for the same
        # reason it went from the build loop: there is nothing left to copy.
        db.session.flush()

    return len(changed_heats)


def _get_tournament_events(event: Event) -> list:
    """Return all events for the same tournament (cached per generate call)."""
    if not hasattr(_get_tournament_events, '_cache'):
        _get_tournament_events._cache = {}
    tid = event.tournament_id
    if tid not in _get_tournament_events._cache:
        try:
            _get_tournament_events._cache[tid] = Event.query.filter_by(tournament_id=tid).all()
        except RuntimeError:
            # Outside Flask app context (e.g. unit tests with fake events) —
            # return empty list so gear cascade checks are safely skipped.
            return []
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
    all_events = _get_tournament_events(event)
    events_to_check = [event, *get_family_events(event, all_events)]

    # Group entries intentionally name the shared equipment, not another
    # competitor. Compare the raw group token because person-name
    # normalization removes the ``group:`` marker used by the generic helper.
    def group_tokens(comp: dict) -> set[str]:
        tokens = set()
        sharing = comp.get('gear_sharing', {}) or {}
        for key, value in sharing.items():
            token = str(value or '').strip().lower()
            if not token.startswith('group:'):
                continue
            if any(event_matches_gear_key(candidate, key) for candidate in events_to_check):
                tokens.add(token)
        return tokens

    if group_tokens(comp1) & group_tokens(comp2):
        return True

    return competitors_share_gear_for_event(
        str(comp1.get('base_name') or comp1.get('name', '')).strip(),
        comp1.get('gear_sharing', {}) or {},
        str(comp2.get('base_name') or comp2.get('name', '')).strip(),
        comp2.get('gear_sharing', {}) or {},
        event,
        all_events=all_events,
    )


def _delete_event_heats(event_id: int) -> None:
    """Delete all heats for an event, clearing HeatAssignment rows first to satisfy FK constraints."""
    heat_ids = [h.id for h in Heat.query.filter_by(event_id=event_id).with_entities(Heat.id).all()]
    if heat_ids:
        HeatAssignment.query.filter(HeatAssignment.heat_id.in_(heat_ids)).delete(synchronize_session='fetch')
    Heat.query.filter_by(event_id=event_id).delete(synchronize_session='fetch')


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
