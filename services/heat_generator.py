"""
Heat generation service using snake draft distribution.
Adapted from STRATHEX tournament_ui.py patterns.
"""
import logging
import math

import config
from config import LIST_ONLY_EVENT_NAMES
from config import event_rank_category as _rank_category_for_event
from database import db
from models import Event, EventResult, Heat, HeatAssignment
from models.competitor import CollegeCompetitor, ProCompetitor
from services.gear_sharing import competitors_share_gear_for_event

logger = logging.getLogger(__name__)
# LIST_ONLY_EVENT_NAMES and _rank_category_for_event imported from config above.

# Per-event gear-sharing violation log populated by the snake-draft fallbacks.
# Routes call get_last_gear_violations(event.id) after generate_event_heats() to
# surface a warning flash to the judge (gear audit fix G2/G3 — 2026-04-07).
_last_gear_violations: dict[int, list[dict]] = {}

# Per-event left-handed springboard overflow log, populated by
# _generate_springboard_heats() when LH cutter count exceeds heat count.
# Separate from gear violations because the remediation is different
# (reconfigure field sizes vs. rebuild gear pairs).
_last_lh_overflow_warnings: dict[int, list[dict]] = {}


def get_last_gear_violations(event_id: int) -> list[dict]:
    """Return the gear-sharing violations recorded by the most recent
    generate_event_heats(event) call for this event_id, or an empty list."""
    return list(_last_gear_violations.get(event_id, []))


