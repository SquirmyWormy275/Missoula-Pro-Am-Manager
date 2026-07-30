"""
Scoring Engine — authoritative scoring logic for all event types.

This module is the single source of truth for:
  - Position calculation with per-event tiebreak rules
  - Score outlier flagging
  - Tie detection and throw-off management
  - Team point recalculation
  - Individual and team standings
  - Payout template CRUD

Tiebreak rules (applied inside _calculate_positions):
  1. Default      — combined sum of run1 + run2 (lowest sum wins for time, highest for score)
  2. Hard-Hit     — primary: hits (highest_wins); tiebreak: tiebreak_value time (lowest_wins)
  3. Axe Throw    — primary: cumulative score (highest_wins); tie → throwoff_pending = True;
                    judge enters throw-off positions via record_throwoff_result()

Dual-run best-run fix:
  - lowest_wins  → min(run1, run2)   Speed Climb, Chokerman, time-based
  - highest_wins → max(run1, run2)   Caber Toss (distance)
"""
from __future__ import annotations

import csv
import io
import logging
import statistics
from decimal import Decimal
from typing import Optional

from sqlalchemy import func

from database import db
from models.competitor import CollegeCompetitor, ProCompetitor
from models.event import Event, EventResult
from models.payout_template import PayoutTemplate
from models.team import Team
from services.audit import log_action

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 3 (V2.8.0) — split-tie points table and helper.
# ---------------------------------------------------------------------------
#
# AWFC tie-split rule (per ProAM requirements + PLAN_REVIEW.md v3):
#
#   "Ties: split the combined points equally.
#    Two tied for 5th: each gets (2 + 1) / 2 = 1.5 points.
#    Three tied for 1st: each gets (10 + 7 + 5) / 3 = 7.33... points."
#
# This means tied competitors collectively occupy the positions starting at
# their shared rank, the points for those positions are summed, and the sum
# is divided evenly across all tied competitors.
#
# All math is done in Decimal so the final value is exact and can be stored
# in the Numeric(6,2) points_awarded column from Phase 1B.  Use Decimal('0.01')
# as the quantize step (two decimal places) — matches the column precision.

PLACEMENT_POINTS_DECIMAL = [
    Decimal('10'),  # 1st
    Decimal('7'),   # 2nd
    Decimal('5'),   # 3rd
    Decimal('3'),   # 4th
    Decimal('2'),   # 5th
    Decimal('1'),   # 6th
]
_QUANTIZE_2DP = Decimal('0.01')


def split_tie_points(start_rank: int, count: int) -> Decimal:
    """Return the per-competitor points for `count` competitors tied at `start_rank`.

    Examples:
      split_tie_points(1, 1) -> Decimal('10')      (solo 1st place)
      split_tie_points(1, 2) -> Decimal('8.5')     (two tied for 1st: (10+7)/2)
      split_tie_points(1, 3) -> Decimal('7.33')    (three tied for 1st: (10+7+5)/3)
      split_tie_points(2, 2) -> Decimal('6')       (two tied for 2nd: (7+5)/2)
      split_tie_points(5, 2) -> Decimal('1.5')     (two tied for 5th: (2+1)/2)
      split_tie_points(7, 1) -> Decimal('0')       (7th and beyond = 0)
      split_tie_points(6, 3) -> Decimal('0.33')    (6th + two off-table: (1+0+0)/3)

    Positions beyond 6th place receive 0 points.  When some tied competitors
    spill past the table boundary, the points sum still divides over the full
    `count` of tied competitors — that's the spec, every tied competitor gets
    the same share, even those who would have placed off the table individually.
    """
    if count <= 0:
        return Decimal('0')
    end_rank = start_rank + count  # exclusive
    total = Decimal('0')
    for rank in range(start_rank, end_rank):
        idx = rank - 1  # 0-indexed table lookup
        if 0 <= idx < len(PLACEMENT_POINTS_DECIMAL):
            total += PLACEMENT_POINTS_DECIMAL[idx]
        # else: rank is off the points table, contributes 0
    return (total / Decimal(count)).quantize(_QUANTIZE_2DP)


def _rebuild_individual_points(competitor_ids: list[int]) -> None:
    """Rebuild CollegeCompetitor.individual_points from EventResult.points_awarded.

    For each competitor in `competitor_ids`, sets individual_points to the SUM
    of points_awarded across ALL their finalized event results.  This is the
    "rebuild from source of truth" approach mandated by PLAN_REVIEW.md A6 and
    C2 — replaces the old strip-then-add pattern that diverged from the
    record_throwoff_result code path.

    Uses one batched GROUP BY query so it's O(1) round-trips instead of N.
    """
    if not competitor_ids:
        return
    # Compute the SUM for every requested competitor in a single query.
    sums = dict(
        db.session.query(
            EventResult.competitor_id,
            func.coalesce(func.sum(EventResult.points_awarded), 0),
        )
        .filter(
            EventResult.competitor_id.in_(competitor_ids),
            EventResult.competitor_type == 'college',
        )
        .group_by(EventResult.competitor_id)
        .all()
    )
    # Apply to the in-session competitor objects so the rebuild participates
    # in the current transaction.
    rows = (
        CollegeCompetitor.query
        .filter(CollegeCompetitor.id.in_(competitor_ids))
        .all()
    )
    for comp in rows:
        new_total = sums.get(comp.id, 0)
        comp.individual_points = Decimal(new_total) if not isinstance(new_total, Decimal) else new_total


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _metric(result: EventResult, event: Event) -> Optional[float]:
    """Return the primary ranked metric for a result row.

    For handicap-format events (event.is_handicap is True), the competitor's
    start mark (stored in result.handicap_factor as seconds) is subtracted from
    the raw time to produce the adjusted net time used for ranking.
    A handicap_factor of None or 0.0 means scratch (no start mark).
    """
    if event.requires_dual_runs:
        raw = result.best_run
    else:
        raw = result.result_value

    if raw is None:
        return None

    # Apply handicap start mark: net_time = raw_time - start_mark_seconds.
    # handicap_factor stores the start mark in seconds; 0.0 or None = scratch.
    if getattr(event, 'is_handicap', False) and event.scoring_type == 'time':
        start_mark = result.handicap_factor
        if start_mark is None:
            start_mark = 0.0
        raw = max(0.0, raw - start_mark)

    return raw


