"""
Heat management routes: event_heats, generate_heats, generate_college_heats,
move_competitor_between_heats, scratch_competitor, heat_sync_check, heat_sync_fix.
"""
import json

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

import config
import strings as text
from database import db
from models import Event, EventResult, Heat, HeatAssignment, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.audit import log_action
from services.cache_invalidation import invalidate_tournament_caches

from . import (
    _build_signup_rows,
    _is_list_only_event,
    _load_competitor_lookup,
    _max_per_heat,
    _normalize_name,
    _signed_up_competitors,
    scheduling_bp,
)


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/heats')
def event_heats(tournament_id, event_id):
    """View and manage heats for an event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament.id:
        abort(404)

    heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
    signup_list_mode = _is_list_only_event(event)
    signup_rows = _build_signup_rows(event) if signup_list_mode else []

    # Build competitor spacing heatmap data (run-1 heats only)
    spacing_data = {}
    if not signup_list_mode and heats:
        run1_heats = [h for h in heats if h.run_number == 1] or heats
        comp_appearances: dict = {}
        for h in run1_heats:
            for cid in h.get_competitors():
                comp_appearances.setdefault(int(cid), []).append(h.heat_number)
        all_cids = list(comp_appearances.keys())
        if all_cids:
            if event.event_type == 'college':
                name_map = {c.id: c.display_name for c in CollegeCompetitor.query.filter(
                    CollegeCompetitor.id.in_(all_cids)).all()}
            else:
                name_map = {c.id: c.display_name for c in ProCompetitor.query.filter(
                    ProCompetitor.id.in_(all_cids)).all()}
            spacing_data = {
                'total_heats': len(run1_heats),
                'competitors': sorted(
                    [{'name': name_map.get(cid, f'ID:{cid}'), 'appearances': sorted(app)}
                     for cid, app in comp_appearances.items()],
                    key=lambda x: x['name'].lower(),
                ),
            }

    # Batch-load competitor objects and result statuses to fix N+1 queries.
    # Templates use comp_lookup and result_status_map instead of per-row DB hits.
    all_comp_ids = []
    for h in heats:
        all_comp_ids.extend(h.get_competitors())
    comp_lookup = _load_competitor_lookup(event, all_comp_ids)
    all_results = EventResult.query.filter_by(event_id=event.id).all()
    result_status_map = {r.competitor_id: r.status for r in all_results}

    # Competitors registered for this event but not in any heat (for Add Competitor UI)
    all_heat_comp_ids = set(all_comp_ids)
    unassigned_competitors = [
        {'id': r.competitor_id, 'name': r.competitor_name}
        for r in all_results
        if r.competitor_id not in all_heat_comp_ids and r.status != 'scratched'
    ]

    return render_template('scheduling/heats.html',
                           tournament=tournament,
                           event=event,
                           heats=heats,
                           signup_rows=signup_rows,
                           signup_list_mode=signup_list_mode,
                           spacing_data=spacing_data,
                           comp_lookup=comp_lookup,
                           result_status_map=result_status_map,
                           unassigned_competitors=unassigned_competitors)


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/generate-heats', methods=['POST'])
def generate_heats(tournament_id, event_id):
    """Generate heats for an event using snake draft distribution."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    # Hard block: finalized events cannot have heats regenerated
    if event.is_finalized:
        flash(f'{event.display_name} is finalized. Heat regeneration is blocked.', 'error')
        return redirect(url_for('scheduling.event_heats',
                                tournament_id=tournament_id, event_id=event_id))

    # Soft warn: scored events require explicit confirmation to regenerate
    has_scored = EventResult.query.filter_by(event_id=event.id, status='completed').first() is not None
    if has_scored and request.form.get('confirm') != 'true':
        flash(
            f'{event.display_name} has scored results. Regenerating heats will orphan '
            f'those results. Click Regenerate again to confirm.',
            'warning'
        )
        return redirect(url_for('scheduling.event_heats',
                                tournament_id=tournament_id, event_id=event_id))

    # Gear-sharing integrity gate for pro events: block generation when any enrolled
    # competitor has unstructured gear details but no structured gear_sharing map.
    # This prevents silently building heats with unresolved gear conflicts.
    if event.event_type == 'pro':
        enrolled_ids = {
            r.competitor_id
            for r in EventResult.query.filter_by(event_id=event.id, competitor_type='pro').all()
        }
        if enrolled_ids:
            unresolved_gear = [
                c for c in ProCompetitor.query.filter(
                    ProCompetitor.id.in_(enrolled_ids),
                    ProCompetitor.tournament_id == tournament_id,
                    ProCompetitor.status == 'active',
                ).all()
                if str(getattr(c, 'gear_sharing_details', '') or '').strip()
                and not c.get_gear_sharing()
            ]
            if unresolved_gear:
                names = ', '.join(c.name for c in unresolved_gear[:5])
                extra = f' (+{len(unresolved_gear) - 5} more)' if len(unresolved_gear) > 5 else ''
                flash(
                    f'Heat generation blocked: {len(unresolved_gear)} competitor(s) in '
                    f'{event.display_name} have unstructured gear-sharing notes — '
                    f'{names}{extra}. '
                    'Parse gear details in the Gear Sharing Manager first, or run Preflight Auto-Fix.',
                    'error'
                )
                return redirect(url_for('scheduling.event_heats',
                                        tournament_id=tournament_id,
                                        event_id=event_id))

    # Import heat generation service
    from services.heat_generator import generate_event_heats, get_last_gear_violations
    from services.saw_block_assignment import trigger_saw_block_recompute

    try:
        num_heats = generate_event_heats(event)
        db.session.commit()
        if _is_list_only_event(event):
            flash(f'{event.display_name} uses signups only (no heats).', 'success')
        else:
            flash(text.FLASH['heats_generated'].format(num_heats=num_heats, event_name=event.display_name), 'success')
        # Surface any forced gear-sharing fallback placements (gear audit G2/G3).
        violations = get_last_gear_violations(event.id)
        if violations:
            flash(
                f'WARNING: {len(violations)} gear-sharing conflict(s) could not be avoided '
                f'during heat generation. Review the gear manager before running the show.',
                'warning'
            )
        # Recompute hand-saw stand block alternation after heat gen commits.
        tournament = Tournament.query.get(tournament_id)
        if tournament is not None:
            trigger_saw_block_recompute(tournament)
    except Exception as e:
        db.session.rollback()
        from flask import current_app
        current_app.logger.exception(
            'Heat generation failed for tournament %s event %s', tournament_id, event_id,
        )
        # Generic message — full traceback is in app logs. Raw exception text
        # never surfaces in the UI per CLAUDE.md §6 safe-error-handling rule.
        flash(text.FLASH['heats_error'].format(
            error='see application logs (admin only)'
        ), 'error')

    return redirect(url_for('scheduling.event_heats',
                            tournament_id=tournament_id,
                            event_id=event_id))


