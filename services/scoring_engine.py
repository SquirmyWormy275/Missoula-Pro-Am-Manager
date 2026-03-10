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
from typing import Optional

from database import db
from models.event import Event, EventResult
from models.competitor import CollegeCompetitor, ProCompetitor
from models.team import Team
from models.payout_template import PayoutTemplate
import config
from services.audit import log_action

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _metric(result: EventResult, event: Event) -> Optional[float]:
    """Return the primary ranked metric for a result row.

    For handicap-format events (event.is_handicap is True), the competitor's
    start mark (stored in result.handicap_factor as seconds) is subtracted from
    the raw time to produce the adjusted net time used for ranking.
    A handicap_factor of None or 1.0 (the DB default placeholder) is treated as
    0.0 (scratch — no start mark assigned yet).
    """
    if event.requires_dual_runs:
        raw = result.best_run
    else:
        raw = result.result_value

    if raw is None:
        return None

    # Apply handicap start mark: net_time = raw_time - start_mark_seconds.
    # handicap_factor stores the start mark; 1.0 is the DB default placeholder
    # meaning "not yet assigned" — treat as 0.0 (scratch).
    if getattr(event, 'is_handicap', False) and event.scoring_type == 'time':
        start_mark = result.handicap_factor
        if start_mark is None or start_mark == 1.0:
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


def _detect_axe_ties(results: list[EventResult]) -> list[list[EventResult]]:
    """Return groups of 2+ results that share the same result_value (axe throw tie)."""
    from itertools import groupby
    groups = []
    sorted_results = sorted(results, key=lambda r: r.result_value or 0, reverse=True)
    for _val, group in groupby(sorted_results, key=lambda r: r.result_value):
        group_list = list(group)
        if len(group_list) >= 2:
            groups.append(group_list)
    return groups


# ---------------------------------------------------------------------------
# Public: position calculation
# ---------------------------------------------------------------------------