def _tiebreak_metric(result: EventResult, event: Event) -> float:
    """Return the secondary tiebreak metric.

    Hard-Hit: tiebreak_value (elapsed time, lowest wins).
    Default:  combined run1 + run2 sum (direction = scoring_order).
    """
    if event.is_hard_hit:
        # Lower time is better; None floats to worst
        return result.tiebreak_value if result.tiebreak_value is not None else float('inf')

    # Default: combined sum of both runs (only meaningful for dual-run events)
    r1 = result.run1_value or 0.0
    r2 = result.run2_value or 0.0
    combined = r1 + r2
    # For lowest_wins the smaller combined time wins (return as-is).
    # For highest_wins a larger combined is better — negate so sort ascending still works.
    if event.scoring_order == 'highest_wins':
        return -combined
    return combined


def _sort_key(result: EventResult, event: Event):
    """Composite sort key: (primary, tiebreak). Always sorts ascending."""
    primary = _metric(result, event)
    if primary is None:
        primary = float('inf') if event.scoring_order == 'lowest_wins' else float('-inf')

    if event.scoring_order == 'highest_wins':
        primary = -primary  # negate so ascending sort gives correct rank

    tiebreak = _tiebreak_metric(result, event)
    return (primary, tiebreak)


def _pair_key_for(result: EventResult, is_partnered: bool):
    """The competing unit a result row belongs to.

    On a partnered event the pair is the unit, not the person: both member rows
    carry the pair's single score, so they must collapse to one key.  frozenset
    is symmetric, so the two row orderings land on the same key.  On a solo
    event every row is its own unit.

    Same idiom as the three inline copies further down this file (the position
    loop, the payout split, the points split).  Those are left alone
    deliberately: they work, and rewriting working position math to share a
    helper buys nothing and risks a regression on show day.
    """
    if is_partnered:
        return frozenset((result.competitor_name, result.partner_name))
    return result.competitor_id


def _detect_axe_ties(results: list[EventResult],
                     event: Event | None = None) -> list[list[EventResult]]:
    """Return groups of rows contesting the same position (axe throw tie).

    A bucket of rows sharing a result_value is only a tie if it holds two or
    more distinct COMPETING UNITS.  On a partnered event it usually does not.
    services/partnered_axe.py writes the pair's one score to both member rows
    (_sync_prelim_to_event_results and _save_event_results), so every pair is
    automatically a two-row bucket at whatever score it earned, tie or no tie.
    Counting rows therefore declared every pair tied with itself: five pairs at
    five distinct scores produced ten flagged rows and a red "Throw-Off
    Required" banner over a result set containing no tie at all.

    The event argument is optional so that callers passing a bare list keep
    working; without it the row count is the unit count, which is the
    pre-existing behaviour and is correct for solo events.
    """
    from itertools import groupby
    is_partnered = bool(getattr(event, 'is_partnered', False)) if event else False
    groups = []
    sorted_results = sorted(results, key=lambda r: r.result_value or 0, reverse=True)
    for _val, group in groupby(sorted_results, key=lambda r: r.result_value):
        group_list = list(group)
        if len({_pair_key_for(r, is_partnered) for r in group_list}) >= 2:
            groups.append(group_list)
    return groups


def throwoff_groups(event: Event) -> list[dict]:
    """Pending throw-offs shaped so the judge cannot submit a corrupting form.

    One entry per contested block of places:

        {'positions': [3, 4],
         'entries': [{'label': 'A & B', 'result_ids': [10, 250],
                      'score': 24.0, 'current_position': 3}, ...]}

    Three defects in the throw-off form this shape closes.

    First, the form offered positions 1..N where N was the number of pending
    rows, so a tie for 3rd and 4th could only be answered "Position 1" or
    "Position 2".  The contested places are the ones the tied units already
    hold: calculate_positions gives every unit in a tied group the same
    position, so a two-way tie at 3rd resolves into 3 and 4.

    Second, no option carried the selected attribute, so every select rendered
    on "Position 1".  Submitting the form as rendered wrote final_position=1 to
    every pending row, and the public spectator page then showed ten
    competitors in first place.  Each select now starts on the position that
    unit already holds.  Tied units all hold the SAME position, so an untouched
    submit is a set of duplicates and validate_throwoff_submission refuses it
    with a message naming the entries that still need separating.  Refusing is
    the right outcome rather than a silent no-op: the throw-off exists because
    a judge has to make a call, and a form that quietly accepts "no call made"
    would leave the tie unresolved behind a success message.  Pre-filling
    distinct places instead would be worse still, because it would dress an
    arbitrary ordering up as a judge's decision and one careless click would
    publish it.

    Third, on a partnered event the pair is the competing unit and gets ONE
    select covering both member rows.  Per-row selects let a judge hand the two
    halves of a pair different finishing positions.
    """
    pending = pending_throwoffs(event)
    if not pending:
        return []

    is_partnered = bool(getattr(event, 'is_partnered', False))
    groups = []
    for bucket in _detect_axe_ties(pending, event):
        by_unit: dict = {}
        for r in bucket:
            by_unit.setdefault(_pair_key_for(r, is_partnered), []).append(r)

        entries = []
        for rows in by_unit.values():
            first = rows[0]
            if is_partnered and first.partner_name:
                label = f'{first.competitor_name} & {first.partner_name}'
            else:
                label = first.competitor_name
            entries.append({
                'label': label,
                'result_ids': sorted(r.id for r in rows),
                'score': first.result_value,
                'current_position': first.final_position,
            })
        entries.sort(key=lambda e: (e['current_position'] or 0, e['label']))

        # The block of places these units are fighting over.  Every unit in a
        # tied group leaves calculate_positions on the same position, so the
        # block runs from there for as many units as there are.
        held = [e['current_position'] for e in entries if e['current_position']]
        start = min(held) if held else 1
        groups.append({
            'positions': list(range(start, start + len(entries))),
            'entries': entries,
        })

    groups.sort(key=lambda g: g['positions'][0])
    return groups