@scheduling_bp.route('/<int:tournament_id>/generate-college-heats', methods=['POST'])
def generate_college_heats(tournament_id):
    """Bulk-generate heats for all closed college events in one click."""
    from services.heat_generator import generate_event_heats

    tournament = Tournament.query.get_or_404(tournament_id)
    events = tournament.events.filter_by(event_type='college').order_by(Event.name, Event.gender).all()

    generated = 0
    skipped_open = 0
    skipped_completed = 0
    errors = 0

    skipped_finalized = 0
    for event in events:
        if _is_list_only_event(event):
            skipped_open += 1
            continue
        if event.status == 'completed':
            skipped_completed += 1
            continue
        if event.is_finalized:
            skipped_finalized += 1
            continue
        try:
            with db.session.begin_nested():
                generate_event_heats(event)
            generated += 1
        except Exception as exc:
            if 'No competitors entered' in str(exc):
                skipped_open += 1
            else:
                errors += 1
                flash(f'Error generating heats for {event.display_name}: {exc}', 'error')

    db.session.commit()

    # Recompute hand-saw stand block alternation after bulk college gen.
    from services.saw_block_assignment import trigger_saw_block_recompute
    trigger_saw_block_recompute(tournament)

    parts = []
    if generated:
        parts.append(f'Heats generated for {generated} event(s)')
    if skipped_open:
        parts.append(f'{skipped_open} signup-list event(s) skipped')
    if skipped_completed:
        parts.append(f'{skipped_completed} completed event(s) unchanged')
    if skipped_finalized:
        parts.append(f'{skipped_finalized} finalized event(s) unchanged')
    if parts:
        flash('. '.join(parts) + '.', 'success')

    log_action('generate_college_heats', 'tournament', tournament_id,
               {'generated': generated, 'skipped_open': skipped_open,
                'skipped_finalized': skipped_finalized, 'errors': errors})
    return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/move-competitor', methods=['POST'])