def get_last_lh_overflow_warnings(event_id: int) -> list[dict]:
    """Return the left-handed springboard overflow warnings recorded by the
    most recent generate_event_heats(event) call for this event_id, or an
    empty list.  Each entry is a dict with keys type, heat_index,
    overflow_count, overflow_names."""
    return list(_last_lh_overflow_warnings.get(event_id, []))


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

    # Prelim-based events (Partnered Axe Throw) are managed by a dedicated
    # state machine (services.partnered_axe.PartneredAxeThrow), not the
    # standard snake-draft generator. Skip so we don't produce one-pair-per-heat
    # output that bypasses the prelim/final flow.
    if getattr(event, 'has_prelims', False):
        _delete_event_heats(event.id)
        event.status = 'pending'
        db.session.flush()
        return 0

    # Get stand configuration; event.max_stands is authoritative when set
    stand_config = config.STAND_CONFIGS.get(event.stand_type, {})
    max_per_heat = event.max_stands if event.max_stands is not None else stand_config.get('total', 4)
    if max_per_heat is None or int(max_per_heat) <= 0:
        raise ValueError(
            f"{event.display_name} has invalid max_stands={max_per_heat}. "
            'Set max_stands to at least 1 before generating heats.'
        )
    max_per_heat = int(max_per_heat)

    # Calculate number of heats needed
    num_heats = math.ceil(len(competitors) / max_per_heat)

    # Clear existing heats
    _delete_event_heats(event.id)

    # Per-event gear-sharing fallback violations recorded by the snake-draft
    # helpers.  Entries are dicts with keys: comp_id, comp_name, heat_index.
    # Cleared on every generate_event_heats() call (gear audit fix G2/G3).
    gear_violations: list[dict] = []
    _last_gear_violations.pop(event.id, None)

    # Per-event left-handed springboard overflow warnings recorded by
    # _generate_springboard_heats when LH count > heat count.
    lh_warnings: list[dict] = []
    _last_lh_overflow_warnings.pop(event.id, None)

    # Apply special constraints
    if event.stand_type == 'springboard':
        heats = _generate_springboard_heats(competitors, num_heats, max_per_heat, stand_config, event=event,
                                            gear_violations=gear_violations,
                                            lh_warnings=lh_warnings)
    elif event.stand_type in ['saw_hand']:
        heats = _generate_saw_heats(competitors, num_heats, max_per_heat, stand_config, event=event,
                                    gear_violations=gear_violations)
    else:
        heats = _generate_standard_heats(competitors, num_heats, max_per_heat, event=event,
                                         gear_violations=gear_violations)

    # Use actual heat count returned by the generator (saw events recalculate internally).
    actual_heat_count = len(heats)

    # Validate: every competitor must appear in exactly one heat.
    placed_ids = {c['id'] for heat_comps in heats for c in heat_comps}
    expected_ids = {c['id'] for c in competitors}
    missing = expected_ids - placed_ids
    if missing:
        logger.warning(
            'heat_generator: %d competitor(s) not placed in any heat for event %r: %s',
            len(missing), event.display_name, sorted(missing),
        )

    # Create Heat objects
    stand_numbers = _stand_numbers_for_event(event, max_per_heat, stand_config)
    is_partnered = bool(getattr(event, 'is_partnered', False))
    created_heats = []
    for heat_num, heat_competitors in enumerate(heats, start=1):
        heat = Heat(
            event_id=event.id,
            heat_number=heat_num,
            run_number=1
        )
        heat.set_competitors([c['id'] for c in heat_competitors])

        # Assign stands.  For partnered events each PAIR shares one stand —
        # both partners receive the same stand number.  Non-partnered events
        # are one competitor per stand as before.
        if is_partnered:
            pair_units = _rebuild_pair_units(heat_competitors, event)
            stand_idx = 0
            for unit in pair_units:
                stand_num = stand_numbers[stand_idx] if stand_idx < len(stand_numbers) else stand_idx + 1
                for comp in unit:
                    heat.set_stand_assignment(comp['id'], stand_num)
                stand_idx += 1
        elif event.stand_type == 'springboard':
            # Phase 5 rule: Dummy 4 is the LH-configured physical dummy. If any
            # competitor in this springboard heat is left-handed, they get
            # stand_number=4; others fill stands 1-3 in competitor-list order.
            # If no LH cutter is in the heat, fall through to the default
            # per-index assignment so stand 4 still gets used.
            lh_comp = next((c for c in heat_competitors if c.get('is_left_handed')), None)
            if lh_comp is not None:
                # Surface a heat-level warning if the heat has more than one LH
                # cutter (overflow scenario) — only the first gets stand 4, the
                # rest fall back to list-order assignment and will physically
                # collide. This is rare but possible if LH_count > heat_count.
                lh_comps_in_heat = [c for c in heat_competitors if c.get('is_left_handed')]
                if len(lh_comps_in_heat) > 1 and lh_warnings is not None:
                    lh_warnings.append({
                        'type': 'multiple_lh_same_heat',
                        'heat_index': heat_num - 1,
                        'lh_count': len(lh_comps_in_heat),
                        'lh_names': [c.get('name', '') for c in lh_comps_in_heat],
                    })
                # LH cutter goes on stand 4.
                heat.set_stand_assignment(lh_comp['id'], 4)
                # Fill stands 1, 2, 3 for the remaining cutters in order.
                rh_stand_idx = 0
                rh_stands = [1, 2, 3]
                for comp in heat_competitors:
                    if comp['id'] == lh_comp['id']:
                        continue
                    stand_num = (
                        rh_stands[rh_stand_idx]
                        if rh_stand_idx < len(rh_stands)
                        else rh_stand_idx + 1
                    )
                    heat.set_stand_assignment(comp['id'], stand_num)
                    rh_stand_idx += 1
            else:
                # No LH cutter — plain per-index assignment (stand 4 may still
                # be used by whoever lands in index 3 of heat_competitors).
                for i, comp in enumerate(heat_competitors):
                    stand_num = stand_numbers[i] if i < len(stand_numbers) else i + 1
                    heat.set_stand_assignment(comp['id'], stand_num)
        else:
            for i, comp in enumerate(heat_competitors):
                stand_num = stand_numbers[i] if i < len(stand_numbers) else i + 1
                heat.set_stand_assignment(comp['id'], stand_num)

        db.session.add(heat)
        created_heats.append(heat)

    # For dual-run events, create second run heats
    if event.requires_dual_runs:
        for heat_num, heat_competitors in enumerate(heats, start=1):
            heat = Heat(
                event_id=event.id,
                heat_number=heat_num,
                run_number=2
            )
            heat.set_competitors([c['id'] for c in heat_competitors])

            # Swap stand assignments for run 2 (e.g., Course 1 <-> Course 2).
            # Reverse only the stands actually used by THIS heat, not the full list.
            if is_partnered:
                pair_units = _rebuild_pair_units(heat_competitors, event)
                stands_needed = len(pair_units)
                run2_stands = list(reversed(stand_numbers[:stands_needed]))
                for unit_idx, unit in enumerate(pair_units):
                    s = run2_stands[unit_idx] if unit_idx < len(run2_stands) else unit_idx + 1
                    for comp in unit:
                        heat.set_stand_assignment(comp['id'], s)
                db.session.add(heat)
                created_heats.append(heat)
                continue
            heat_size = len(heat_competitors)
            run2_stands = list(reversed(stand_numbers[:heat_size]))
            for i, comp in enumerate(heat_competitors):
                heat.set_stand_assignment(comp['id'], run2_stands[i])

            db.session.add(heat)
            created_heats.append(heat)

    event.status = 'in_progress'
    db.session.flush()

    comp_type = event.event_type  # 'pro' or 'college'
    for heat in created_heats:
        heat.sync_assignments(comp_type)

    # Promote any fallback gear-sharing violations recorded by the snake-draft
    # helpers into the module-level lookup so the route layer can surface a
    # WARNING flash to the judge (gear audit fix G2/G3 — 2026-04-07).  Each
    # violation's heat_index is mapped to the freshly created Heat row's id.
    if gear_violations:
        resolved: list[dict] = []
        for v in gear_violations:
            idx = v.get('heat_index')
            heat_id = None
            heat_number = None
            if isinstance(idx, int) and 0 <= idx < len(created_heats):
                heat_id = created_heats[idx].id
                heat_number = created_heats[idx].heat_number
            resolved.append({
                'comp_id': v.get('comp_id'),
                'comp_name': v.get('comp_name', ''),
                'heat_id': heat_id,
                'heat_number': heat_number,
            })
            logger.warning(
                'GEAR CONFLICT FORCED: %s placed in heat %s — manual review required',
                v.get('comp_name', ''), heat_id,
            )
        _last_gear_violations[event.id] = resolved

    # Promote LH overflow warnings for springboard events, same pattern.
    if lh_warnings:
        resolved_lh: list[dict] = []
        for w in lh_warnings:
            idx = w.get('heat_index')
            heat_id = None
            heat_number = None
            if isinstance(idx, int) and 0 <= idx < len(created_heats):
                heat_id = created_heats[idx].id
                heat_number = created_heats[idx].heat_number
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
    if event.event_type == 'college':
        all_comps = CollegeCompetitor.query.filter_by(
            tournament_id=event.tournament_id,
            status='active'
        ).all()
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
            'partner_name': _get_partner_name_for_event(comp, event)
        }
        if event.event_type == 'pro':
            comp_data['is_slow_springboard'] = bool(getattr(comp, 'springboard_slow_heat', False))

        competitors.append(comp_data)

    db.session.flush()
    return competitors