def _state_machine_ranking(event: Event) -> list[list[int]] | None:
    """Ranked competitor ids for events whose finishing order is owned by a
    state machine, or ``None`` when the ordinary result_value sort is correct.

    Returned as a list of tiers, best first, each tier holding the competitor
    ids that share one position.  A partnered pair is one tier of two ids.

    Why this exists.  Partnered Axe Throw runs prelims scored on total hits and
    then a four-pair final scored on its own scale.  Both stages write into the
    same ``event_results.result_value`` column, so once the final lands that
    column holds finals numbers for the four finalists and prelim numbers for
    the pairs that were knocked out.  Re-sorting the column mixes the two
    scales, which is how a pair eliminated in prelims can be handed a better
    finishing position than the pair that actually won the event.  The true
    order is not in that column at all; it is in ``event_state``.  Read it from
    there.

    Only the ``completed`` stage is intercepted.  Before the final is scored,
    every row still carries its prelim number, the scales match, and the
    ordinary sort is already right.  Intercepting earlier would replace working
    behaviour for no gain.
    """
    if not getattr(event, 'has_prelims', False):
        return None
    # Guard: the college branch of calculate_positions rebuilds individual and
    # team point caches, which this shortcut does not do.  No college event
    # sets has_prelims today, and if one ever does it must not land here
    # silently.
    if event.event_type == 'college':
        logger.warning(
            'scoring_engine: event %s has prelims but is a college event; '
            'falling through to the ordinary sort', event.id
        )
        return None

    try:
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(event)
        if pat.get_stage() != 'completed':
            return None

        finalists = pat.get_finalists()
        finalist_ids = {p.get('pair_id') for p in finalists}
        ranked_pairs = sorted(
            (p for p in finalists if p.get('final_position') is not None),
            key=lambda p: p['final_position'],
        )
        # Everyone who did not make the final is ranked behind every finalist,
        # in prelim order.  get_prelim_standings() is already sorted by hits.
        ranked_pairs.extend(
            p for p in pat.get_prelim_standings()
            if p.get('pair_id') not in finalist_ids
        )
    except Exception:
        # Corrupt or half-written state must not take finalization down on
        # show day.  Fall through to the ordinary sort, which is wrong in the
        # specific way documented above but is at least the behaviour the
        # judges already know.
        logger.exception(
            'scoring_engine: could not read state-machine standings for event %s',
            event.id,
        )
        return None

    tiers = []
    for pair in ranked_pairs:
        tier = []
        for key in ('competitor1', 'competitor2'):
            member = pair.get(key) or {}
            if member.get('id') is not None:
                tier.append(member['id'])
        if tier:
            tiers.append(tier)
    return tiers or None


def _apply_state_machine_ranking(event: Event, completed: list[EventResult],
                                 tiers: list[list[int]]) -> None:
    """Write positions and payouts from a state machine's ranking.

    Payouts still come from ``event.get_payout_for_position``, exactly as the
    ordinary path does, so this changes finishing order and nothing about how
    money is computed.
    """
    position_by_competitor = {}
    for index, tier in enumerate(tiers, 1):
        for competitor_id in tier:
            position_by_competitor[competitor_id] = index

    comp_lookup = {
        c.id: c for c in ProCompetitor.query.filter(
            ProCompetitor.id.in_([r.competitor_id for r in completed])
        ).all()
    }

    ranked = [r for r in completed if r.competitor_id in position_by_competitor]
    unranked = [r for r in completed if r.competitor_id not in position_by_competitor]

    def _award(result, position):
        payout = event.get_payout_for_position(position)
        result.final_position = position
        result.payout_amount = payout
        comp = comp_lookup.get(result.competitor_id)
        if comp:
            comp.total_earnings += payout

    for result in ranked:
        _award(result, position_by_competitor[result.competitor_id])

    # A result row for somebody the state machine never heard of should not be
    # dropped on the floor, and it must not outrank a real pair either.  Rank
    # it behind everyone, in the ordinary order, and say so in the log.
    if unranked:
        logger.warning(
            'scoring_engine: event %s has %d completed result(s) with no pair '
            'in event_state; ranking them behind every pair',
            event.id, len(unranked),
        )
        next_position = len(tiers) + 1
        for result in sorted(unranked, key=lambda r: _sort_key(r, event)):
            _award(result, next_position)
            next_position += 1

    flag_score_outliers(completed, event)
    event.status = 'completed'
    event.is_finalized = True


# ---------------------------------------------------------------------------
# Public: position calculation
# ---------------------------------------------------------------------------