def move_competitor_between_heats(tournament_id, event_id):
    """Move a competitor between heats (and mirrored dual run heat, if needed)."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    try:
        competitor_id = int(request.form.get('competitor_id', ''))
        from_heat_id = int(request.form.get('from_heat_id', ''))
        to_heat_id = int(request.form.get('to_heat_id', ''))
    except (TypeError, ValueError):
        flash('Invalid move request.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    from_heat = Heat.query.get_or_404(from_heat_id)
    to_heat = Heat.query.get_or_404(to_heat_id)
    if from_heat.event_id != event.id or to_heat.event_id != event.id:
        abort(404)
    if from_heat.id == to_heat.id:
        flash('Select a different destination heat.', 'warning')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    # Lock check — don't move into a heat that's being scored by another judge
    user_id = _current_user_id()
    if to_heat.is_locked() and to_heat.locked_by_user_id != (user_id or -1):
        flash('Destination heat is being scored by another judge.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    # Capacity check — don't overfill the destination heat
    max_cap = _max_per_heat(event)
    if len(to_heat.get_competitors()) >= max_cap:
        flash(f'Destination heat is full ({max_cap} competitors max).', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    run_numbers = [1, 2] if event.requires_dual_runs else [from_heat.run_number]
    from_pairs = []
    to_pairs = []
    for run_number in run_numbers:
        source = event.heats.filter_by(heat_number=from_heat.heat_number, run_number=run_number).first()
        target = event.heats.filter_by(heat_number=to_heat.heat_number, run_number=run_number).first()
        if not source or not target:
            flash('Could not find matching source/destination heats for move.', 'error')
            return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))
        from_pairs.append(source)
        to_pairs.append(target)

    comp_type = event.event_type  # 'pro' or 'college'
    for source, target in zip(from_pairs, to_pairs):
        source_ids = source.get_competitors()
        if competitor_id not in source_ids:
            flash('Competitor is not in the selected source heat.', 'error')
            return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))
        target_ids = target.get_competitors()
        if competitor_id in target_ids:
            continue
        source.remove_competitor(competitor_id)
        target.add_competitor(competitor_id)

        source_assignments = source.get_stand_assignments()
        source_assignments.pop(str(competitor_id), None)
        source.stand_assignments = json.dumps(source_assignments)

        target_assignments = target.get_stand_assignments()
        target_assignments[str(competitor_id)] = _next_open_stand(target_ids, target_assignments, event)
        target.stand_assignments = json.dumps(target_assignments)

        source.sync_assignments(comp_type)
        target.sync_assignments(comp_type)

    db.session.commit()

    # Check for gear-sharing conflicts created by this move (warn, don't block).
    if event.event_type == 'pro':
        try:
            from models import Event as EventModel
            from services.gear_sharing import competitors_share_gear_for_event
            mover = ProCompetitor.query.get(competitor_id)
            if mover:
                mover_gear = mover.get_gear_sharing()
                all_events = EventModel.query.filter_by(tournament_id=event.tournament_id).all()
                final_to_heat = to_pairs[0] if to_pairs else to_heat
                target_ids = final_to_heat.get_competitors()
                target_comps = ProCompetitor.query.filter(
                    ProCompetitor.id.in_([cid for cid in target_ids if cid != competitor_id])
                ).all()
                conflicts = []
                for tc in target_comps:
                    if competitors_share_gear_for_event(
                        mover.name, mover_gear,
                        tc.name, tc.get_gear_sharing(),
                        event,
                        all_events=all_events,
                    ):
                        conflicts.append(tc.name)
                if conflicts:
                    flash(
                        f'Warning: {mover.name} shares gear with '
                        f'{", ".join(conflicts)} who are already in the destination heat. '
                        f'This may cause a scheduling conflict.',
                        'warning',
                    )
        except Exception:
            pass  # Gear check failure should not block the move

    flash('Competitor moved successfully.', 'success')
    return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))


def _next_open_stand(target_ids: list, assignments: dict, event: Event):
    """Return next available stand number for a target heat."""
    stand_config = config.STAND_CONFIGS.get(event.stand_type or '', {})
    total = event.max_stands if event.max_stands is not None else stand_config.get('total', max(len(target_ids), 1))
    if event.stand_type == 'saw_hand':
        total = min(total, 4)
    if event.event_type == 'college' and _normalize_name(event.name) == _normalize_name('Stock Saw'):
        available = [7, 8]
    elif stand_config.get('specific_stands'):
        available = list(stand_config['specific_stands'])
    else:
        available = list(range(1, total + 1))
    used = {int(v) for v in assignments.values() if str(v).isdigit()}
    for stand in available:
        if stand not in used:
            return stand
    return available[0] if available else None


def _current_user_id() -> int | None:
    """Return authenticated user id, or None for anonymous/dev sessions."""
    if current_user and current_user.is_authenticated:
        return current_user.id
    return None


# ---------------------------------------------------------------------------
# Scratch competitor from heat
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/scratch-competitor', methods=['POST'])
def scratch_competitor(tournament_id, event_id):
    """Scratch a competitor from a heat (day-of no-show operation).

    Removes the competitor from the Heat.competitors JSON and stand assignments,
    sets their EventResult.status to 'scratched', cleans gear-sharing references,
    and recalculates positions if the event has scored results.
    For dual-run events, mirrors the scratch across both run heats.
    """
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    try:
        competitor_id = int(request.form.get('competitor_id', ''))
        heat_id = int(request.form.get('heat_id', ''))
    except (TypeError, ValueError):
        flash('Invalid scratch request.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    heat = Heat.query.get_or_404(heat_id)
    if heat.event_id != event.id:
        abort(404)

    # Lock check — don't mutate a heat that's being scored by another judge
    user_id = _current_user_id()
    if heat.is_locked() and heat.locked_by_user_id != (user_id or -1):
        flash('This heat is currently being scored by another judge. Try again after they finish.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    if competitor_id not in heat.get_competitors():
        flash('Competitor is not in the selected heat.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    # Look up competitor name for flash message
    if event.event_type == 'college':
        comp = CollegeCompetitor.query.get(competitor_id)
    else:
        comp = ProCompetitor.query.get(competitor_id)
    comp_name = comp.display_name if comp else f'Competitor #{competitor_id}'

    try:
        # For dual-run events, scratch from both run heats
        run_numbers = [1, 2] if event.requires_dual_runs else [heat.run_number]
        comp_type = event.event_type  # 'pro' or 'college'

        for run_number in run_numbers:
            target = event.heats.filter_by(heat_number=heat.heat_number, run_number=run_number).first()
            if not target:
                continue
            if competitor_id not in target.get_competitors():
                continue

            target.remove_competitor(competitor_id)

            # Free stand assignment
            assignments = target.get_stand_assignments()
            assignments.pop(str(competitor_id), None)
            target.stand_assignments = json.dumps(assignments)

            target.sync_assignments(comp_type)

            # Auto-complete empty heats to prevent finalization block (Codex #7)
            if len(target.get_competitors()) == 0:
                target.status = 'completed'

        # Set EventResult.status = 'scratched' (do NOT delete the row)
        result = EventResult.query.filter_by(
            event_id=event.id,
            competitor_id=competitor_id,
            competitor_type=comp_type,
        ).first()
        if result:
            result.status = 'scratched'

        # Clean gear-sharing references on other active competitors
        if comp:
            try:
                from services.gear_sharing import cleanup_scratched_gear_entries
                tournament = Tournament.query.get(tournament_id)
                cleanup_scratched_gear_entries(tournament, scratched_competitor=comp)
            except Exception:
                pass  # Gear cleanup failure should not block the scratch

        # Recalculate positions if event has scored results (Codex #1)
        has_scored = EventResult.query.filter_by(event_id=event.id, status='completed').first() is not None
        if has_scored:
            if event.is_finalized:
                event.is_finalized = False
                event.status = 'in_progress'
            try:
                import services.scoring_engine as engine
                engine.calculate_positions(event)
            except Exception:
                pass  # Position recalc failure should not block the scratch

        invalidate_tournament_caches(tournament_id)
        log_action('heat_scratch', 'event', event.id, {
            'competitor_id': competitor_id,
            'competitor_name': comp_name,
            'heat_number': heat.heat_number,
            'event_name': event.display_name,
            'user_id': user_id,
        })

        db.session.commit()

        # Flash messages
        flash(f'{comp_name} scratched from {event.display_name} Heat {heat.heat_number}.', 'success')
        if event.is_partnered:
            flash(f'Warning: This is a partnered event. {comp_name}\'s partner may need to be scratched too.', 'warning')
        # Check if any heat became empty
        for run_number in run_numbers:
            target = event.heats.filter_by(heat_number=heat.heat_number, run_number=run_number).first()
            if target and len(target.get_competitors()) == 0:
                flash(f'Heat {heat.heat_number} is now empty and marked complete.', 'info')
                break

    except Exception as e:
        db.session.rollback()
        flash(f'Error scratching competitor: {e}', 'error')

    return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))


# ---------------------------------------------------------------------------
# Add late entry to a specific heat
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/add-to-heat', methods=['POST'])
def add_to_heat(tournament_id, event_id):
    """Add a competitor to an existing heat (day-of late entry).

    Validates capacity, creates EventResult if missing, and mirrors
    dual-run events. Blocked once the show is active for that division.
    """
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    try:
        competitor_id = int(request.form.get('competitor_id', ''))
        heat_id = int(request.form.get('heat_id', ''))
    except (TypeError, ValueError):
        flash('Invalid add request.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    heat = Heat.query.get_or_404(heat_id)
    if heat.event_id != event.id:
        abort(404)

    # Show-start lockout — additions blocked once the show is active
    if event.event_type == 'pro' and tournament.status == 'pro_active':
        flash('Cannot add competitors after the pro show has started.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))
    if event.event_type == 'college' and tournament.status == 'college_active':
        flash('Cannot add competitors after the college show has started.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    # Lock check
    user_id = _current_user_id()
    if heat.is_locked() and heat.locked_by_user_id != (user_id or -1):
        flash('This heat is currently being scored by another judge. Try again after they finish.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    # Capacity check
    max_cap = _max_per_heat(event)
    if len(heat.get_competitors()) >= max_cap:
        flash(f'Heat is full ({max_cap} competitors max).', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    # Verify competitor is already in the heat list — reject if duplicate
    if competitor_id in heat.get_competitors():
        flash('Competitor is already in this heat.', 'warning')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    # Look up competitor. Filter on tournament_id + status='active' at
    # the query so a tampered POST with an ID from another tournament
    # fails existence, not just the tournament_id compare below.
    if event.event_type == 'college':
        comp = CollegeCompetitor.query.filter_by(
            id=competitor_id, tournament_id=tournament_id, status='active'
        ).first()
    else:
        comp = ProCompetitor.query.filter_by(
            id=competitor_id, tournament_id=tournament_id, status='active'
        ).first()
    if not comp:
        flash('Competitor not found in this tournament or not active.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    # Event enrollment check: the competitor must actually be entered
    # in this event. Without this gate, a late-add POST could insert a
    # competitor who never signed up, giving them an EventResult row
    # and potentially a payout.
    entered = set()
    for raw in comp.get_events_entered():
        try:
            entered.add(int(raw))
        except (TypeError, ValueError):
            continue
    if event.id not in entered:
        flash(
            f'{comp.display_name} is not entered in {event.display_name}. '
            f'Enter the competitor in this event from the registration page first.',
            'error',
        )
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    # Gender check for gendered events
    if event.gender and comp.gender and comp.gender != event.gender:
        flash(
            f'{comp.display_name} cannot be added to {event.display_name} '
            f'(gender mismatch).',
            'error',
        )
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    comp_name = comp.display_name

    try:
        comp_type = event.event_type

        # Ensure EventResult row exists
        result = EventResult.query.filter_by(
            event_id=event.id, competitor_id=competitor_id, competitor_type=comp_type,
        ).first()
        if not result:
            result = EventResult(
                event_id=event.id, competitor_id=competitor_id,
                competitor_type=comp_type, competitor_name=comp.name,
                status='pending',
            )
            db.session.add(result)
        elif result.status == 'scratched':
            # Re-add: reset status but preserve raw result values (Codex #4)
            result.status = 'pending'
            result.final_position = None
            result.points_awarded = 0
            result.payout_amount = 0.0
            result.throwoff_pending = False
            result.is_flagged = False

        # For dual-run events, add to both run heats
        run_numbers = [1, 2] if event.requires_dual_runs else [heat.run_number]
        for run_number in run_numbers:
            target = event.heats.filter_by(heat_number=heat.heat_number, run_number=run_number).first()
            if not target:
                continue
            if competitor_id in target.get_competitors():
                continue

            target.add_competitor(competitor_id)
            target_ids = target.get_competitors()
            assignments = target.get_stand_assignments()
            assignments[str(competitor_id)] = _next_open_stand(target_ids, assignments, event)
            target.stand_assignments = json.dumps(assignments)
            target.sync_assignments(comp_type)

        # Gear-sharing conflict check (warn, don't block)
        if event.event_type == 'pro':
            try:
                from services.gear_sharing import competitors_share_gear_for_event
                mover_gear = comp.get_gear_sharing() if hasattr(comp, 'get_gear_sharing') else {}
                all_events = Event.query.filter_by(tournament_id=tournament_id).all()
                target_comps_ids = [cid for cid in heat.get_competitors() if cid != competitor_id]
                target_comps = ProCompetitor.query.filter(
                    ProCompetitor.id.in_(target_comps_ids)
                ).all() if target_comps_ids else []
                conflicts = [
                    tc.name for tc in target_comps
                    if competitors_share_gear_for_event(
                        comp.name, mover_gear,
                        tc.name, tc.get_gear_sharing(),
                        event, all_events=all_events,
                    )
                ]
                if conflicts:
                    flash(
                        f'Warning: {comp_name} shares gear with '
                        f'{", ".join(conflicts)} in this heat.',
                        'warning',
                    )
            except Exception:
                pass

        invalidate_tournament_caches(tournament_id)
        log_action('heat_add_competitor', 'event', event.id, {
            'competitor_id': competitor_id,
            'competitor_name': comp_name,
            'heat_number': heat.heat_number,
            'event_name': event.display_name,
            'user_id': user_id,
        })

        db.session.commit()
        flash(f'{comp_name} added to {event.display_name} Heat {heat.heat_number}.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error adding competitor: {e}', 'error')

    return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))


# ---------------------------------------------------------------------------
# Delete empty heat
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/delete-heat/<int:heat_id>', methods=['POST'])
def delete_heat(tournament_id, event_id, heat_id):
    """Delete an empty heat and renumber remaining heats."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    heat = Heat.query.get_or_404(heat_id)
    if heat.event_id != event.id:
        abort(404)

    # Lock check
    user_id = _current_user_id()
    if heat.is_locked() and heat.locked_by_user_id != (user_id or -1):
        flash('This heat is currently being scored by another judge.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    if len(heat.get_competitors()) > 0:
        flash('Cannot delete a heat that has competitors. Scratch all competitors first.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    heat_number = heat.heat_number

    try:
        # For dual-run events, also delete the matching run_number=2 heat
        heats_to_delete = [heat]
        if event.requires_dual_runs:
            partner_run = event.heats.filter_by(
                heat_number=heat_number,
                run_number=2 if heat.run_number == 1 else 1,
            ).first()
            if partner_run:
                if len(partner_run.get_competitors()) > 0:
                    flash(
                        f'Cannot delete: the matching Run {partner_run.run_number} heat still has competitors.',
                        'error',
                    )
                    return redirect(url_for('scheduling.event_heats',
                                            tournament_id=tournament_id, event_id=event_id))
                heats_to_delete.append(partner_run)

        for h in heats_to_delete:
            HeatAssignment.query.filter_by(heat_id=h.id).delete(synchronize_session=False)
            if h.flight_id:
                h.flight_id = None
                h.flight_position = None
            db.session.delete(h)

        db.session.flush()

        # Renumber remaining heats sequentially
        remaining = (event.heats
                     .order_by(Heat.heat_number, Heat.run_number)
                     .all())
        # Group by run_number=1 to get unique heat numbers, then assign sequentially
        seen_numbers = {}
        new_number = 1
        for h in remaining:
            if h.heat_number not in seen_numbers:
                seen_numbers[h.heat_number] = new_number
                new_number += 1
            h.heat_number = seen_numbers[h.heat_number]

        invalidate_tournament_caches(tournament_id)
        log_action('heat_deleted', 'event', event.id, {
            'deleted_heat_number': heat_number,
            'event_name': event.display_name,
            'user_id': user_id,
        })

        db.session.commit()
        flash(f'Heat {heat_number} deleted from {event.display_name}. '
              f'Heats renumbered. Reprint heat sheets if already distributed.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting heat: {e}', 'error')

    return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))