def calculate_positions(event: Event) -> None:
    """
    Calculate final positions and award points/payouts for event.

    This is idempotent — previously awarded points/payouts are stripped before
    recalculating so calling it twice yields the same result.

    Raises nothing; caller should catch StaleDataError/IntegrityError and rollback.
    """
    logger.info('scoring_engine: calculate_positions event_id=%s name=%r type=%s',
                event.id, event.name, event.event_type)
    all_results = event.results.all()

    # --- strip previous awards ---
    if event.event_type == 'college':
        for r in all_results:
            awarded = int(r.points_awarded or 0)
            if awarded:
                comp = CollegeCompetitor.query.get(r.competitor_id)
                if comp:
                    comp.individual_points = max(0, comp.individual_points - awarded)
            r.points_awarded = 0
            r.final_position = None
    else:
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
        return

    # --- axe throw tie detection (before sorting) ---
    if event.is_axe_throw_cumulative:
        tie_groups = _detect_axe_ties(completed)
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
    # Exception: axe throw ties are unresolved until throw-off — keep the same sort order
    # but mark them with the same provisional position.
    competitor_ids = [r.competitor_id for r in completed]
    if event.event_type == 'college':
        comp_rows = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(competitor_ids)).all()
    else:
        comp_rows = ProCompetitor.query.filter(ProCompetitor.id.in_(competitor_ids)).all()
    comp_lookup = {c.id: c for c in comp_rows}

    position = 1
    for i, result in enumerate(completed):
        if i > 0:
            prev = completed[i - 1]
            if _sort_key(result, event) != _sort_key(prev, event):
                position = i + 1  # skip positions for tied block

        result.final_position = position

        if event.event_type == 'college':
            points = config.PLACEMENT_POINTS.get(position, 0)
            result.points_awarded = points
            comp = comp_lookup.get(result.competitor_id)
            if comp:
                comp.individual_points += points
        else:
            payout = event.get_payout_for_position(position)
            result.payout_amount = payout
            comp = comp_lookup.get(result.competitor_id)
            if comp:
                comp.total_earnings += payout

    # --- team point recalc (college) ---
    if event.event_type == 'college':
        touched_team_ids = {c.team_id for c in comp_rows if hasattr(c, 'team_id') and c.team_id}
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
    """
    completed = [r for r in event.results.all() if r.status == 'completed']
    if not completed:
        return []

    completed.sort(key=lambda r: _sort_key(r, event))

    position = 1
    out = []
    for i, result in enumerate(completed):
        if i > 0:
            prev = completed[i - 1]
            if _sort_key(result, event) != _sort_key(prev, event):
                position = i + 1

        metric = _metric(result, event)
        row = {
            'position': position,
            'competitor_name': result.competitor_name,
            'partner_name': result.partner_name,
            'result_value': metric,
            'run1_value': result.run1_value,
            'run2_value': result.run2_value,
            'run3_value': result.run3_value,
            'best_run': result.best_run,
            'tiebreak_value': result.tiebreak_value,
            'throwoff_pending': result.throwoff_pending,
            'status': result.status,
        }
        if event.event_type == 'college':
            row['points'] = config.PLACEMENT_POINTS.get(position, 0)
        else:
            row['payout'] = event.get_payout_for_position(position)
        out.append(row)
    return out


def pending_throwoffs(event: Event) -> list[EventResult]:
    """Return results that have a pending throw-off for this event."""
    return [r for r in event.results.all() if r.throwoff_pending]


def record_throwoff_result(event: Event, position_map: dict[int, int]) -> None:
    """
    Resolve throw-off positions for axe throw ties.

    position_map: {result_id: final_position} — judge-assigned positions after throw-off.
    Clears throwoff_pending flags and re-awards points/payouts for affected positions.
    """
    result_lookup = {r.id: r for r in event.results.all()}
    for result_id, position in position_map.items():
        result = result_lookup.get(result_id)
        if result is None:
            continue
        result.final_position = position
        result.throwoff_pending = False

        if event.event_type == 'college':
            old_pts = int(result.points_awarded or 0)
            new_pts = config.PLACEMENT_POINTS.get(position, 0)
            diff = new_pts - old_pts
            result.points_awarded = new_pts
            comp = CollegeCompetitor.query.get(result.competitor_id)
            if comp:
                comp.individual_points = max(0, comp.individual_points + diff)
        else:
            old_pay = float(result.payout_amount or 0)
            new_pay = event.get_payout_for_position(position)
            diff = new_pay - old_pay
            result.payout_amount = new_pay
            comp = ProCompetitor.query.get(result.competitor_id)
            if comp:
                comp.total_earnings = max(0.0, comp.total_earnings + diff)

    if event.event_type == 'college':
        all_comp_ids = [r.competitor_id for r in result_lookup.values()]
        comp_rows = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(all_comp_ids)).all()
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
    """
    results = event.results.all()
    completed = [r for r in results if r.status == 'completed']
    completed.sort(key=lambda r: _sort_key(r, event))

    rows = []
    position = 1
    for i, r in enumerate(completed):
        if i > 0 and _sort_key(r, event) != _sort_key(completed[i - 1], event):
            position = i + 1
        rows.append({
            'position': position,
            'competitor_name': r.competitor_name,
            'result_value': _metric(r, event),
            'run1_value': r.run1_value,
            'run2_value': r.run2_value,
            'run3_value': r.run3_value,
            'best_run': r.best_run,
            'is_flagged': r.is_flagged,
            'throwoff_pending': r.throwoff_pending,
            'status': r.status,
        })

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

    # Build name → competitor lookup
    if event.event_type == 'college':
        all_comps = CollegeCompetitor.query.filter_by(tournament_id=event.tournament_id).all()
    else:
        all_comps = ProCompetitor.query.filter_by(tournament_id=event.tournament_id).all()
    comp_by_name = {c.name.strip().lower(): c for c in all_comps}

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

        try:
            result_val = float(row.get('result') or 0)
        except (TypeError, ValueError):
            errors.append(f"Row {row_num}: invalid result value for '{name}'.")
            skipped += 1
            continue

        r = existing.get(comp.id)
        if not r:
            r = EventResult(
                event_id=event.id,
                competitor_id=comp.id,
                competitor_type=event.event_type,
                competitor_name=comp.name,
            )
            db.session.add(r)
            existing[comp.id] = r

        r.result_value = result_val
        try:
            r.run1_value = float(row.get('run1') or 0) or None
            r.run2_value = float(row.get('run2') or 0) or None
            r.run3_value = float(row.get('run3') or 0) or None
        except (TypeError, ValueError):
            pass

        if event.requires_dual_runs and r.run1_value is not None:
            r.calculate_best_run(event.scoring_order)
        if event.requires_triple_runs:
            r.calculate_cumulative_score()

        r.status = (row.get('status') or 'completed').strip().lower()
        imported += 1

    return {'imported': imported, 'skipped': skipped, 'errors': errors}


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
    """Apply a saved template's payout structure to an event. Returns True on success."""
    template = PayoutTemplate.query.get(template_id)
    if template is None:
        return False
    event.set_payouts(template.get_payouts())
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