def _generate_standard_heats(competitors: list, num_heats: int, max_per_heat: int, event: Event = None,
                              gear_violations: list | None = None) -> list:
    """
    Generate heats using snake draft distribution.

    Snake draft ensures each heat has a mix of skill levels.

    For partnered events, each unit (a pair) occupies ONE stand. `max_per_heat`
    therefore counts STANDS, not individual competitors, and num_heats is
    recomputed from unit count so we don't over-allocate empty heats.
    """
    competitors = _sort_by_ability(competitors, event)
    units = _build_partner_units(competitors, event)
    # Re-sort partner units by composite rank so paired competitors enter the
    # snake draft in the right ability order (#22).
    units = _sort_units_by_ability(units, event)

    is_partnered = bool(event and getattr(event, 'is_partnered', False))

    # For partnered events, num_heats must be recomputed from unit count:
    # each pair takes 1 stand (not 2 competitor slots).  For solo events, the
    # unit count equals the competitor count so this is a no-op.
    if is_partnered:
        num_heats = max(1, math.ceil(len(units) / max_per_heat))

    heats = [[] for _ in range(num_heats)]
    stands_used = [0] * num_heats  # count of stands (units) per heat

    # Snake draft distribution
    direction = 1
    heat_idx = 0

    for unit in units:
        placed = False

        # First pass: look for a heat with capacity and no gear-sharing conflict.
        for _ in range(num_heats):
            if (
                (stands_used[heat_idx] + 1) <= max_per_heat and
                not any(_has_gear_sharing_conflict(comp, heats[heat_idx], event) for comp in unit)
            ):
                heats[heat_idx].extend(unit)
                stands_used[heat_idx] += 1
                placed = True
                break
            heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

        # Fallback: place despite conflict if every heat conflicts/full.
        # Record any gear-sharing conflict introduced here so the caller can
        # surface a warning to the judge (gear audit fix G2 — 2026-04-07).
        if not placed:
            for _ in range(num_heats):
                if (stands_used[heat_idx] + 1) <= max_per_heat:
                    if gear_violations is not None:
                        for comp in unit:
                            if _has_gear_sharing_conflict(comp, heats[heat_idx], event):
                                gear_violations.append({
                                    'comp_id': comp.get('id'),
                                    'comp_name': comp.get('name', ''),
                                    'heat_index': heat_idx,
                                })
                    heats[heat_idx].extend(unit)
                    stands_used[heat_idx] += 1
                    placed = True
                    break
                heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

        heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

    return heats