def calculate_positions(event: Event) -> None:
    """
    Calculate final positions and award points/payouts for event.

    Phase 3 (V2.8.0) rewrite:
      * College ties are split per the AWFC rule (see split_tie_points).
      * College individual_points and team total_points are REBUILT from
        SUM(points_awarded) after the per-result writes, replacing the old
        strip-then-add pattern.  This makes the function fully idempotent and
        keeps record_throwoff_result() consistent with calculate_positions().
      * Pro events keep the same payout_amount accumulation logic — total_earnings
        for pro competitors still uses Float, not Decimal.

    Raises nothing; caller should catch StaleDataError/IntegrityError and rollback.
    """
    logger.info('scoring_engine: calculate_positions event_id=%s name=%r type=%s',
                event.id, event.name, event.event_type)
    all_results = event.results.all()

    # --- strip previous awards ---
    # College: just zero the result-row points + position.  The rebuild SUM at
    # the end of this function recomputes individual_points from scratch, so
    # there's no need to subtract from comp.individual_points here (the old
    # strip-and-subtract pattern is gone).
    if event.event_type == 'college':
        for r in all_results:
            r.points_awarded = Decimal('0')
            r.final_position = None
    else:
        # Pro: still uses the per-row strip pattern because total_earnings is
        # a Float and there's no equivalent SUM rebuild for pro standings.
        for r in all_results:
            awarded = float(r.payout_amount or 0)
            if awarded:
                comp = ProCompetitor.query.get(r.competitor_id)
                if comp:
                    comp.total_earnings = max(0.0, comp.total_earnings - awarded)
            r.payout_amount = 0.0
            r.final_position = None

    completed = [r for r in all_results if r.status == 'completed']
    if not completed:
        event.status = 'in_progress'
        event.is_finalized = False
        # College: even with no completed results we still need to refresh
        # individual_points + team totals to clear any prior award.
        if event.event_type == 'college':
            stripped_competitor_ids = [r.competitor_id for r in all_results]
            _rebuild_individual_points(stripped_competitor_ids)
            stripped_comps = (
                CollegeCompetitor.query
                .filter(CollegeCompetitor.id.in_(stripped_competitor_ids))
                .all()
            )
            stripped_team_ids = {c.team_id for c in stripped_comps if c.team_id}
            for tid in stripped_team_ids:
                team = Team.query.get(tid)
                if team:
                    team.recalculate_points()
        return

    # --- state-machine events: take the order from event_state, not from
    # result_value.  See _state_machine_ranking for why. ---
    tiers = _state_machine_ranking(event)
    if tiers is not None:
        _apply_state_machine_ranking(event, completed, tiers)
        return

    # --- axe throw tie detection (before sorting) ---
    if event.is_axe_throw_cumulative:
        tie_groups = _detect_axe_ties(completed, event)
        for group in tie_groups:
            for r in group:
                r.throwoff_pending = True
        if tie_groups:
            # Positions cannot be fully resolved until throw-off is entered.
            # We still assign provisional positions; the route layer should warn the judge.
            pass
        else:
            # Clear any stale throw-off flags
            for r in completed:
                r.throwoff_pending = False

    # --- sort ---
    completed.sort(key=lambda r: _sort_key(r, event))

    # --- assign positions with proper tie handling ---
    # Two competitors are tied if BOTH their primary metric AND tiebreak metric are equal.
    # Group consecutive results that share a sort key, then for each group:
    #   * assign all members the position of the FIRST member in the group
    #   * for college: split the combined points across the tied competitors
    #   * for pro: each tied competitor gets the payout of their assigned position
    competitor_ids = [r.competitor_id for r in completed]
    if event.event_type == 'college':
        comp_rows = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(competitor_ids)).all()
    else:
        comp_rows = ProCompetitor.query.filter(ProCompetitor.id.in_(competitor_ids)).all()
    comp_lookup = {c.id: c for c in comp_rows}

    # Walk the sorted list once, identifying tied groups by run length.
    #
    # Phase 3 (V2.8.0) partner-event handling (PLAN_REVIEW.md A5):
    # For partnered events (Double Buck, Jack & Jill, Pulp Toss, Peavey Log Roll),
    # each pair has TWO EventResult rows — one per competitor — that share the
    # same time and the same sort key.  These two rows are ONE PAIR for position
    # purposes.  If two pairs tie for 1st (4 rows, all same sort key), the
    # position group consumes 2 positions (not 4), and each row gets the
    # 2-pair split: (10 + 7) / 2 = 8.5 each.
    #
    # Pair detection key: frozenset({competitor_name, partner_name}).  This is
    # symmetric so both row orderings collide on the same key.  Non-partnered
    # events have partner_name=None on every row, so frozenset({name, None}) is
    # unique per row and the pair count equals the row count — same behavior
    # as before for solo events.
    is_partnered = bool(getattr(event, 'is_partnered', False))

    def _pair_key_for(result):
        if is_partnered:
            return frozenset((result.competitor_name, result.partner_name))
        return result.competitor_id  # unique per row for non-partnered

    position = 1
    i = 0
    while i < len(completed):
        # Find the end of the current tied group.
        group_start = i
        group_key = _sort_key(completed[i], event)
        while i < len(completed) and _sort_key(completed[i], event) == group_key:
            i += 1
        group_end = i  # exclusive

        # Count unique PAIRS in this tied group (not unique rows).
        unique_pairs_in_group = len({
            _pair_key_for(completed[j]) for j in range(group_start, group_end)
        })

        if event.event_type == 'college':
            # Split-tie points for college.  Solo positions still flow through
            # this helper — split_tie_points(rank, 1) is just the table value
            # at that rank, so the math is unchanged for non-tied results.
            #
            # Crucially: divide combined points by unique_pairs_in_group, NOT
            # by row count.  In a partnered event, both members of a tied pair
            # collectively occupy ONE position, not two.
            per_pair_points = split_tie_points(position, unique_pairs_in_group)
            for j in range(group_start, group_end):
                completed[j].final_position = position
                completed[j].points_awarded = per_pair_points
                # No inline += on comp.individual_points — the rebuild SUM
                # below handles that in one batched query.
        else:
            # Pro events: each tied competitor gets the payout amount for the
            # tie-shared position.  This matches the historical behavior;
            # whether to split pro payouts on a tie is a separate
            # business decision out of scope for V2.8.0.
            payout = event.get_payout_for_position(position)
            for j in range(group_start, group_end):
                completed[j].final_position = position
                completed[j].payout_amount = payout
                comp = comp_lookup.get(completed[j].competitor_id)
                if comp:
                    comp.total_earnings += payout

        # Advance the position counter by the number of unique pairs (one per
        # entity), not the number of rows.  For solo events these are equal.
        position += unique_pairs_in_group

    # --- college: rebuild individual_points + team totals from SUM ---
    # This single batched rebuild replaces the old per-row += accumulation
    # AND the strip-then-add pattern from the top of this function.  After
    # this call, every competitor's individual_points equals the sum of
    # their EventResult.points_awarded across all events — by construction.
    if event.event_type == 'college':
        # Rebuild for ALL competitors touched by this event (even ones whose
        # current row is not 'completed' — they still need their cache
        # refreshed because the previous-award strip zeroed their points).
        all_touched_ids = list({r.competitor_id for r in all_results})
        _rebuild_individual_points(all_touched_ids)

        # Now rebuild every team that had a competitor in this event.
        all_touched_comps = (
            CollegeCompetitor.query
            .filter(CollegeCompetitor.id.in_(all_touched_ids))
            .all()
        )
        touched_team_ids = {c.team_id for c in all_touched_comps if c.team_id}
        for team_id in touched_team_ids:
            team = Team.query.get(team_id)
            if team:
                team.recalculate_points()

    # --- outlier flagging ---
    flag_score_outliers(completed, event)

    event.status = 'completed'
    event.is_finalized = True


def flag_score_outliers(results: list[EventResult], event: Event) -> None:
    """Flag rows whose score is >2 standard deviations from the event mean."""
    values = []
    for r in results:
        v = _metric(r, event)
        if v is not None:
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                pass

    if len(values) < 3:
        for r in results:
            r.is_flagged = False
        return

    mean = statistics.mean(values)
    stdev = statistics.stdev(values)
    for r in results:
        v = _metric(r, event)
        try:
            fv = float(v)
            r.is_flagged = stdev > 0 and abs(fv - mean) > 2 * stdev
        except (TypeError, ValueError):
            r.is_flagged = False


