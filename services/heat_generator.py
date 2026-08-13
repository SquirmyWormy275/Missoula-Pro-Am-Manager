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

# The venue owns exactly one left-hand-configured springboard dummy and it is
# physically stand 4. This is a fact about the site, not a preference, so it
# lives here as a named constant instead of being spelled as a bare 4 in the
# middle of the assignment loop, which is how it came to be paired with a
# hardcoded [1, 2, 3] for everyone else.
LH_SPRINGBOARD_STAND = 4
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

# Per-event unpaired-partnered-competitor log, populated by
# _build_partner_units() when a partnered-event entrant cannot be paired
# (partner_name blank, unresolved against the event pool, self-reference, or
# nonreciprocal).
# When skip_unpaired=True (default) these competitors are HELD BACK from heat
# generation rather than placed solo on a stand. Routes call
# get_last_unpaired_partnered() after generate_event_heats() to surface a
# warning flash + Preflight resolution prompt.
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

    # The routes normally reject regeneration after scoring, but this service
    # is also used by bulk and background workflows. Keep every caller from
    # deleting completed score or heat history, including an all-scratch heat
    # that has no completed EventResult.
    if event.is_finalized:
        raise HeatGenerationSafetyError(
            f'{event.display_name} is finalized. Heat regeneration is blocked.'
        )
    if EventResult.query.filter_by(event_id=event.id, status='completed').first() is not None:
        raise HeatGenerationSafetyError(
            f'{event.display_name} has scored results. Heat regeneration is blocked.'
        )
    if Heat.query.filter_by(event_id=event.id, status='completed').first() is not None:
        raise HeatGenerationSafetyError(
            f'{event.display_name} has completed heat history. Heat regeneration is blocked.'
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

    # The helpers identify constraints they cannot satisfy without placing two
    # people who share gear in the same heat.  Do this before deleting an
    # existing layout, so an unsafe regeneration leaves that layout intact.
    gear_violations: list[dict] = []
    _last_gear_violations.pop(event.id, None)

    # Per-event left-handed springboard overflow warnings recorded by
    # _generate_springboard_heats when LH count > heat count.
    lh_warnings: list[dict] = []
    _last_lh_overflow_warnings.pop(event.id, None)

    # Per-event unpaired-partnered-competitor list. Populated when a partnered
    # event entrant has a blank, unresolved, self-referential, or nonreciprocal
    # partner_name.
    # Held-back competitors are excluded from heats so a partnered event never
    # has a solo person on a stand. Surfaced via flash + Preflight.
    unpaired_log: list[dict] = []
    _last_unpaired_partnered.pop(event.id, None)

    # Apply special constraints
    if event.stand_type == 'springboard':
        heats = _generate_springboard_heats(competitors, num_heats, max_per_heat, stand_config, event=event,
                                            gear_violations=gear_violations,
                                            lh_warnings=lh_warnings)
    elif event.stand_type in ['saw_hand']:
        heats = _generate_saw_heats(competitors, num_heats, max_per_heat, stand_config, event=event,
                                    gear_violations=gear_violations,
                                    unpaired_log=unpaired_log)
    else:
        heats = _generate_standard_heats(competitors, num_heats, max_per_heat, event=event,
                                         gear_violations=gear_violations,
                                         unpaired_log=unpaired_log)

    forced_conflicts: list[dict] = []
    if gear_violations:
        forced_conflicts = [v for v in gear_violations if not v.get('reason')]
        unplaced = [v for v in gear_violations if v.get('reason')]
        if unplaced:
            names = ', '.join(v.get('comp_name', '') for v in unplaced[:5])
            extra = f' (+{len(unplaced) - 5} more)' if len(unplaced) > 5 else ''
            raise HeatGenerationSafetyError(
                f'Heat generation blocked for {event.display_name}: no safe '
                f'springboard capacity for {names}{extra}. Resolve the roster '
                'before regenerating.'
            )

    # No safety blocker remains, so replacing the current layout is safe.
    _delete_event_heats(event.id)

    # Use actual heat count returned by the generator (saw events recalculate internally).
    actual_heat_count = len(heats)

    # Validate: every competitor must appear in exactly one heat — UNLESS they
    # were intentionally held back as an unpaired partnered-event entrant.
    # Held-back IDs are tracked in unpaired_log so the warning below only fires
    # for genuine snake-draft / gear-conflict misplacements.
    placed_ids = {c['id'] for heat_comps in heats for c in heat_comps}
    held_back_ids = {entry['comp_id'] for entry in unpaired_log}
    expected_ids = {c['id'] for c in competitors}
    missing = (expected_ids - placed_ids) - held_back_ids
    if missing:
        logger.warning(
            'heat_generator: %d competitor(s) not placed in any heat for event %r: %s',
            len(missing), event.display_name, sorted(missing),
        )

    # Create Heat objects
    stand_numbers = _stand_numbers_for_event(event, max_per_heat, stand_config)
    is_partnered = bool(getattr(event, 'is_partnered', False))
    created_heats = []

    # 'pro' or 'college'.  Hoisted above the build loop because each heat now
    # writes its roster once, here, instead of writing JSON here and having a
    # second pass copy it into the rows after the flush.
    comp_type = event.event_type
    for heat_num, heat_competitors in enumerate(heats, start=1):
        heat = Heat(
            event_id=event.id,
            heat_number=heat_num,
            run_number=1
        )
        heat_comp_ids = [c['id'] for c in heat_competitors]

        # Stands accumulate in a plain dict and are written once, with the
        # roster, at the bottom of this block.  They used to be pushed into the
        # heat one at a time.  That was free while `set_stand_assignment` only
        # edited a JSON blob, and stops being free the moment a roster write
        # touches the `heat_assignments` rows: a six-competitor heat would tear
        # its roster down and rebuild it seven times, once per stand, and
        # resolve every competitor's uid again on each pass.  Nothing here needs
        # the intermediate states, so nothing here should be paying for them.
        stands = {}

        # Assign stands.  For partnered events each PAIR shares one stand —
        # both partners receive the same stand number.  Non-partnered events
        # are one competitor per stand as before.
        if is_partnered:
            pair_units = _rebuild_pair_units(heat_competitors, event)
            stand_idx = 0
            for unit in pair_units:
                stand_num = stand_numbers[stand_idx] if stand_idx < len(stand_numbers) else stand_idx + 1
                for comp in unit:
                    stands[comp['id']] = stand_num
                stand_idx += 1
        elif event.stand_type == 'springboard':
            # Phase 5 rule: Dummy 4 is the LH-configured physical dummy. If any
            # competitor in this springboard heat is left-handed, they get
            # stand_number=4; the rest fill the remaining configured stands in
            # competitor-list order. If no LH cutter is in the heat, fall
            # through to the default per-index assignment so stand 4 still gets
            # used.
            #
            # The right-handed stands are derived from the event's own stand
            # list, not hardcoded. The original wrote them as a literal
            # [1, 2, 3], which is only correct when the event runs exactly four
            # stands. Measured on a copy of production: event 31, Pro 1-Board,
            # is configured for five, and generating it with one LH cutter put
            # competitors 12 and 45 both on stand 4 while stand 5 was never
            # emitted. Two people to one springboard, and a block sized for five
            # running four.
            #
            # Latent as the data ships, because no pro currently carries the
            # flag. It arms from a single checkbox on the pro detail form, and
            # nothing downstream checks for a doubled stand.
            lh_comp = next((c for c in heat_competitors if c.get('is_left_handed')), None)
            if lh_comp is not None and LH_SPRINGBOARD_STAND not in stand_numbers:
                # The venue's left-hand dummy is stand 4. If this event is not
                # configured to use stand 4 at all (a specific_stands override,
                # or fewer than four stands), there is no LH dummy to assign and
                # pinning anyone to it would send them to a stand the event is
                # not running. Say so and fall through to the plain assignment
                # rather than silently placing them somewhere wrong.
                if lh_warnings is not None:
                    lh_warnings.append({
                        'type': 'lh_stand_not_in_event_stands',
                        'heat_index': heat_num - 1,
                        'lh_names': [lh_comp.get('name', '')],
                        'event_stands': list(stand_numbers),
                    })
                lh_comp = None
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
                # LH cutter goes on the left-hand dummy.
                stands[lh_comp['id']] = LH_SPRINGBOARD_STAND
                # Everyone else fills the event's other stands in order. Order
                # is preserved rather than sorted so the assignment stays the
                # same as it was for four-stand events, which is the case the
                # crew has run before.
                rh_stands = [s for s in stand_numbers if s != LH_SPRINGBOARD_STAND]
                rh_stand_idx = 0
                overflow_base = (max(stand_numbers) if stand_numbers
                                 else LH_SPRINGBOARD_STAND)
                for comp in heat_competitors:
                    if comp['id'] == lh_comp['id']:
                        continue
                    if rh_stand_idx < len(rh_stands):
                        stand_num = rh_stands[rh_stand_idx]
                    else:
                        # More cutters than the event has stands. The heat is
                        # already over capacity and the admin has a separate
                        # problem, but the numbers still have to be distinct:
                        # the old else-arm here returned the loop index plus
                        # one, which walks straight back over stands the loop
                        # had already handed out.
                        stand_num = (
                            overflow_base + 1 + (rh_stand_idx - len(rh_stands)))
                    stands[comp['id']] = stand_num
                    rh_stand_idx += 1
            else:
                # No LH cutter — plain per-index assignment (stand 4 may still
                # be used by whoever lands in index 3 of heat_competitors).
                for i, comp in enumerate(heat_competitors):
                    stand_num = stand_numbers[i] if i < len(stand_numbers) else i + 1
                    stands[comp['id']] = stand_num
        else:
            for i, comp in enumerate(heat_competitors):
                stand_num = stand_numbers[i] if i < len(stand_numbers) else i + 1
                stands[comp['id']] = stand_num

        heat.set_roster(comp_type, heat_comp_ids, stands)
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
            heat_comp_ids = [c['id'] for c in heat_competitors]
            stands = {}

            # Swap stand assignments for run 2 (e.g., Course 1 <-> Course 2).
            # Reverse only the stands actually used by THIS heat, not the full list.
            if is_partnered:
                pair_units = _rebuild_pair_units(heat_competitors, event)
                stands_needed = len(pair_units)
                run2_stands = list(reversed(stand_numbers[:stands_needed]))
                for unit_idx, unit in enumerate(pair_units):
                    s = run2_stands[unit_idx] if unit_idx < len(run2_stands) else unit_idx + 1
                    for comp in unit:
                        stands[comp['id']] = s
                heat.set_roster(comp_type, heat_comp_ids, stands)
                db.session.add(heat)
                created_heats.append(heat)
                continue
            heat_size = len(heat_competitors)
            run2_stands = list(reversed(stand_numbers[:heat_size]))
            for i, comp in enumerate(heat_competitors):
                stands[comp['id']] = run2_stands[i]

            heat.set_roster(comp_type, heat_comp_ids, stands)
            db.session.add(heat)
            created_heats.append(heat)

    event.status = 'in_progress'
    db.session.flush()

    # The `for heat in created_heats: heat.sync_assignments(comp_type)` pass
    # that used to sit here is gone.  Every heat above already wrote its rows
    # through `set_roster` before it was added to the session, so this loop had
    # nothing left to copy: it re-read the JSON each heat had just been rendered
    # from and handed it straight back, for one uid-resolution query per heat.
    # The flush above is what puts the rows in the table, and it is still here.

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

    # Promote unpaired-partnered competitors so the route can flash + Preflight
    # can offer a resolution UI. These competitors were intentionally HELD BACK
    # from heat generation rather than placed solo on a stand. Each entry has:
    # comp_id, comp_name, partner_name (raw), reason ('blank'|'unresolved'|
    # 'self_reference'|'nonreciprocal').
    if unpaired_log:
        _last_unpaired_partnered[event.id] = list(unpaired_log)
        for u in unpaired_log:
            logger.warning(
                'UNPAIRED PARTNERED ENTRANT: %s (event=%r, partner=%r, reason=%s) '
                '— held back from heats, resolve in Preflight',
                u.get('comp_name', ''), event.display_name,
                u.get('partner_name', ''), u.get('reason', ''),
            )

    if forced_conflicts:
        _last_gear_violations[event.id] = list(forced_conflicts)
        names = ', '.join(v.get('comp_name', '') for v in forced_conflicts[:5])
        extra = f' (+{len(forced_conflicts) - 5} more)' if len(forced_conflicts) > 5 else ''
        logger.warning(
            'FORCED GEAR-SHARING CONFLICT: %s%s (event=%r)',
            names, extra, event.display_name,
        )

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
    (single heat, all heats the same size, or any heat over capacity — the
    last case being intentional springboard LH overflow that must stay pinned
    to the final heat).

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
        #
        # Both passes are bounded by heats EXAMINED, not by steps taken.
        # _advance_snake_index BOUNCES at both boundaries: given (num_heats-1,
        # +1) it returns (num_heats-1, -1), the same index. That bounce is what
        # makes a snake draft a snake draft (0,1,2,2,1,0,0,...) and must stay,
        # but it means a loop bounded by num_heats STEPS spends steps without
        # examining a new heat, so it can run out while a heat still has a free
        # stand. When the fallback ran out that way the unit was dropped with no
        # heat, no gear violation, and nothing the operator could see.
        examined = set()
        while len(examined) < num_heats:
            if heat_idx in examined:
                heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)
                continue
            examined.add(heat_idx)
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
        # Its own `examined` set: this pass must be able to revisit the heats
        # the conflict-avoiding pass already rejected, since rejecting them is
        # the whole reason it is running.
        if not placed:
            examined = set()
            while len(examined) < num_heats:
                if heat_idx in examined:
                    heat_idx, direction = _advance_snake_index(heat_idx, direction, num_heats)
                    continue
                examined.add(heat_idx)
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

    # Re-order so any partial heat closes the event instead of opening it.
    # Remap gear_violations heat indices in-place so the judge's flash points
    # at the heat the competitor actually landed in after the reorder.
    heats, old_to_new = _move_partial_heats_to_end(heats, stands_used, max_per_heat)
    _remap_violation_heat_indices(gear_violations, old_to_new)

    return heats


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

    # Sort LH cutters by ability rank (1 = fastest). When LH_count > num_heats
    # the tail of this list overflows into the final heat — we want the
    # SLOWEST LH cutters to overflow (alongside any slow-heat-flagged cutters
    # already clustering there), not whoever happens to be alphabetically
    # last in registration order. Name-order overflow placement was the
    # original V2.5.0 behaviour; tying the split point to ProEventRank means
    # the fast LH cutters each get their own heat + LH dummy time-slot, and
    # the slow LH cutters share the dedicated slow-heat block.
    # Falls back gracefully to input order when no ranks exist for this
    # tournament + category (that's _sort_by_ability's documented behaviour).
    if left_handed:
        left_handed = _sort_by_ability(left_handed, event)

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

    # Re-order so any partial heat closes the event instead of opening it.
    # Springboard isn't partnered, so competitor count == capacity-relevant size.
    # The helper no-ops when any heat is over capacity (LH overflow stays put).
    # Skip the reorder entirely when slow-heat cutters were placed — the slow
    # cluster is intentionally pinned to the final heat and must not migrate.
    # Remap gear_violations heat indices in-place after the reorder.
    if not slow_heat:
        heats, old_to_new = _move_partial_heats_to_end(
            heats, [len(h) for h in heats], max_per_heat,
        )
        _remap_violation_heat_indices(gear_violations, old_to_new)

    return heats


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
