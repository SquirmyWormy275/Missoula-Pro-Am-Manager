"""
Heat management routes: event_heats, generate_heats, generate_college_heats,
move_competitor_between_heats, heat_sync_check, heat_sync_fix.
"""
import json

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

import config
import strings as text
from database import db
from models import Event, Heat, HeatAssignment, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.audit import log_action

from . import (
    _build_signup_rows,
    _is_list_only_event,
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
                name_map = {c.id: c.name for c in CollegeCompetitor.query.filter(
                    CollegeCompetitor.id.in_(all_cids)).all()}
            else:
                name_map = {c.id: c.name for c in ProCompetitor.query.filter(
                    ProCompetitor.id.in_(all_cids)).all()}
            spacing_data = {
                'total_heats': len(run1_heats),
                'competitors': sorted(
                    [{'name': name_map.get(cid, f'ID:{cid}'), 'appearances': sorted(app)}
                     for cid, app in comp_appearances.items()],
                    key=lambda x: x['name'].lower(),
                ),
            }

    return render_template('scheduling/heats.html',
                           tournament=tournament,
                           event=event,
                           heats=heats,
                           signup_rows=signup_rows,
                           signup_list_mode=signup_list_mode,
                           spacing_data=spacing_data)


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/generate-heats', methods=['POST'])
def generate_heats(tournament_id, event_id):
    """Generate heats for an event using snake draft distribution."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    # Gear-sharing integrity gate for pro events: block generation when any enrolled
    # competitor has unstructured gear details but no structured gear_sharing map.
    # This prevents silently building heats with unresolved gear conflicts.
    if event.event_type == 'pro':
        from models import EventResult
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
    from services.heat_generator import generate_event_heats

    try:
        num_heats = generate_event_heats(event)
        db.session.commit()
        if _is_list_only_event(event):
            flash(f'{event.display_name} uses signups only (no heats).', 'success')
        else:
            flash(text.FLASH['heats_generated'].format(num_heats=num_heats, event_name=event.display_name), 'success')
    except Exception as e:
        db.session.rollback()
        flash(text.FLASH['heats_error'].format(error=str(e)), 'error')

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

    for event in events:
        if _is_list_only_event(event):
            skipped_open += 1
            continue
        if event.status == 'completed':
            skipped_completed += 1
            continue
        try:
            generate_event_heats(event)
            generated += 1
        except Exception as exc:
            if 'No competitors entered' in str(exc):
                skipped_open += 1
            else:
                errors += 1
                flash(f'Error generating heats for {event.display_name}: {exc}', 'error')

    db.session.commit()

    parts = []
    if generated:
        parts.append(f'Heats generated for {generated} event(s)')
    if skipped_open:
        parts.append(f'{skipped_open} signup-list event(s) skipped')
    if skipped_completed:
        parts.append(f'{skipped_completed} completed event(s) unchanged')
    if parts:
        flash('. '.join(parts) + '.', 'success')

    log_action('generate_college_heats', 'tournament', tournament_id,
               {'generated': generated, 'skipped_open': skipped_open, 'errors': errors})
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