# ---------------------------------------------------------------------------
# #19 — HeatAssignment sync check / fix
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/heats/sync-check')
def heat_sync_check(tournament_id, event_id):
    """Return JSON showing mismatches between Heat.competitors JSON and HeatAssignment rows."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    mismatches = []
    for heat in event.heats.order_by(Heat.heat_number, Heat.run_number).all():
        json_ids = set(heat.get_competitors())
        table_ids = set(
            a.competitor_id
            for a in HeatAssignment.query.filter_by(heat_id=heat.id).all()
        )
        if json_ids != table_ids:
            mismatches.append({
                'heat_id': heat.id,
                'heat_number': heat.heat_number,
                'run_number': heat.run_number,
                'json_only': sorted(json_ids - table_ids),
                'table_only': sorted(table_ids - json_ids),
            })

    return jsonify({'event_id': event_id, 'mismatches': mismatches, 'ok': len(mismatches) == 0})


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/heats/sync-fix', methods=['POST'])
def heat_sync_fix(tournament_id, event_id):
    """Reconcile HeatAssignment rows to match authoritative Heat.competitors JSON."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    fixed = 0
    for heat in event.heats.all():
        json_ids = heat.get_competitors()
        HeatAssignment.query.filter_by(heat_id=heat.id).delete()
        comp_type = event.event_type  # 'pro' or 'college'
        assignments = heat.get_stand_assignments()
        for comp_id in json_ids:
            ha = HeatAssignment(
                heat_id=heat.id,
                competitor_id=comp_id,
                competitor_type=comp_type,
                stand_number=assignments.get(str(comp_id)),
            )
            db.session.add(ha)
        fixed += 1

    db.session.commit()
    log_action('heat_assignments_synced', 'event', event_id, {'heats_fixed': fixed})
    flash(f'HeatAssignment table synced for {fixed} heats.', 'success')
    return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))


