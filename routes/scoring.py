"""
Scoring routes — heat result entry, event finalization, live poll, heat locking,
undo, throw-off resolution, bulk CSV import, payout templates, and next-event navigation.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, abort, jsonify, session, Response, stream_with_context)
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from database import db
from models import Tournament, Event, EventResult, Heat
from models.competitor import CollegeCompetitor, ProCompetitor
from models.payout_template import PayoutTemplate
import config
import strings as text
from services.audit import log_action
from services.cache_invalidation import invalidate_tournament_caches
import services.scoring_engine as engine

scoring_bp = Blueprint('scoring', __name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event_for_tournament_or_404(tournament_id: int, event_id: int) -> Event:
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)
    return event


def _heat_for_tournament_or_404(tournament_id: int, heat_id: int) -> Heat:
    heat = Heat.query.get_or_404(heat_id)
    if not heat.event or heat.event.tournament_id != tournament_id:
        abort(404)
    return heat


def _is_async() -> bool:
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _current_user_id() -> int | None:
    """Return authenticated user id, or None for anonymous/dev sessions."""
    if current_user and current_user.is_authenticated:
        return current_user.id
    return None


def _competitor_lookup(event: Event, competitor_ids: list[int]) -> dict:
    if event.event_type == 'college':
        comps = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(competitor_ids)).all()
    else:
        comps = ProCompetitor.query.filter(ProCompetitor.id.in_(competitor_ids)).all()
    return {c.id: c for c in comps}


def _existing_results(event: Event, competitor_ids: list[int]) -> dict:
    rows = EventResult.query.filter(
        EventResult.event_id == event.id,
        EventResult.competitor_id.in_(competitor_ids),
        EventResult.competitor_type == event.event_type
    ).all()
    return {r.competitor_id: r for r in rows}


# ---------------------------------------------------------------------------
# Core heat result saver
# ---------------------------------------------------------------------------

def _save_heat_results_submission(tournament_id: int, heat: Heat, event: Event) -> dict:
    """Parse posted form data, validate, and write EventResult rows."""
    competitor_ids = [int(cid) for cid in heat.get_competitors()]

    # Optimistic lock check
    posted_version = request.form.get('heat_version', type=int)
    if posted_version is None or posted_version != heat.version_id:
        return {
            'ok': False, 'category': 'error',
            'message': 'This heat changed in another session. Reload and re-enter results.',
            'redirect_url': url_for('scoring.enter_heat_results',
                                    tournament_id=tournament_id, heat_id=heat.id),
            'status_code': 409,
        }

    result_by_comp = _existing_results(event, competitor_ids)
    comp_lookup = _competitor_lookup(event, competitor_ids)
    changes = 0
    invalid = []

    try:
        for comp_id in competitor_ids:
            status = request.form.get(f'status_{comp_id}', 'completed')

            # -- parse primary result --
            raw = request.form.get(f'result_{comp_id}')
            if not raw:
                continue
            try:
                parsed = float(raw)
            except (TypeError, ValueError):
                invalid.append((comp_id, raw))
                continue

            # -- get or create result row --
            result = result_by_comp.get(comp_id)
            if not result:
                comp = comp_lookup.get(comp_id)
                result = EventResult(
                    event_id=event.id,
                    competitor_id=comp_id,
                    competitor_type=event.event_type,
                    competitor_name=comp.name if comp else f'Unknown ({comp_id})',
                )
                db.session.add(result)
                result_by_comp[comp_id] = result

            # -- store run values --
            if event.requires_dual_runs:
                if heat.run_number == 1:
                    result.run1_value = parsed
                else:
                    result.run2_value = parsed
                result.calculate_best_run(event.scoring_order)

            elif event.requires_triple_runs:
                run_slot = request.form.get(f'run_slot_{comp_id}', '1')
                if run_slot == '2':
                    result.run2_value = parsed
                elif run_slot == '3':
                    result.run3_value = parsed
                else:
                    result.run1_value = parsed
                # Also pick up additional run slots submitted together
                for slot, field in [('2', f'result2_{comp_id}'), ('3', f'result3_{comp_id}')]:
                    raw2 = request.form.get(field)
                    if raw2:
                        try:
                            v = float(raw2)
                            if slot == '2':
                                result.run2_value = v
                            else:
                                result.run3_value = v
                        except (TypeError, ValueError):
                            pass
                result.calculate_cumulative_score()

            else:
                result.result_value = parsed

            # -- tiebreak value (Hard-Hit events) --
            if event.is_hard_hit:
                raw_tb = request.form.get(f'tiebreak_{comp_id}')
                if raw_tb:
                    try:
                        result.tiebreak_value = float(raw_tb)
                    except (TypeError, ValueError):
                        pass

            result.status = status

            # Audit: log if result already existed (edit scenario)
            if result.id:
                log_action('score_edited', 'event_result', result.id,
                           {'event_id': event.id, 'heat_id': heat.id,
                            'new_value': result.result_value,
                            'judge_user_id': _current_user_id()})
                # If event was previously finalized, reset finalization
                if event.is_finalized:
                    event.is_finalized = False
                    event.status = 'in_progress'

            changes += 1

        if changes == 0:
            return {
                'ok': False, 'category': 'warning',
                'message': 'No result values were entered; heat remains pending.',
                'redirect_url': url_for('scoring.enter_heat_results',
                                        tournament_id=tournament_id, heat_id=heat.id),
                'status_code': 400,
            }

        heat.status = 'completed'
        # Release edit lock on successful save
        heat.release_lock(_current_user_id() or 0)

        # Auto-finalize when all heats in event are complete (both runs for dual-run events)
        all_heats_complete = all(h.status == 'completed' for h in event.heats.all())
        if all_heats_complete:
            engine.calculate_positions(event)

        log_action('heat_results_saved', 'heat', heat.id,
                   {'event_id': event.id, 'result_updates': changes,
                    'judge_user_id': _current_user_id()})
        db.session.commit()

    except (StaleDataError, IntegrityError):
        db.session.rollback()
        return {
            'ok': False, 'category': 'error',
            'message': 'Concurrent edit detected while saving. Reload and try again.',
            'redirect_url': url_for('scoring.enter_heat_results',
                                    tournament_id=tournament_id, heat_id=heat.id),
            'status_code': 409,
        }

    invalidate_tournament_caches(tournament_id)

    # Store undo token in session (30-second window)
    session[f'undo_heat_{heat.id}'] = {
        'heat_id': heat.id,
        'event_id': event.id,
        'saved_at': datetime.now(timezone.utc).isoformat(),
    }

    if invalid:
        return {
            'ok': True, 'category': 'warning',
            'message': f"Heat saved with {len(invalid)} invalid value(s) skipped.",
            'redirect_url': url_for('scoring.event_results',
                                    tournament_id=tournament_id, event_id=event.id),
            'status_code': 200,
            'undo_heat_id': heat.id,
        }
    return {
        'ok': True, 'category': 'success',
        'message': text.FLASH['heat_saved'],
        'redirect_url': url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=event.id),
        'status_code': 200,
        'undo_heat_id': heat.id,
    }


# ---------------------------------------------------------------------------
# Routes: navigation helpers
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/next-heat')
def next_unscored_heat(tournament_id, event_id):
    """Redirect to first pending heat for an event."""
    event = _event_for_tournament_or_404(tournament_id, event_id)
    heat = (Heat.query.filter_by(event_id=event.id, status='pending')
            .order_by(Heat.heat_number, Heat.run_number).first())
    if heat:
        return redirect(url_for('scoring.enter_heat_results',
                                tournament_id=tournament_id, heat_id=heat.id))
    return redirect(url_for('scoring.event_results',
                            tournament_id=tournament_id, event_id=event_id))


@scoring_bp.route('/<int:tournament_id>/next-incomplete-event')
def next_incomplete_event(tournament_id):
    """Jump to the first event with pending heats in this tournament."""
    tournament = Tournament.query.get_or_404(tournament_id)
    # Find events that have at least one pending heat
    incomplete = (Event.query
                  .join(Heat, Heat.event_id == Event.id)
                  .filter(Event.tournament_id == tournament_id,
                          Heat.status == 'pending')
                  .order_by(Event.id)
                  .first())
    if incomplete:
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=incomplete.id))
    flash('All events are complete — no pending heats remain.', 'success')
    return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))


# ---------------------------------------------------------------------------
# Routes: event results view
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/results')
def event_results(tournament_id, event_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    event = _event_for_tournament_or_404(tournament_id, event_id)
    heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
    results = event.get_results_sorted()
    payout_templates = PayoutTemplate.query.order_by(PayoutTemplate.name).all()
    throwoff_pending = engine.pending_throwoffs(event) if event.is_axe_throw_cumulative else []

    return render_template('scoring/event_results.html',
                           tournament=tournament, event=event,
                           heats=heats, results=results,
                           payout_templates=payout_templates,
                           throwoff_pending=throwoff_pending)


# ---------------------------------------------------------------------------
# Routes: live standings poll
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/live-standings')
def live_standings(tournament_id, event_id):
    """JSON endpoint polled every 10 s by the event_results page."""
    event = _event_for_tournament_or_404(tournament_id, event_id)
    return jsonify(engine.live_standings_data(event))


# ---------------------------------------------------------------------------
# Routes: finalize preview + finalize
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/finalize-preview')
def finalize_preview(tournament_id, event_id):
    """JSON: provisional standings + outlier warnings for the confirmation modal."""
    event = _event_for_tournament_or_404(tournament_id, event_id)
    preview = engine.preview_positions(event)
    outliers = engine.outlier_check(event)
    throwoffs = [{'id': r.id, 'name': r.competitor_name, 'score': r.result_value}
                 for r in engine.pending_throwoffs(event)]
    return jsonify({'preview': preview, 'outliers': outliers, 'throwoffs': throwoffs})


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/finalize', methods=['POST'])
def finalize_event(tournament_id, event_id):
    event = _event_for_tournament_or_404(tournament_id, event_id)

    try:
        with db.session.begin_nested():   # savepoint — rolls back to pre-finalize if error
            engine.calculate_positions(event)
            log_action('event_finalized', 'event', event.id,
                       {'tournament_id': tournament_id,
                        'judge_user_id': _current_user_id()})
        db.session.commit()
    except (StaleDataError, IntegrityError):
        db.session.rollback()
        if _is_async():
            return jsonify({'ok': False, 'message': 'Concurrent update during finalization.'}), 409
        flash('Concurrent update detected while finalizing event.', 'error')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=event_id))

    invalidate_tournament_caches(tournament_id)
    if _is_async():
        return jsonify({'ok': True, 'message': f'{event.display_name} finalized.'})
    flash(text.FLASH['event_finalized'].format(event_name=event.display_name), 'success')
    return redirect(url_for('scoring.event_results',
                            tournament_id=tournament_id, event_id=event_id))


# ---------------------------------------------------------------------------
# Routes: heat entry
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/heat/<int:heat_id>/enter', methods=['GET', 'POST'])
def enter_heat_results(tournament_id, heat_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    heat = _heat_for_tournament_or_404(tournament_id, heat_id)
    event = heat.event

    if request.method == 'POST':
        outcome = _save_heat_results_submission(tournament_id=tournament_id,
                                                heat=heat, event=event)
        if _is_async():
            return jsonify({k: outcome[k] for k in
                            ('ok', 'message', 'redirect_url', 'category',
                             'undo_heat_id') if k in outcome}), outcome['status_code']
        flash(outcome['message'], outcome['category'])
        redirect_url = outcome['redirect_url']
        if outcome.get('undo_heat_id'):
            redirect_url += f'?undo_heat={outcome["undo_heat_id"]}'
        return redirect(redirect_url)

    # -- GET: acquire heat lock --
    user_id = _current_user_id()
    lock_owner = None
    lock_blocked = False
    if user_id:
        if heat.is_locked() and heat.locked_by_user_id != user_id:
            lock_blocked = True
            from models.user import User
            locker = User.query.get(heat.locked_by_user_id)
            lock_owner = locker.username if locker else f'User #{heat.locked_by_user_id}'
        else:
            heat.acquire_lock(user_id)
            db.session.commit()

    competitor_ids = heat.get_competitors()
    comp_lookup = _competitor_lookup(event, competitor_ids)
    result_lookup = _existing_results(event, competitor_ids)

    # For run-2 heats, also fetch run-1 results to show context
    run1_results: dict[int, float | None] = {}
    if event.requires_dual_runs and heat.run_number == 2:
        run1_heat = (Heat.query.filter_by(event_id=event.id, heat_number=heat.heat_number,
                                         run_number=1).first())
        if run1_heat:
            r1_results = _existing_results(event, competitor_ids)
            run1_results = {cid: r.run1_value for cid, r in r1_results.items()
                            if r.run1_value is not None}

    competitors = []
    for comp_id in competitor_ids:
        comp = comp_lookup.get(comp_id)
        result = result_lookup.get(comp_id)
        if comp:
            competitors.append({
                'id': comp_id,
                'name': comp.name,
                'stand': heat.get_stand_for_competitor(comp_id),
                'headshot': getattr(comp, 'headshot_filename', None),
                'existing_result': result.result_value if result else None,
                'existing_run1': result.run1_value if result else None,
                'existing_run2': result.run2_value if result else None,
                'existing_run3': result.run3_value if result else None,
                'existing_tiebreak': result.tiebreak_value if result else None,
                'existing_status': result.status if result else 'completed',
                'run1_context': run1_results.get(comp_id),  # for run-2 display
            })

    next_heat = (Heat.query.filter_by(event_id=event.id, status='pending')
                 .filter(Heat.id != heat.id)
                 .order_by(Heat.heat_number, Heat.run_number).first())
    next_heat_url = (url_for('scoring.enter_heat_results', tournament_id=tournament_id,
                             heat_id=next_heat.id) if next_heat else None)

    return render_template('scoring/enter_heat.html',
                           tournament=tournament, heat=heat, event=event,
                           competitors=competitors, heat_version=heat.version_id,
                           next_heat_url=next_heat_url,
                           lock_blocked=lock_blocked, lock_owner=lock_owner)


# ---------------------------------------------------------------------------
# Routes: heat lock / unlock
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/heat/<int:heat_id>/release-lock', methods=['POST'])
def release_heat_lock(tournament_id, heat_id):
    heat = _heat_for_tournament_or_404(tournament_id, heat_id)
    user_id = _current_user_id()
    if user_id:
        heat.release_lock(user_id)
        db.session.commit()
    if _is_async():
        return jsonify({'ok': True})
    return redirect(url_for('scoring.event_results',
                            tournament_id=tournament_id, event_id=heat.event_id))


# ---------------------------------------------------------------------------
# Routes: undo last heat save
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/heat/<int:heat_id>/undo', methods=['POST'])
def undo_heat_save(tournament_id, heat_id):
    """Revert a heat to pending and delete its EventResult rows (within 30-second window)."""
    token = session.get(f'undo_heat_{heat_id}')
    if not token:
        if _is_async():
            return jsonify({'ok': False, 'message': 'Undo window has expired.'}), 400
        flash('Undo window has expired.', 'warning')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id,
                                event_id=request.form.get('event_id', 0)))

    saved_at_str = token['saved_at']
    # Strip sub-second precision and Z suffix for fromisoformat compat (Python < 3.11)
    saved_at_str = saved_at_str.split('.')[0].rstrip('Z')
    try:
        saved_at = datetime.fromisoformat(saved_at_str).replace(tzinfo=timezone.utc)
    except ValueError:
        saved_at = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    if (now - saved_at).total_seconds() > 30:
        session.pop(f'undo_heat_{heat_id}', None)
        if _is_async():
            return jsonify({'ok': False, 'message': 'Undo window (30 s) has expired.'}), 400
        flash('Undo window (30 s) has expired.', 'warning')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id,
                                event_id=token['event_id']))

    heat = _heat_for_tournament_or_404(tournament_id, heat_id)
    event = heat.event
    competitor_ids = heat.get_competitors()

    EventResult.query.filter(
        EventResult.event_id == event.id,
        EventResult.competitor_id.in_(competitor_ids),
        EventResult.competitor_type == event.event_type,
    ).delete(synchronize_session='fetch')

    heat.status = 'pending'
    event.status = 'in_progress'
    event.is_finalized = False
    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    session.pop(f'undo_heat_{heat_id}', None)

    if _is_async():
        return jsonify({'ok': True, 'message': 'Heat results reverted to pending.'})
    flash('Heat results undone — re-enter when ready.', 'info')
    return redirect(url_for('scoring.enter_heat_results',
                            tournament_id=tournament_id, heat_id=heat_id))


# ---------------------------------------------------------------------------
# Routes: throw-off resolution (axe throw)
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/throwoff', methods=['POST'])
def record_throwoff(tournament_id, event_id):
    """Record judge-assigned positions after an axe throw throw-off."""
    event = _event_for_tournament_or_404(tournament_id, event_id)
    position_map: dict[int, int] = {}
    for key, val in request.form.items():
        if key.startswith('throwoff_pos_'):
            try:
                result_id = int(key.replace('throwoff_pos_', ''))
                position_map[result_id] = int(val)
            except (TypeError, ValueError):
                pass

    if not position_map:
        flash('No throw-off positions submitted.', 'warning')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=event_id))

    engine.record_throwoff_result(event, position_map)
    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    flash('Throw-off positions recorded.', 'success')
    return redirect(url_for('scoring.event_results',
                            tournament_id=tournament_id, event_id=event_id))


# ---------------------------------------------------------------------------
# Routes: bulk CSV import
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/import-results', methods=['GET', 'POST'])
def import_results(tournament_id, event_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    event = _event_for_tournament_or_404(tournament_id, event_id)

    if request.method == 'POST':
        f = request.files.get('csv_file')
        csv_text = ''
        if f and f.filename:
            csv_text = f.read().decode('utf-8', errors='replace')
        elif request.form.get('csv_text'):
            csv_text = request.form['csv_text']

        if not csv_text.strip():
            flash('No CSV data provided.', 'warning')
            return redirect(url_for('scoring.import_results',
                                    tournament_id=tournament_id, event_id=event_id))

        result = engine.import_results_from_csv(event, csv_text)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('results_bulk_imported', 'event', event.id,
                   {'imported': result['imported'], 'skipped': result['skipped']})

        for err in result['errors']:
            flash(err, 'warning')
        flash(f"Imported {result['imported']} result(s), skipped {result['skipped']}.", 'success')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=event_id))

    return render_template('scoring/import_results.html',
                           tournament=tournament, event=event)


# ---------------------------------------------------------------------------
# Routes: offline ops
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/offline-ops')
def offline_ops(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    events = tournament.events.order_by(Event.name).all()
    event_directory = {e.id: {'name': e.display_name, 'type': e.event_type} for e in events}
    return render_template('scoring/offline_ops.html',
                           tournament=tournament, event_directory=event_directory)


# ---------------------------------------------------------------------------
# Routes: configure payouts + payout templates
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/payouts', methods=['GET', 'POST'])
def configure_payouts(tournament_id, event_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    event = _event_for_tournament_or_404(tournament_id, event_id)

    if event.event_type != 'pro':
        flash(text.FLASH['pro_only_payouts'], 'error')
        return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))

    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action == 'apply_template':
            tpl_id = request.form.get('template_id', type=int)
            if engine.apply_payout_template(event, tpl_id):
                flash('Template applied.', 'success')
            else:
                flash('Template not found.', 'error')
            return redirect(url_for('scoring.configure_payouts',
                                    tournament_id=tournament_id, event_id=event_id))

        if action == 'delete_template':
            tpl_id = request.form.get('template_id', type=int)
            engine.delete_payout_template(tpl_id)
            flash('Template deleted.', 'success')
            return redirect(url_for('scoring.configure_payouts',
                                    tournament_id=tournament_id, event_id=event_id))

        if action == 'save_template':
            tpl_name = (request.form.get('template_name') or '').strip()
            if not tpl_name:
                flash('Template name is required.', 'error')
                return redirect(url_for('scoring.configure_payouts',
                                        tournament_id=tournament_id, event_id=event_id))
            payouts = _parse_payout_form()
            if payouts is None:
                return redirect(url_for('scoring.configure_payouts',
                                        tournament_id=tournament_id, event_id=event_id))
            engine.save_payout_template(tpl_name, payouts)
            flash(f'Template "{tpl_name}" saved.', 'success')
            return redirect(url_for('scoring.configure_payouts',
                                    tournament_id=tournament_id, event_id=event_id))

        # Default: save to event
        payouts = _parse_payout_form()
        if payouts is None:
            return redirect(url_for('scoring.configure_payouts',
                                    tournament_id=tournament_id, event_id=event_id))
        try:
            event.set_payouts(payouts)
            log_action('payouts_configured', 'event', event.id,
                       {'positions': sorted(payouts.keys())})
            db.session.commit()
        except (StaleDataError, IntegrityError):
            db.session.rollback()
            flash('Another user changed this event while saving. Please retry.', 'error')
            return redirect(url_for('scoring.configure_payouts',
                                    tournament_id=tournament_id, event_id=event_id))

        invalidate_tournament_caches(tournament_id)
        flash(text.FLASH['payouts_saved'], 'success')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=event_id))

    templates = engine.list_payout_templates()
    return render_template('scoring/configure_payouts.html',
                           tournament=tournament, event=event,
                           current_payouts=event.get_payouts(),
                           templates=templates)


def _parse_payout_form() -> dict | None:
    """Parse payout positions 1–15 from POST form. Returns None and flashes on error."""
    payouts = {}
    for i in range(1, 16):
        amount = request.form.get(f'payout_{i}')
        if amount:
            try:
                payouts[str(i)] = float(amount)
            except (TypeError, ValueError):
                flash(f'Invalid payout amount for position {i}: {amount!r}', 'error')
                return None
    return payouts


# ---------------------------------------------------------------------------
# Routes: birling bracket
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/birling-bracket')
def birling_bracket(tournament_id, event_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    event = _event_for_tournament_or_404(tournament_id, event_id)

    if event.scoring_type != 'bracket':
        flash('This event does not use a bracket.', 'warning')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=event_id))

    from services.birling_bracket import BirlingBracket
    bb = BirlingBracket(event)
    bracket_data = bb.bracket_data
    comp_lookup = {str(c['id']): c['name'] for c in bracket_data.get('competitors', [])}

    return render_template('scoring/birling_bracket.html',
                           tournament=tournament, event=event,
                           bracket=bracket_data['bracket'],
                           placements=bracket_data.get('placements', {}),
                           comp_lookup=comp_lookup,
                           current_round=bracket_data.get('current_round', ''))