def _first_token(value: str) -> str:
    """Return the first whitespace-separated token of a normalized name."""
    value = _norm_name(value or '')
    return value.split(' ', 1)[0] if value else ''


def _find_partner(partner_name: str, pool: list, self_comp: dict) -> dict | None:
    """Best-effort partner match against a pool of competitors.

    1. Exact full-name (normalized) match.
    2. First-name fuzzy match, but only if exactly ONE pool competitor shares
       that first name (ambiguous first names do NOT pair — avoids wrong pairs).

    `self_comp` is excluded from the match pool. Returns the matched competitor
    dict or None.
    """
    if not partner_name:
        return None
    norm_partner = _norm_name(partner_name)
    if not norm_partner:
        return None
    self_id = self_comp.get('id')

    def _key(c):
        return _norm_name(c.get('base_name') or c.get('name'))

    # Exact match first.
    for c in pool:
        if c.get('id') == self_id:
            continue
        if _key(c) == norm_partner:
            return c

    # First-name fallback. Partner string is often "TOBY" or "Greer" — match to
    # exactly one competitor whose first name matches, else give up (ambiguous).
    partner_first = _first_token(partner_name)
    if not partner_first:
        return None
    first_matches = [c for c in pool
                     if c.get('id') != self_id
                     and _first_token(c.get('base_name') or c.get('name')) == partner_first]
    if len(first_matches) == 1:
        return first_matches[0]
    return None


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
        if partner and partner['id'] not in used:
            units.append([comp, partner])
            used.add(comp['id'])
            used.add(partner['id'])
            continue
        units.append([comp])
        used.add(comp['id'])
    return units