def preview_positions(event: Event) -> list[dict]:
    """
    Compute provisional standings without writing to the DB.
    Returns a list of dicts ready for JSON/modal display.

    Phase 3 (V2.8.0): preview points use the same split-tie math as
    calculate_positions(), so the modal accurately shows what each
    competitor will receive after finalization.  Decimal values are
    converted to float at the JSON boundary so jsonify() doesn't
    raise TypeError on the live response.
    """
    completed = [r for r in event.results.all() if r.status == 'completed']
    if not completed:
        return []

    completed.sort(key=lambda r: _sort_key(r, event))

    # Phase 3 (V2.8.0): mirror the pair-aware grouping from calculate_positions
    # so the preview modal shows the same per-row points the finalize call
    # will write.  See the long comment in calculate_positions for the
    # partnered-event reasoning.
    is_partnered = bool(getattr(event, 'is_partnered', False))

    def _pair_key_for(result):
        if is_partnered:
            return frozenset((result.competitor_name, result.partner_name))
        return result.competitor_id

    rows: list[dict] = []
    position = 1
    i = 0
    while i < len(completed):
        group_start = i
        group_key = _sort_key(completed[i], event)
        while i < len(completed) and _sort_key(completed[i], event) == group_key:
            i += 1
        group_end = i

        unique_pairs_in_group = len({
            _pair_key_for(completed[j]) for j in range(group_start, group_end)
        })

        per_pair_points = (
            split_tie_points(position, unique_pairs_in_group)
            if event.event_type == 'college' else None
        )

        for j in range(group_start, group_end):
            result = completed[j]
            metric = _metric(result, event)
            row = {
                'position': position,
                'competitor_name': result.competitor_name,
                'partner_name': result.partner_name,
                'result_value': float(metric) if metric is not None else None,
                'run1_value': float(result.run1_value) if result.run1_value is not None else None,
                'run2_value': float(result.run2_value) if result.run2_value is not None else None,
                'run3_value': float(result.run3_value) if result.run3_value is not None else None,
                'best_run': float(result.best_run) if result.best_run is not None else None,
                'tiebreak_value': float(result.tiebreak_value) if result.tiebreak_value is not None else None,
                'throwoff_pending': result.throwoff_pending,
                'status': result.status,
            }
            if event.event_type == 'college':
                row['points'] = float(per_pair_points)
                # tied_with reports the number of unique entities (pairs for
                # partnered events, rows otherwise) sharing this position.
                row['tied_with'] = unique_pairs_in_group if unique_pairs_in_group > 1 else 0
            else:
                row['payout'] = event.get_payout_for_position(position)
            rows.append(row)

        position += unique_pairs_in_group
    return rows


def pending_throwoffs(event: Event) -> list[EventResult]:
    """Return results that have a pending throw-off for this event."""
    return [r for r in event.results.all() if r.throwoff_pending]


def validate_finalization(event: Event) -> list[dict]:
    """Pre-finalization checks. Returns a list of warnings/blockers.

    Each item: {'level': 'warning'|'blocker', 'message': str}
    - 'blocker' = finalization should be prevented
    - 'warning' = finalization can proceed but judge should be aware
    """
    issues = []

    # Check 1: Pro events with no payouts configured
    if event.event_type == 'pro' and not event.uses_payouts_for_state:
        payouts = event.get_payouts()
        if not payouts:
            issues.append({
                'level': 'warning',
                'message': 'No payouts configured for this pro event. '
                           'All payout amounts will be $0. Configure payouts first '
                           'if you want to award prize money.',
            })

    # Check 2: Handicap events with unassigned marks
    if getattr(event, 'is_handicap', False) and event.scoring_type == 'time':
        completed = [r for r in event.results.all() if r.status == 'completed']
        unassigned = [r for r in completed
                      if r.handicap_factor is None or r.handicap_factor == 0.0]
        if unassigned:
            names = ', '.join(r.competitor_name for r in unassigned[:5])
            more = f' (+{len(unassigned) - 5} more)' if len(unassigned) > 5 else ''
            issues.append({
                'level': 'warning',
                'message': f'Handicap event with {len(unassigned)} competitor(s) at scratch '
                           f'(no start mark assigned): {names}{more}. '
                           f'Assign marks before finalizing for accurate handicap results.',
            })

    # Check 3: rows with only one timer read
    #
    # calculate_positions places only status=='completed' rows and sets
    # is_finalized=True regardless, so a partial row finalizes at position None
    # with 0.00 points and is filtered out of the public results feed
    # (routes/api.py:190). save_heat_results_submission now refuses to
    # AUTO-finalize while any of these exist. Manual finalize is still allowed
    # on purpose, because an operator whose second stopwatch is genuinely gone
    # has to be able to close the event, but they should not be able to do it
    # without being told who they are dropping.
    #
    # Filed at 'warning' rather than 'blocker' because 'blocker' is a dead
    # level: the docstring above says it prevents finalization and no caller
    # anywhere implements that. routes/scoring.py finalize_event flashes every
    # issue at 'warning' and proceeds either way. Emitting a 'blocker' here
    # would read as enforcement that does not exist.
    partial = [r for r in event.results.all() if r.status == 'partial']
    if partial:
        names = ', '.join(r.competitor_name for r in partial[:5])
        more = f' (+{len(partial) - 5} more)' if len(partial) > 5 else ''
        issues.append({
            'level': 'warning',
            'message': f'{len(partial)} competitor(s) have only one timer '
                       f'entered: {names}{more}. Finalizing now places them at '
                       f'no position with 0.00 points and leaves them out of '
                       f'the public results. Enter the second timer, or set '
                       f'them to DNF, before finalizing.',
        })

    # Check 4: Pending throwoffs on axe events
    throwoffs = [r for r in event.results.all() if r.throwoff_pending]
    if throwoffs:
        names = ', '.join(r.competitor_name for r in throwoffs[:5])
        more = f' (+{len(throwoffs) - 5} more)' if len(throwoffs) > 5 else ''
        issues.append({
            'level': 'warning',
            'message': f'Throwoff pending for {len(throwoffs)} competitor(s): {names}{more}. '
                       f'Positions will be provisional until throwoffs are resolved.',
        })

    return issues


def _spread_positions_across_pairs(event: Event, position_map: dict[int, int],
                                   result_lookup: dict) -> dict[int, int]:
    """Give every row of a partnered unit the position submitted for that unit.

    No-op on solo events, where each row is its own unit.
    """
    if not getattr(event, 'is_partnered', False):
        return position_map

    by_unit: dict = {}
    for r in result_lookup.values():
        by_unit.setdefault(_pair_key_for(r, True), []).append(r)

    spread = dict(position_map)
    for result_id, position in position_map.items():
        row = result_lookup.get(result_id)
        if row is None:
            continue
        for mate in by_unit.get(_pair_key_for(row, True), []):
            spread[mate.id] = position
    return spread