# ---------------------------------------------------------------------------
# Hand-saw stand block alternation — admin safety valve + status page
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/heats/recompute-saw-blocks', methods=['POST'])
def recompute_saw_blocks(tournament_id):
    """Manually recompute hand-saw stand block assignments for the tournament."""
    from services.saw_block_assignment import assign_saw_blocks

    tournament = Tournament.query.get_or_404(tournament_id)
    try:
        summary = assign_saw_blocks(tournament)
        flash(
            f"Saw block assignments recomputed: {summary['heats_updated']} heats updated "
            f"({summary['friday_saw_heats']} Friday, {summary['saturday_saw_heats']} Saturday).",
            'success',
        )
        log_action('saw_blocks_recomputed', 'tournament', tournament_id, summary)
    except Exception as exc:
        flash(f'Saw block recompute failed: {exc}', 'error')

    return redirect(
        request.referrer
        or url_for('scheduling.event_list', tournament_id=tournament_id)
    )


@scheduling_bp.route('/<int:tournament_id>/saw-blocks-status')
def saw_blocks_status(tournament_id):
    """Per-day status page showing block assignment for every hand-saw heat."""
    from services.saw_block_assignment import BLOCK_A, SAW_STAND_TYPE
    from services.schedule_builder import (
        get_friday_ordered_heats,
        get_saturday_ordered_heats,
    )

    tournament = Tournament.query.get_or_404(tournament_id)

    def _rows(ordered_heats):
        rows = []
        for heat in ordered_heats:
            event = heat.event
            if not event or event.stand_type != SAW_STAND_TYPE:
                continue
            assignments = heat.get_stand_assignments()
            used_stands = sorted({
                int(v) for v in assignments.values()
                if v is not None and int(v) > 0
            })
            if not used_stands:
                block_label = '?'
            else:
                block_label = 'A' if used_stands[0] in BLOCK_A else 'B'
            comp_ids = heat.get_competitors()
            if comp_ids:
                if event.event_type == 'college':
                    name_map = {
                        c.id: c.display_name
                        for c in CollegeCompetitor.query.filter(
                            CollegeCompetitor.id.in_(comp_ids)
                        ).all()
                    }
                else:
                    name_map = {
                        c.id: c.display_name
                        for c in ProCompetitor.query.filter(
                            ProCompetitor.id.in_(comp_ids)
                        ).all()
                    }
            else:
                name_map = {}
            names = [name_map.get(int(cid), f'ID:{cid}') for cid in comp_ids]
            rows.append({
                'heat_id': heat.id,
                'event_name': event.display_name,
                'heat_number': heat.heat_number,
                'block': block_label,
                'stands': used_stands,
                'competitors': names,
            })
        return rows

    friday_rows = _rows(get_friday_ordered_heats(tournament))
    saturday_rows = _rows(get_saturday_ordered_heats(tournament))

    # Re-number run order within each day so the page shows the saw-heat
    # sequence (1, 2, 3, ...) rather than raw Heat.id or original heat_number.
    for idx, row in enumerate(friday_rows, start=1):
        row['run_order'] = idx
    for idx, row in enumerate(saturday_rows, start=1):
        row['run_order'] = idx

    return render_template(
        'scheduling/saw_blocks_status.html',
        tournament=tournament,
        friday_rows=friday_rows,
        saturday_rows=saturday_rows,
    )