def _build_partner_units(competitors: list, event: Event) -> list:
    """Build assignment units; partnered events keep recognized pairs together.

    Uses `_find_partner` so nicknames and first-name-only partner strings pair
    correctly when unambiguous within the event pool.
    """
    if not event or not event.is_partnered:
        return [[c] for c in competitors]

    used = set()
    units = []

    for comp in competitors:
        comp_id = comp['id']
        if comp_id in used:
            continue

        partner = _find_partner(comp.get('partner_name'), competitors, comp)
        if partner and partner['id'] not in used:
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
                                 max_per_heat: int, stand_config: dict, event: Event = None,
                                 gear_violations: list | None = None,
                                 lh_warnings: list | None = None) -> list:
    """
    Generate springboard heats with left-handed cutter spreading.

    Only one physical left-handed springboard dummy exists on site, so at most
    ONE left-handed cutter can be in a single heat at a time.  Spread LH cutters
    one per heat across heats 0..N-1.  If more LH cutters than heats exist,
    overflow into the FINAL heat (per user rule, 2026-04-20) and log a warning
    via lh_warnings so the admin knows there is dummy contention.

    Slow-heat cutters still cluster starting at the final heat (unchanged).
    """
    heats = [[] for _ in range(num_heats)]

    # Dedicated springboard buckets:
    # - LH cutters: one per heat (spread), overflow to final heat with warning.
    # - Slow-heat cutters: cluster into the dedicated slow heat.
    left_handed = [c for c in competitors if c.get('is_left_handed', False)]
    slow_heat = [c for c in competitors if c.get('is_slow_springboard', False)]

    slow_heat_idx = (num_heats - 1) if slow_heat else None

    assigned_ids = set()

    # --- LH spread ---
    # One LH cutter per heat, heats 0..N-1.  Overflow spills into the final
    # heat (heats[num_heats-1]), mixed with RH cutters there.  If the final
    # heat also hits max_per_heat, any further LH cutters are unplaceable —
    # surface them via gear_violations as a hard warning so the admin reacts.
    if left_handed and num_heats > 0:
        spread = left_handed[:num_heats]
        overflow = left_handed[num_heats:]

        for i, lh in enumerate(spread):
            if len(heats[i]) < max_per_heat:
                heats[i].append(lh)
                assigned_ids.add(lh['id'])

        if overflow:
            final_idx = num_heats - 1
            placed_overflow: list[str] = []
            unplaced_overflow: list[str] = []
            for lh in overflow:
                if lh['id'] in assigned_ids:
                    continue
                if len(heats[final_idx]) < max_per_heat:
                    heats[final_idx].append(lh)
                    assigned_ids.add(lh['id'])
                    placed_overflow.append(lh.get('name', ''))
                else:
                    unplaced_overflow.append(lh.get('name', ''))

            if placed_overflow and lh_warnings is not None:
                lh_warnings.append({
                    'type': 'lh_overflow',
                    'heat_index': final_idx,
                    'overflow_count': len(placed_overflow),
                    'overflow_names': placed_overflow,
                })
            if unplaced_overflow and gear_violations is not None:
                for name in unplaced_overflow:
                    gear_violations.append({
                        'comp_id': None,
                        'comp_name': name,
                        'heat_index': final_idx,
                        'reason': 'LH cutter unplaced — all heats at capacity',
                    })

    # --- Slow-heat cluster (unchanged behavior) ---
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
        # First pass: find a heat with capacity AND no gear-sharing conflict.
        # Springboards are the highest-stakes shared-equipment event, so this
        # check matches the standard heat generator (gear audit fix G3).
        placed = False
        for _ in range(num_heats):
            if (
                len(heats[heat_idx]) < max_per_heat and
                not _has_gear_sharing_conflict(comp, heats[heat_idx], event)
            ):
                heats[heat_idx].append(comp)
                placed = True
                break
            heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

        # Fallback: place despite conflict if every heat conflicts/full.
        # Record any gear-sharing conflict introduced here so the caller can
        # surface a warning to the judge (gear audit fix G3 — 2026-04-07).
        if not placed:
            for _ in range(num_heats):
                if len(heats[heat_idx]) < max_per_heat:
                    if gear_violations is not None and _has_gear_sharing_conflict(comp, heats[heat_idx], event):
                        gear_violations.append({
                            'comp_id': comp.get('id'),
                            'comp_name': comp.get('name', ''),
                            'heat_index': heat_idx,
                        })
                    heats[heat_idx].append(comp)
                    placed = True
                    break
                heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

        if not placed:
            break
        heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)

    return heats


def _generate_saw_heats(competitors: list, num_heats: int,
                        max_per_heat: int, stand_config: dict, event: Event = None,
                        gear_violations: list | None = None) -> list:
    """
    Generate saw heats respecting stand group constraints.

    Saw stands are in groups of 4. One group runs while the other sets up.
    """
    # Standard snake draft, but limit to 4 per heat for saw events
    actual_max = min(max_per_heat, 4)  # Saw groups are 4 each
    num_heats = math.ceil(len(competitors) / actual_max)

    return _generate_standard_heats(competitors, num_heats, actual_max, event=event,
                                    gear_violations=gear_violations)


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