def validate_throwoff_submission(event: Event,
                                 position_map: dict[int, int]) -> list[str]:
    """Reasons this throw-off submission must be refused, empty list if fine.

    The form used to render every select defaulted to "Position 1" with no
    server-side check, so an operator who opened the banner and pressed the
    button without touching the dropdowns wrote first place to every tied
    competitor and published it.  A throw-off IS the tiebreak, so its output is
    always a permutation of the places being contested: no duplicates, nothing
    outside the block.  That makes the bad submission mechanically detectable
    rather than a judgement call, so it is refused rather than warned about.

    Checked here rather than in the route so a direct POST is held to the same
    rule as the rendered form.
    """
    if not position_map:
        return ['No throw-off positions submitted.']

    groups = throwoff_groups(event)
    if not groups:
        return []

    errors = []
    for group in groups:
        expected = list(group['positions'])
        submitted = []
        split = False
        for entry in group['entries']:
            vals = {position_map[rid] for rid in entry['result_ids']
                    if rid in position_map}
            if not vals:
                continue
            if len(vals) > 1:
                # A hand-built POST naming both halves of a pair with different
                # places.  The rendered form cannot produce this (one select per
                # unit), but the wire format is per result row, so a crafted
                # body can.  Refuse it here rather than letting
                # _spread_positions_across_pairs pick a winner by dict order,
                # which is arbitrary and silently drops one of the two.
                errors.append(
                    '{} was given more than one finishing position ({}). '
                    'A pair finishes together.'.format(
                        entry['label'],
                        ', '.join(str(v) for v in sorted(vals))))
                split = True
                continue
            submitted.append((entry['label'], sorted(vals)[0]))

        if split:
            continue

        if len(submitted) != len(expected):
            errors.append(
                'Places {} need a position for all {} entries; got {}.'.format(
                    ', '.join(str(p) for p in expected), len(expected),
                    len(submitted)))
            continue

        chosen = sorted(v for _label, v in submitted)
        if chosen != expected:
            dupes = sorted({v for v in chosen if chosen.count(v) > 1})
            if dupes:
                names = ', '.join(
                    label for label, v in submitted if v in dupes)
                errors.append(
                    'Two entries were given the same finishing position '
                    '({}): {}. A throw-off has to separate them.'.format(
                        ', '.join(str(d) for d in dupes), names))
            else:
                errors.append(
                    'Positions {} are outside the places being contested '
                    '({}).'.format(', '.join(str(c) for c in chosen),
                                   ', '.join(str(p) for p in expected)))
    return errors


def record_throwoff_result(event: Event, position_map: dict[int, int]) -> None:
    """
    Resolve throw-off positions for axe throw ties.

    position_map: {result_id: final_position} — judge-assigned positions after throw-off.
    Clears throwoff_pending flags and re-awards points/payouts for affected positions.

    Phase 3 (V2.8.0): for college events this now uses the same SUM-rebuild
    pattern as calculate_positions() instead of the old delta-arithmetic
    on comp.individual_points.  The two paths must stay in sync — having
    them diverge was PLAN_REVIEW.md finding A6.

    On a partnered event the form shows one select per pair, named for the
    pair's first result id, and the position is spread to the pair's other row
    here rather than in the template.  Doing it server side means a hand-built
    POST cannot split a pair across two finishing positions either.
    """
    result_lookup = {r.id: r for r in event.results.all()}
    position_map = _spread_positions_across_pairs(event, position_map,
                                                  result_lookup)
    for result_id, position in position_map.items():
        result = result_lookup.get(result_id)
        if result is None:
            continue
        result.final_position = position
        result.throwoff_pending = False

        if event.event_type == 'college':
            # Throw-off positions are explicit per-competitor positions assigned
            # by the judge — no tie-splitting at this stage (the throw-off IS
            # the tiebreak).  Use the canonical PLACEMENT_POINTS_DECIMAL table
            # (same source of truth as calculate_positions / split_tie_points).
            idx = position - 1
            new_pts = PLACEMENT_POINTS_DECIMAL[idx] if 0 <= idx < len(PLACEMENT_POINTS_DECIMAL) else Decimal('0')
            result.points_awarded = new_pts
        else:
            old_pay = float(result.payout_amount or 0)
            new_pay = event.get_payout_for_position(position)
            diff = new_pay - old_pay
            result.payout_amount = new_pay
            comp = ProCompetitor.query.get(result.competitor_id)
            if comp:
                comp.total_earnings = max(0.0, comp.total_earnings + diff)

    if event.event_type == 'college':
        # Rebuild from SUM — single source of truth, same path as
        # calculate_positions() so the two functions stay consistent.
        all_comp_ids = list({r.competitor_id for r in result_lookup.values()})
        _rebuild_individual_points(all_comp_ids)
        comp_rows = (
            CollegeCompetitor.query
            .filter(CollegeCompetitor.id.in_(all_comp_ids))
            .all()
        )
        touched_team_ids = {c.team_id for c in comp_rows if getattr(c, 'team_id', None)}
        for team_id in touched_team_ids:
            team = Team.query.get(team_id)
            if team:
                team.recalculate_points()

    log_action('throwoff_recorded', 'event', event.id,
               {'positions': position_map})


def outlier_check(event: Event) -> list[dict]:
    """
    Return a list of would-be-flagged results *before* finalization.
    Used to show a warning modal so the judge can confirm.
    """
    completed = [r for r in event.results.all() if r.status == 'completed']
    values = []
    for r in completed:
        v = _metric(r, event)
        if v is not None:
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                pass

    if len(values) < 3:
        return []

    mean = statistics.mean(values)
    stdev = statistics.stdev(values)
    flagged = []
    for r in completed:
        v = _metric(r, event)
        try:
            fv = float(v)
            if stdev > 0 and abs(fv - mean) > 2 * stdev:
                flagged.append({
                    'competitor_name': r.competitor_name,
                    'result_value': fv,
                    'mean': round(mean, 3),
                    'deviation': round(abs(fv - mean) / stdev, 2),
                })
        except (TypeError, ValueError):
            pass
    return flagged


# ---------------------------------------------------------------------------
# Public: standings
# ---------------------------------------------------------------------------

def get_individual_standings(tournament_id: int, gender: str = None, limit: int = None) -> list:
    """Ranked college competitor list by individual_points. Returns [(rank, competitor)]."""
    query = CollegeCompetitor.query.filter_by(tournament_id=tournament_id, status='active')
    if gender:
        query = query.filter_by(gender=gender)
    competitors = query.order_by(CollegeCompetitor.individual_points.desc()).all()
    if limit:
        competitors = competitors[:limit]

    standings = []
    current_rank = 1
    previous_points = None
    for i, comp in enumerate(competitors):
        if comp.individual_points != previous_points:
            current_rank = i + 1
        standings.append((current_rank, comp))
        previous_points = comp.individual_points
    return standings


def get_team_standings(tournament_id: int, limit: int = None) -> list:
    """Ranked team list by total_points. Returns [(rank, team)]."""
    teams = (Team.query.filter_by(tournament_id=tournament_id, status='active')
             .order_by(Team.total_points.desc()).all())
    if limit:
        teams = teams[:limit]

    standings = []
    current_rank = 1
    previous_points = None
    for i, team in enumerate(teams):
        if team.total_points != previous_points:
            current_rank = i + 1
        standings.append((current_rank, team))
        previous_points = team.total_points
    return standings


def recalculate_all_team_points(tournament_id: int) -> None:
    """Recalculate all team points from member individual_points. Use after corrections."""
    teams = Team.query.filter_by(tournament_id=tournament_id).all()
    for team in teams:
        team.recalculate_points()
    db.session.commit()


# ---------------------------------------------------------------------------
# Public: live poll data
# ---------------------------------------------------------------------------

def live_standings_data(event: Event) -> dict:
    """
    Return current standings as JSON-serialisable dict for the polling endpoint.
    Includes unfinished heats so in-progress events show real-time ordering.

    Phase 3 (V2.8.0): all numeric fields cast to float at the JSON boundary
    so jsonify() works on Decimal-typed columns from Phase 1B.
    """
    results = event.results.all()
    completed = [r for r in results if r.status == 'completed']
    completed.sort(key=lambda r: _sort_key(r, event))

    # Phase 3 (V2.8.0): pair-aware position grouping (same logic as
    # calculate_positions / preview_positions).  Two rows from the same
    # partnered pair share one position.
    is_partnered = bool(getattr(event, 'is_partnered', False))

    def _pair_key_for(result):
        if is_partnered:
            return frozenset((result.competitor_name, result.partner_name))
        return result.competitor_id

    rows = []
    position = 1
    i = 0
    while i < len(completed):
        group_start = i
        group_key = _sort_key(completed[i], event)
        while i < len(completed) and _sort_key(completed[i], event) == group_key:
            i += 1
        group_end = i

        unique_pairs_in_group = len({
            _pair_key_for(completed[j]) for j in range(group_start, group_end)
        })

        for j in range(group_start, group_end):
            r = completed[j]
            metric = _metric(r, event)
            rows.append({
                'position': position,
                'competitor_name': r.competitor_name,
                'result_value': float(metric) if metric is not None else None,
                'run1_value': float(r.run1_value) if r.run1_value is not None else None,
                'run2_value': float(r.run2_value) if r.run2_value is not None else None,
                'run3_value': float(r.run3_value) if r.run3_value is not None else None,
                'best_run': float(r.best_run) if r.best_run is not None else None,
                'is_flagged': r.is_flagged,
                'throwoff_pending': r.throwoff_pending,
                'status': r.status,
            })

        position += unique_pairs_in_group

    total_heats = event.heats.count()
    completed_heats = event.heats.filter_by(status='completed').count()

    return {
        'event_id': event.id,
        'event_name': event.display_name,
        'is_finalized': event.is_finalized,
        'scoring_order': event.scoring_order,
        'requires_dual_runs': event.requires_dual_runs,
        'requires_triple_runs': event.requires_triple_runs,
        'heats_total': total_heats,
        'heats_completed': completed_heats,
        'rows': rows,
    }


# ---------------------------------------------------------------------------
# Public: bulk CSV import
# ---------------------------------------------------------------------------

def import_results_from_csv(event: Event, csv_text: str) -> dict:
    """
    Parse a CSV with columns: competitor_name, result, [run1], [run2], [run3], [status]
    and upsert EventResult rows.

    Returns {'imported': int, 'skipped': int, 'errors': list[str]}.
    """
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    imported = 0
    skipped = 0
    errors = []

    # Build name → competitor lookup scoped to THIS event's roster, not the
    # whole tournament. If the lookup used the tournament-wide roster, a
    # competitor who is in the tournament but NOT entered in this event
    # could receive an EventResult row via CSV import (and points, and a
    # payout). Only accept names that match a competitor whose
    # events_entered list includes this event ID.
    if event.event_type == 'college':
        all_comps = CollegeCompetitor.query.filter_by(
            tournament_id=event.tournament_id, status='active'
        ).all()
    else:
        all_comps = ProCompetitor.query.filter_by(
            tournament_id=event.tournament_id, status='active'
        ).all()

    event_id = event.id
    # Per CLAUDE.md §4, both college and pro competitors store events_entered
    # as a JSON list of NAMES (Excel/Google Forms imports), not numeric IDs.
    # Resolving by integer ID alone silently drops every competitor whose
    # entries are name-keyed — same failure shape as the V2.8.2 Woodboss
    # zero-count bug. Build BOTH an int-id set and a normalised-name set,
    # accept a row if either matches.
    import re as _re

    def _normalize_event_name(s: str) -> str:
        return _re.sub(r'[^a-z0-9]+', '', str(s or '').lower())

    target_aliases = {
        _normalize_event_name(event.name),
        _normalize_event_name(event.display_name),
    }
    comp_by_name = {}
    for c in all_comps:
        entered_ids: set[int] = set()
        entered_names: set[str] = set()
        for raw in c.get_events_entered():
            s = str(raw).strip()
            if not s:
                continue
            try:
                entered_ids.add(int(s))
            except (TypeError, ValueError):
                entered_names.add(_normalize_event_name(s))
        if event_id not in entered_ids and not (entered_names & target_aliases):
            continue
        # Gender filter: refuse to import men's results into a women's
        # event and vice versa. Open events (event.gender is None) accept
        # any gender.
        if event.gender and c.gender and c.gender != event.gender:
            continue
        comp_by_name[c.name.strip().lower()] = c

    existing = {r.competitor_id: r for r in event.results.all()}

    for row_num, row in enumerate(reader, start=2):
        name = (row.get('competitor_name') or '').strip()
        if not name:
            skipped += 1
            continue

        comp = comp_by_name.get(name.lower())
        if comp is None:
            errors.append(f"Row {row_num}: competitor '{name}' not found in tournament.")
            skipped += 1
            continue

        # Handle DQ/DNS/DNF status values in the result column
        raw_result = (row.get('result') or '').strip()
        # An empty result cell and an explicit DQ are two different statements
        # and have to stay distinguishable. They are collapsed together below
        # because both produce result_value=None, but only the explicit token
        # is the operator saying the competitor did not post a result. A blank
        # cell on a row that carries run columns means "derive it", and the
        # import page tells the operator exactly that.
        result_is_blank = raw_result == ''
        is_dq = raw_result.upper() in ('DQ', 'DNS', 'DNF', 'DSQ', 'DISQUALIFIED', '')

        if is_dq:
            result_val = None
            row_status = 'dnf' if raw_result.upper() in ('DNS', 'DNF') else 'scratched'
        else:
            try:
                result_val = _parse_result_value(raw_result)
            except (TypeError, ValueError):
                errors.append(f"Row {row_num}: invalid result value '{raw_result}' for '{name}'.")
                skipped += 1
                continue
            row_status = None  # determined below

        r = existing.get(comp.id)
        if not r:
            r = EventResult(
                event_id=event.id,
                competitor_id=comp.id,
                competitor_type=event.event_type,
                competitor_name=comp.display_name,
            )
            db.session.add(r)
            existing[comp.id] = r

        r.result_value = result_val

        # Parse individual run values (also handle DQ in run columns)
        for attr, col_name in [('run1_value', 'run1'), ('run2_value', 'run2'), ('run3_value', 'run3')]:
            raw_run = (row.get(col_name) or '').strip()
            if raw_run.upper() in ('DQ', 'DNS', 'DNF', 'DSQ', ''):
                setattr(r, attr, None)
            else:
                try:
                    val = _parse_result_value(raw_run)
                    setattr(r, attr, val if val else None)
                except (TypeError, ValueError):
                    setattr(r, attr, None)

        if event.requires_dual_runs and r.run1_value is not None:
            r.calculate_best_run(event.scoring_order)
        if event.requires_triple_runs:
            r.calculate_cumulative_score()

        # The scratch decision was made above from the raw result cell, before
        # either derivation had run. On a dual or triple run event that reads
        # the blank cell, calls the row a scratch, then fills result_value in
        # from the run columns and marks the scratch anyway. The operator gets
        # 'Imported N result(s), skipped 0.' in green and a competitor who
        # posted three real throws sits out the standings. Reconsider it here,
        # once, now that the value is actually known.
        #
        # Only a blank cell is reconsidered. An explicit DQ, DNS, DNF or DSQ is
        # the operator making a statement and outranks anything the run columns
        # imply, so those are left alone. A row with no result and no runs is
        # still a scratch, because nothing derived a value.
        if result_is_blank and r.result_value is not None:
            row_status = None

        # Status: explicit column overrides auto-detection
        explicit_status = (row.get('status') or '').strip().lower()
        if explicit_status:
            r.status = explicit_status
        elif row_status:
            r.status = row_status
        else:
            r.status = 'completed'
        imported += 1

    return {'imported': imported, 'skipped': skipped, 'errors': errors}


def _parse_result_value(raw: str) -> float:
    """Parse a result value string, handling feet/inches distance format.

    Supports:
      - Plain numeric: "28.0", "94"
      - Feet/inches: "23'3\\"", "23' 3" (apostrophe required)
      - Minutes:seconds: "2:30.5"

    Returns float value. Raises ValueError on unparseable input.
    """
    import re
    raw = str(raw or '').strip()
    if not raw:
        raise ValueError('empty result')

    # Try plain numeric first (most common case)
    try:
        return float(raw)
    except ValueError:
        pass

    # Feet/inches: 23'3", 23' 3, etc. — apostrophe is REQUIRED to distinguish from plain numbers
    ft_match = re.match(r"^(\d+)['\u2019]\s*(\d+(?:\.\d+)?)[\"″]?\s*$", raw)
    if ft_match:
        feet = float(ft_match.group(1))
        inches = float(ft_match.group(2))
        return feet * 12 + inches  # return as total inches

    ft_only = re.match(r"^(\d+)['\u2019]\s*$", raw)
    if ft_only:
        return float(ft_only.group(1)) * 12  # feet only, convert to inches

    # Minutes:seconds: 2:30.5 → 150.5
    time_match = re.match(r'^(\d+):(\d+(?:\.\d+)?)\s*$', raw)
    if time_match:
        minutes = float(time_match.group(1))
        seconds = float(time_match.group(2))
        return minutes * 60 + seconds

    raise ValueError(f'Cannot parse result value: {raw!r}')


# ---------------------------------------------------------------------------
# Public: payout templates
# ---------------------------------------------------------------------------

def list_payout_templates() -> list[PayoutTemplate]:
    return PayoutTemplate.query.order_by(PayoutTemplate.name).all()


def save_payout_template(name: str, payout_dict: dict) -> PayoutTemplate:
    """Create or update a named payout template."""
    name = name.strip()
    template = PayoutTemplate.query.filter_by(name=name).first()
    if template is None:
        template = PayoutTemplate(name=name)
        db.session.add(template)
    template.set_payouts(payout_dict)
    db.session.commit()
    return template


def apply_payout_template(event: Event, template_id: int) -> bool:
    """Apply a saved template's payout structure to an event.

    If the event is already finalized, re-runs calculate_positions() so that
    the new payout amounts propagate to EventResult.payout_amount and
    ProCompetitor.total_earnings immediately.  Returns True on success.
    """
    template = PayoutTemplate.query.get(template_id)
    if template is None:
        return False
    event.set_payouts(template.get_payouts())
    if event.is_finalized:
        with db.session.begin_nested():
            calculate_positions(event)
    db.session.commit()
    return True


def delete_payout_template(template_id: int) -> bool:
    """Delete a payout template. Returns True if deleted."""
    template = PayoutTemplate.query.get(template_id)
    if template is None:
        return False
    db.session.delete(template)
    db.session.commit()
    return True
