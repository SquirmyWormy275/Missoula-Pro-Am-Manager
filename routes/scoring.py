"""
Scoring routes — heat result entry, event finalization, live poll, heat locking,
undo, throw-off resolution, bulk CSV import, payout templates, and next-event navigation.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

import config
import services.scoring_engine as engine
import strings as text
from database import db
from models import Event, EventResult, Heat, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from models.payout_template import PayoutTemplate
from routes.api import write_limit
from services.audit import log_action
from services.cache_invalidation import invalidate_tournament_caches

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


def _push_strathmark_results(event: Event, tournament_id: int) -> None:
    """
    Attempt to push finalized results to STRATHMARK for eligible event types.

    Covers Change 2 (pro SB/UH) and Change 3 (college SB Speed / UH Speed).
    All STRATHMARK calls are non-blocking — failures are logged, never raised.
    """
    try:
        from services import strathmark_sync
        tournament = Tournament.query.get(tournament_id)
        year = tournament.year if tournament else 0

        if event.event_type == 'pro' and event.stand_type in ('standing_block', 'underhand'):
            strathmark_sync.push_pro_event_results(event, year)
        elif strathmark_sync.is_college_sb_uh_speed(event):
            strathmark_sync.push_college_event_results(event, year)
    except Exception as exc:
        # Belt-and-suspenders guard: strathmark_sync functions are already
        # non-blocking, but catch anything that escapes to protect the response.
        import logging as _logging
        _logging.getLogger(__name__).error(
            'STRATHMARK: unexpected error in _push_strathmark_results: %s', exc
        )


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

def _parse_dual_timer(comp_id: int, run_suffix: str, invalid: list) -> tuple:
    """Parse t1_{run_suffix}_{cid} and t2_{run_suffix}_{cid} form fields.

    run_suffix is 'run1' or 'run2'.

    Returns (t1, t2, average) — all None when both fields are absent or empty.
    Returns (t1, None, None) or (None, t2, None) when only one field is present
    (the caller treats this as a 'partial' entry that should not finalize).
    Records (comp_id, raw) into invalid for any non-numeric input.

    Phase 2 of the V2.8.0 scoring fix.  Every timed event in this codebase
    (college and pro, single-run AND dual-run) takes two judge stopwatch
    readings per physical run.  The average becomes the run's "scored time"
    that flows into the existing run1_value / run2_value / result_value
    fields, preserving all downstream scoring code paths.
    """
    raw_t1 = request.form.get(f't1_{run_suffix}_{comp_id}')
    raw_t2 = request.form.get(f't2_{run_suffix}_{comp_id}')

    def _try_parse(raw):
        if raw is None or raw == '':
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            invalid.append((comp_id, raw))
            return None

    t1 = _try_parse(raw_t1)
    t2 = _try_parse(raw_t2)

    if t1 is not None and t2 is not None:
        return (t1, t2, (t1 + t2) / 2.0)
    return (t1, t2, None)


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
    # True for events that use the dual-judge timer entry path.  Hard-Hit
    # primary score (hits) stays single-input per PLAN_REVIEW.md A1
    # scope-reduction; triple-run axe throw is also single-input per throw.
    is_dual_timer_event = (
        event.scoring_type in ('time', 'distance')
        and not event.requires_triple_runs
    )

    try:
        for comp_id in competitor_ids:
            status = request.form.get(f'status_{comp_id}', 'completed')

            # -- parse primary result --
            # Path A: dual-judge timer entry (Phase 2 of V2.8.0).
            # Used for any timed/distance event whose primary metric comes
            # from two judge stopwatches that get averaged.  Covers BOTH
            # single-run events (Pro Underhand, College Single Buck, etc.)
            # AND dual-run events (Speed Climb, Chokerman, Caber Toss).
            #
            # Path B (else branch below): legacy single-input path.
            # Hard-Hit primary score, axe throw triple-run, and any future
            # non-timed event still use one input field.
            if is_dual_timer_event:
                # Determine which run pair to read based on heat run_number.
                run_suffix = 'run2' if (event.requires_dual_runs and heat.run_number == 2) else 'run1'
                t1, t2, average = _parse_dual_timer(comp_id, run_suffix, invalid)

                # Skip rows with no input at all (judge hasn't entered this competitor yet).
                if t1 is None and t2 is None:
                    continue

                # -- get or create result row --
                result = result_by_comp.get(comp_id)
                if not result:
                    comp = comp_lookup.get(comp_id)
                    result = EventResult(
                        event_id=event.id,
                        competitor_id=comp_id,
                        competitor_type=event.event_type,
                        competitor_name=comp.display_name if comp else f'Unknown ({comp_id})',
                    )
                    db.session.add(result)
                    result_by_comp[comp_id] = result

                # Store raw timer readings on the new Phase 1 columns.
                if run_suffix == 'run1':
                    result.t1_run1 = t1
                    result.t2_run1 = t2
                else:
                    result.t1_run2 = t1
                    result.t2_run2 = t2

                # Compute the run's "scored value" (average) and write it
                # into the existing run1_value / run2_value / result_value
                # fields so all downstream scoring code paths see the same
                # plumbing they always have.  When only one timer is present,
                # we leave the run value untouched and force status='partial'
                # so finalization is blocked until the second timer arrives.
                if average is not None:
                    if event.requires_dual_runs:
                        if heat.run_number == 1:
                            result.run1_value = average
                        else:
                            result.run2_value = average
                        result.calculate_best_run(event.scoring_order)
                    else:
                        result.result_value = average
                        # For single-run events, also populate run1_value so
                        # tiebreak_metric() and any other reader that looks
                        # at run1_value sees a consistent value.
                        result.run1_value = average
                else:
                    # Partial entry — only one timer was filled in.  Mark the
                    # row partial so calculate_positions() (which filters on
                    # status == 'completed') excludes it from ranking.
                    if status == 'completed':
                        status = 'partial'

            elif event.requires_triple_runs:
                # Triple-run events (axe throw) — single input per throw.
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
                        competitor_name=comp.display_name if comp else f'Unknown ({comp_id})',
                    )
                    db.session.add(result)
                    result_by_comp[comp_id] = result

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
                # Path B: legacy single-input.  Hard-Hit primary score (hits)
                # is the only event type left here after Phase 2.
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
                        competitor_name=comp.display_name if comp else f'Unknown ({comp_id})',
                    )
                    db.session.add(result)
                    result_by_comp[comp_id] = result

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

            # Capture free-text reason for non-completion statuses.
            # The form always posts reason_{cid}; we only persist it when the
            # status is one of scratched/dnf/dq, otherwise clear it so stale
            # reasons don't linger from an earlier edit.
            raw_reason = (request.form.get(f'reason_{comp_id}') or '').strip()
            if status in ('scratched', 'dnf', 'dq'):
                result.status_reason = raw_reason or None
            else:
                result.status_reason = None

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

        # Auto-finalize when all heats in event are complete (both runs for
        # dual-run events).
        #
        # Phase 4 (V2.8.0): wrap auto-finalize in a savepoint so that if
        # calculate_positions() raises (e.g., a database constraint trips),
        # we roll back to the pre-finalize state but KEEP the heat results
        # the judge just entered.  Without the savepoint, a finalize crash
        # would either lose the heat results (rollback everything) or leave
        # the cache half-updated (commit anyway).  Neither is acceptable.
        all_heats_complete = all(h.status == 'completed' for h in event.heats.all())
        finalize_failed = False
        if all_heats_complete:
            try:
                with db.session.begin_nested():
                    engine.calculate_positions(event)
            except Exception as exc:
                # Roll back the savepoint only — the outer transaction (heat
                # results) is still alive and will commit below.
                import logging as _logging
                _logging.getLogger(__name__).error(
                    'auto-finalize failed for event %s: %s', event.id, exc
                )
                # Make sure the event is in a coherent state: not finalized.
                event.is_finalized = False
                event.status = 'in_progress'
                finalize_failed = True

        log_action('heat_results_saved', 'heat', heat.id,
                   {'event_id': event.id, 'result_updates': changes,
                    'judge_user_id': _current_user_id()})
        db.session.commit()

    except StaleDataError:
        db.session.rollback()
        return {
            'ok': False, 'category': 'warning',
            'message': 'These scores were updated by another judge while you were entering results. '
                       'Please reload to see the latest values before saving again.',
            'redirect_url': url_for('scoring.enter_heat_results',
                                    tournament_id=tournament_id, heat_id=heat.id),
            'status_code': 409,
        }
    except IntegrityError:
        db.session.rollback()
        return {
            'ok': False, 'category': 'error',
            'message': 'A database constraint was violated while saving results. '
                       'Check for duplicate entries and try again.',
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

    # Phase 4 (V2.8.0): if auto-finalize raised, the heat results were saved
    # but the points were not awarded.  Surface this loudly so the judge knows
    # to retry from the event results page.
    if finalize_failed:
        return {
            'ok': True, 'category': 'warning',
            'message': ('Heat saved, but auto-finalization failed. The event '
                        'results page will let you retry — your timer values '
                        'are safe.'),
            'redirect_url': url_for('scoring.event_results',
                                    tournament_id=tournament_id, event_id=event.id),
            'status_code': 200,
            'undo_heat_id': heat.id,
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

    # Partnered Axe Throw: pass PAT state for inline prelim scoring
    pat = None
    pat_stage = None
    pat_pairs = None
    if event.has_prelims:
        from services.partnered_axe import PartneredAxeThrow
        pat = PartneredAxeThrow(event)
        pat_stage = pat.get_stage()
        pat_pairs = pat.get_pairs()

    return render_template('scoring/event_results.html',
                           tournament=tournament, event=event,
                           heats=heats, results=results,
                           payout_templates=payout_templates,
                           throwoff_pending=throwoff_pending,
                           pat=pat, pat_stage=pat_stage, pat_pairs=pat_pairs)


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
    """JSON: provisional standings + outlier warnings + finalization checks for the confirmation modal."""
    event = _event_for_tournament_or_404(tournament_id, event_id)
    preview = engine.preview_positions(event)
    outliers = engine.outlier_check(event)
    throwoffs = [{'id': r.id, 'name': r.competitor_name, 'score': r.result_value}
                 for r in engine.pending_throwoffs(event)]
    finalize_warnings = engine.validate_finalization(event)
    return jsonify({'preview': preview, 'outliers': outliers, 'throwoffs': throwoffs,
                    'finalize_warnings': finalize_warnings})


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/finalize', methods=['POST'])
@write_limit('10 per minute')
def finalize_event(tournament_id, event_id):
    event = _event_for_tournament_or_404(tournament_id, event_id)

    # Pre-finalization validation — surface warnings so the judge knows what's off.
    # These are warnings, not blockers, because there are legitimate reasons to
    # finalize without payouts (test runs) or without marks (scratch events).
    finalize_issues = engine.validate_finalization(event)
    for issue in finalize_issues:
        flash(issue['message'], 'warning')

    try:
        with db.session.begin_nested():   # savepoint — rolls back to pre-finalize if error
            engine.calculate_positions(event)
            log_action('event_finalized', 'event', event.id,
                       {'tournament_id': tournament_id,
                        'judge_user_id': _current_user_id()})
        db.session.commit()
    except StaleDataError:
        db.session.rollback()
        msg = 'Results were modified by another judge during finalization. Reload and finalize again.'
        if _is_async():
            return jsonify({'ok': False, 'message': msg}), 409
        flash(msg, 'warning')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=event_id))
    except IntegrityError:
        db.session.rollback()
        msg = 'A database constraint error occurred during finalization. Contact an admin if this persists.'
        if _is_async():
            return jsonify({'ok': False, 'message': msg}), 409
        flash(msg, 'error')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=event_id))

    invalidate_tournament_caches(tournament_id)

    # STRATHMARK: push results for pro SB/UH and college SB/UH Speed events.
    # Non-blocking — any failure is logged and the response is unaffected.
    _push_strathmark_results(event, tournament_id)

    if _is_async():
        return jsonify({'ok': True, 'message': f'{event.display_name} finalized.'})
    flash(text.FLASH['event_finalized'].format(event_name=event.display_name), 'success')
    return redirect(url_for('scoring.event_results',
                            tournament_id=tournament_id, event_id=event_id))


# ---------------------------------------------------------------------------
# Routes: heat entry
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/heat/<int:heat_id>/enter', methods=['GET', 'POST'])
@write_limit('60 per minute')
def enter_heat_results(tournament_id, heat_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    heat = _heat_for_tournament_or_404(tournament_id, heat_id)
    event = heat.event

    if request.method == 'POST':
        # Reject POST if the heat is locked by a different judge.  This is the server-side
        # enforcement of the advisory lock shown on the GET form — a second tab or another
        # device cannot overwrite results while someone else holds the lock.
        user_id = _current_user_id()
        if heat.is_locked() and heat.locked_by_user_id != (user_id or -1):
            from models.user import User
            locker = User.query.get(heat.locked_by_user_id)
            owner = locker.username if locker else f'User #{heat.locked_by_user_id}'
            msg = f'Heat is currently being edited by {owner}. Your submission was not saved.'
            if _is_async():
                return jsonify({'ok': False, 'category': 'warning', 'message': msg}), 423
            flash(msg, 'warning')
            return redirect(url_for('scoring.enter_heat_results',
                                    tournament_id=tournament_id, heat_id=heat_id))

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
                'name': comp.display_name,
                'stand': heat.get_stand_for_competitor(comp_id),
                'headshot': getattr(comp, 'headshot_filename', None),
                'existing_result': result.result_value if result else None,
                'existing_run1': result.run1_value if result else None,
                'existing_run2': result.run2_value if result else None,
                'existing_run3': result.run3_value if result else None,
                'existing_tiebreak': result.tiebreak_value if result else None,
                # Phase 2 (V2.8.0): raw judge stopwatch readings.  Float-cast
                # because the model column is Numeric and Jinja's "%.2f"|format
                # filter rejects Decimal in some Python/Jinja versions.
                'existing_t1_run1': float(result.t1_run1) if result and result.t1_run1 is not None else None,
                'existing_t2_run1': float(result.t2_run1) if result and result.t2_run1 is not None else None,
                'existing_t1_run2': float(result.t1_run2) if result and result.t1_run2 is not None else None,
                'existing_t2_run2': float(result.t2_run2) if result and result.t2_run2 is not None else None,
                'existing_status': result.status if result else 'completed',
                'existing_status_reason': result.status_reason if result else None,
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

    # Phase 4 (V2.8.0) — strip points before delete (PLAN_REVIEW.md A6/C2/C7).
    #
    # The pre-V2.8.0 path deleted EventResult rows directly without zeroing
    # their points_awarded, which left "phantom" points cached on
    # CollegeCompetitor.individual_points and Team.total_points if the heat
    # had previously been auto-finalized.  After Phase 3 we have the
    # _rebuild_individual_points() helper that recomputes the cache from
    # SUM(points_awarded), so the correct undo sequence is:
    #
    #   1. Capture the set of competitor_ids whose results we're about to delete
    #   2. Delete the EventResult rows (their points contribution disappears)
    #   3. Rebuild individual_points for those competitors from the remaining
    #      EventResult rows (which now don't include the deleted ones)
    #   4. Rebuild team totals for any team that had a touched competitor
    #
    # All of this is wrapped in a savepoint so a partial failure rolls back
    # cleanly to the pre-undo state.  If anything raises, the heat stays
    # 'completed' and the cache is unchanged.
    try:
        with db.session.begin_nested():
            # Step 2: delete the result rows.
            EventResult.query.filter(
                EventResult.event_id == event.id,
                EventResult.competitor_id.in_(competitor_ids),
                EventResult.competitor_type == event.event_type,
            ).delete(synchronize_session='fetch')

            # Step 3 + 4: rebuild caches (college only — pro doesn't have an
            # equivalent SUM cache for total_earnings, those rows just stay
            # at whatever value they were at and the next finalize will
            # rewrite them).
            if event.event_type == 'college':
                from models.competitor import CollegeCompetitor
                from models.team import Team
                from services.scoring_engine import _rebuild_individual_points
                _rebuild_individual_points(competitor_ids)
                touched_comps = (
                    CollegeCompetitor.query
                    .filter(CollegeCompetitor.id.in_(competitor_ids))
                    .all()
                )
                touched_team_ids = {c.team_id for c in touched_comps if c.team_id}
                for team_id in touched_team_ids:
                    team = Team.query.get(team_id)
                    if team:
                        team.recalculate_points()

            heat.status = 'pending'
            event.status = 'in_progress'
            event.is_finalized = False

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        import logging as _logging
        _logging.getLogger(__name__).error('undo_heat_save failed for heat %s: %s', heat_id, exc)
        msg = 'Could not undo heat — please reload and try again.'
        if _is_async():
            return jsonify({'ok': False, 'message': msg}), 500
        flash(msg, 'error')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=event.id))

    invalidate_tournament_caches(tournament_id)
    session.pop(f'undo_heat_{heat_id}', None)
    log_action('heat_undo', 'heat', heat_id,
               {'event_id': event.id, 'judge_user_id': _current_user_id()})

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

    if event.uses_payouts_for_state:
        flash('This event uses a specialized scoring system. Payouts cannot be configured here.', 'warning')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id, event_id=event_id))

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
            # If the event was already finalized, re-run position calculation
            # so that payout_amount on each EventResult gets updated with the
            # new payout structure. Without this, judges have to manually
            # re-finalize after configuring payouts.
            if event.is_finalized:
                with db.session.begin_nested():
                    engine.calculate_positions(event)
                flash('Payouts saved and standings recalculated.', 'success')
            else:
                flash(text.FLASH['payouts_saved'], 'success')
            db.session.commit()
        except (StaleDataError, IntegrityError):
            db.session.rollback()
            flash('Another user changed this event while saving. Please retry.', 'error')
            return redirect(url_for('scoring.configure_payouts',
                                    tournament_id=tournament_id, event_id=event_id))

        invalidate_tournament_caches(tournament_id)
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
# Routes: tournament-level payout manager
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/pro/payout-manager', methods=['GET', 'POST'])
def tournament_payout_manager(tournament_id):
    """Tournament-level payout configuration dashboard for all pro events."""
    tournament = Tournament.query.get_or_404(tournament_id)

    def _payout_redirect():
        """Return redirect to payout manager or setup page depending on return_to."""
        if request.form.get('return_to') == 'setup':
            return redirect(url_for('main.tournament_setup', tournament_id=tournament_id, tab='payouts'))
        return redirect(url_for('scoring.tournament_payout_manager', tournament_id=tournament_id))

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'bulk_apply':
            tpl_id = request.form.get('template_id', type=int)
            event_ids = request.form.getlist('event_ids')
            try:
                event_ids = [int(x) for x in event_ids if x]
            except (TypeError, ValueError):
                event_ids = []
            if not tpl_id:
                flash('Select a template to apply.', 'error')
                return _payout_redirect()
            if not event_ids:
                flash('Select at least one event.', 'error')
                return _payout_redirect()
            template = PayoutTemplate.query.get(tpl_id)
            if not template:
                flash('Template not found.', 'error')
                return _payout_redirect()
            applied = 0
            skipped = 0
            for eid in event_ids:
                ev = Event.query.filter_by(id=eid, tournament_id=tournament_id, event_type='pro').first()
                if ev:
                    if ev.uses_payouts_for_state:
                        skipped += 1
                        continue
                    ev.set_payouts(template.get_payouts())
                    applied += 1
            if applied:
                try:
                    db.session.commit()
                    log_action('bulk_payout_template_applied', 'tournament', tournament_id,
                               {'template_id': tpl_id, 'template_name': template.name, 'event_count': applied})
                    invalidate_tournament_caches(tournament_id)
                    msg = f'"{template.name}" applied to {applied} event(s).'
                    if skipped:
                        msg += f' {skipped} special event(s) skipped.'
                    flash(msg, 'success')
                except (StaleDataError, IntegrityError):
                    db.session.rollback()
                    flash('Save failed — please retry.', 'error')
            else:
                flash('No matching pro events found.', 'error')
            return _payout_redirect()

        if action == 'clear_event':
            eid = request.form.get('event_id', type=int)
            ev = Event.query.filter_by(id=eid, tournament_id=tournament_id, event_type='pro').first()
            if ev:
                if ev.uses_payouts_for_state:
                    flash(f'{ev.display_name} uses a specialized scoring system and cannot be cleared here.', 'warning')
                else:
                    ev.set_payouts({})
                    db.session.commit()
                    invalidate_tournament_caches(tournament_id)
                    flash(f'Payouts cleared for {ev.display_name}.', 'success')
            return _payout_redirect()

        if action == 'delete_template':
            tpl_id = request.form.get('template_id', type=int)
            engine.delete_payout_template(tpl_id)
            flash('Template deleted.', 'success')
            return _payout_redirect()

        if action == 'save_template':
            tpl_name = (request.form.get('template_name') or '').strip()
            if not tpl_name:
                flash('Template name is required.', 'error')
                return _payout_redirect()
            payouts = _parse_payout_form()
            if payouts is None:
                return _payout_redirect()
            engine.save_payout_template(tpl_name, payouts)
            flash(f'Template "{tpl_name}" saved.', 'success')
            return _payout_redirect()

        flash('Unknown action.', 'error')
        return _payout_redirect()

    pro_events = (Event.query
                  .filter_by(tournament_id=tournament_id, event_type='pro')
                  .order_by(Event.name)
                  .all())
    templates = engine.list_payout_templates()

    event_summaries = []
    total_purse = 0.0
    configured_count = 0
    for ev in pro_events:
        # Events that store state-machine data in the payouts column
        # (Pro-Am Relay, Partnered Axe Throw, Birling bracket) cannot
        # be configured via the normal payout form.
        if ev.uses_payouts_for_state:
            event_summaries.append({
                'event': ev,
                'payouts': {},
                'purse': 0.0,
                'places_paid': 0,
                'first_place': 0.0,
                'state_event': True,
            })
            continue

        payouts = ev.get_payouts()
        purse = sum(float(v) for v in payouts.values()) if payouts else 0.0
        places_paid = len([v for v in payouts.values() if float(v) > 0]) if payouts else 0
        first_place = float(payouts.get('1', 0)) if payouts else 0.0
        total_purse += purse
        if purse > 0:
            configured_count += 1
        event_summaries.append({
            'event': ev,
            'payouts': payouts,
            'purse': purse,
            'places_paid': places_paid,
            'first_place': first_place,
        })

    return render_template(
        'scoring/tournament_payouts.html',
        tournament=tournament,
        event_summaries=event_summaries,
        templates=templates,
        total_purse=total_purse,
        configured_count=configured_count,
        total_events=len(pro_events),
    )


# ---------------------------------------------------------------------------
# Routes: birling bracket
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/heat/<int:heat_id>/pdf')
def heat_sheet_pdf(tournament_id, heat_id):
    """
    Printable heat sheet for a single heat.

    Attempts to render a PDF via WeasyPrint if installed; falls back to a
    print-optimised HTML page that the browser can save as PDF.
    """
    tournament = Tournament.query.get_or_404(tournament_id)
    heat = _heat_for_tournament_or_404(tournament_id, heat_id)
    event = heat.event

    competitor_ids = heat.get_competitors()
    comp_lookup = _competitor_lookup(event, competitor_ids)
    result_lookup = _existing_results(event, competitor_ids)
    assignments = heat.get_stand_assignments()

    competitors = []
    for cid in competitor_ids:
        comp = comp_lookup.get(cid)
        result = result_lookup.get(cid)
        competitors.append({
            'id': cid,
            'name': comp.display_name if comp else f'Unknown ({cid})',
            'stand': assignments.get(str(cid)),
            'result_value': result.result_value if result else None,
            'run1_value': result.run1_value if result else None,
            'run2_value': result.run2_value if result else None,
            'status': result.status if result else 'pending',
        })

    html = render_template(
        'scoring/heat_sheet_print.html',
        tournament=tournament, event=event, heat=heat,
        competitors=competitors,
    )

    # Try WeasyPrint; if not installed, return print-styled HTML
    try:
        from weasyprint import HTML as WP_HTML  # type: ignore
        pdf_bytes = WP_HTML(string=html).write_pdf()
        return pdf_bytes, 200, {
            'Content-Type': 'application/pdf',
            'Content-Disposition': f'inline; filename="heat_{heat.heat_number}_run{heat.run_number}.pdf"',
        }
    except ImportError:
        return html, 200, {'Content-Type': 'text/html'}


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/birling-bracket')
def birling_bracket(tournament_id, event_id):
    """Legacy route — redirects to the full birling management page."""
    return redirect(url_for('scheduling.birling_manage',
                            tournament_id=tournament_id, event_id=event_id))


# ---------------------------------------------------------------------------
# Routes: blank judge sheets (printable recording forms)
# ---------------------------------------------------------------------------
# These produce OUTPUT-ONLY documents — no data is written back to the DB.
# A judge sheet is a blank form the judges fill in by hand during the event.
# It mirrors the heat sheet PDF flow: WeasyPrint if installed, HTML fallback.

def _safe_filename_part(name: str) -> str:
    """Strip characters that break Content-Disposition filenames."""
    return ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in name)


def _render_judge_sheet_html(sheets: list, tournament: Tournament) -> str:
    """Render the template with a uniform sheets list (one or many events)."""
    return render_template(
        'scoring/judge_sheet.html',
        sheets=sheets,
        year=tournament.year if tournament else '',
        event_date=None,
    )


def _judge_sheet_response(html: str, filename: str):
    """Return a WeasyPrint PDF if available; otherwise return print HTML."""
    try:
        from weasyprint import HTML as WP_HTML  # type: ignore
        pdf_bytes = WP_HTML(string=html).write_pdf()
        return pdf_bytes, 200, {
            'Content-Type': 'application/pdf',
            'Content-Disposition': f'attachment; filename="{filename}.pdf"',
        }
    except ImportError:
        return html, 200, {'Content-Type': 'text/html'}


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/judge-sheet')
def judge_sheet_for_event(tournament_id: int, event_id: int):
    """Blank judge sheet PDF (or HTML fallback) for a single event."""
    from services.judge_sheet import get_event_heats_for_judging

    tournament = Tournament.query.get_or_404(tournament_id)
    event = _event_for_tournament_or_404(tournament_id, event_id)
    sheet = get_event_heats_for_judging(event.id)
    if sheet is None:
        abort(404)

    html = _render_judge_sheet_html([sheet], tournament)
    filename = f'judge_sheet_{_safe_filename_part(event.display_name)}'
    return _judge_sheet_response(html, filename)


@scoring_bp.route('/<int:tournament_id>/judge-sheets/all')
def judge_sheets_all(tournament_id: int):
    """Concatenated judge sheet document for every event in the tournament
    that has heats assigned.  Events without heats are skipped silently so
    that the 'print everything before the day starts' button is one-click.
    """
    from services.judge_sheet import get_event_heats_for_judging

    tournament = Tournament.query.get_or_404(tournament_id)
    events = (
        Event.query
        .filter_by(tournament_id=tournament.id)
        .order_by(Event.event_type.asc(), Event.id.asc())
        .all()
    )
    sheets = []
    for event in events:
        sheet = get_event_heats_for_judging(event.id)
        if sheet and sheet['heats']:
            sheets.append(sheet)

    if not sheets:
        flash('No events with heats are available for judge sheets yet.', 'warning')
        return redirect(url_for('main.tournament_detail', tournament_id=tournament.id))

    html = _render_judge_sheet_html(sheets, tournament)
    filename = f'judge_sheets_tournament_{tournament.id}'
    return _judge_sheet_response(html, filename)


# ---------------------------------------------------------------------------
# Routes: CSRF-exempt offline replay endpoint
# ---------------------------------------------------------------------------
# This endpoint replays queued offline scores that carry expired CSRF tokens.
# Validated via a one-time replay_token generated client-side at queue time and
# stored in IndexedDB. Requires a valid Flask-Login session — not fully public.
#
# REPLAY FLOW:
#   sw.js queues POST + replay_token → offline → reconnect →
#   replayQueue() tries original endpoint → 400 CSRF expired →
#   retries via /api/scoring/replay with replay_token → 2xx success
#
# Security: no CSRF, but requires login + replay_token + valid heat/tournament.

@scoring_bp.route('/api/replay-token', methods=['GET'])
def issue_replay_token():
    """Issue a fresh HMAC-bound replay token for offline score queueing.

    The token is valid for 7 days and tied to the requesting user. The offline
    queue stashes it in IndexedDB at queue time and submits it via the
    replay_offline_score endpoint when connectivity returns.
    """
    if not current_user.is_authenticated:
        return jsonify({'ok': False, 'message': 'Login required.'}), 401
    import hashlib
    import hmac as _hmac
    import time as _time
    secret = current_app.config.get('SECRET_KEY', '')
    if not secret:
        return jsonify({'ok': False, 'message': 'Server not configured.'}), 500
    ts = int(_time.time())
    msg = f'{current_user.id}:{ts}'.encode('utf-8')
    sig = _hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()[:32]
    return jsonify({'ok': True, 'replay_token': f'{ts}.{sig}'})


@scoring_bp.route('/api/replay', methods=['POST'])
def replay_offline_score():
    """Accept an offline-queued score submission without CSRF validation."""
    from app import csrf
    # CSRF exemption is applied via decorator-free approach below

    if not current_user.is_authenticated:
        return jsonify({'ok': False, 'message': 'Login required.'}), 401

    replay_token = request.form.get('replay_token', '').strip()
    # SECURITY (CSO #6): replay_token must be a real HMAC bound to (user_id, ts).
    # Length-only validation previously accepted any 16+ char string, leaving
    # the CSRF-exempt endpoint open to cross-site POST attacks against logged-in
    # scorers. Token format: "{ts}.{hex_sig}" where sig is HMAC-SHA256 truncated
    # to 32 hex chars over f"{user_id}:{ts}" using SECRET_KEY.
    if not replay_token or '.' not in replay_token:
        return jsonify({'ok': False, 'message': 'Missing or malformed replay token.'}), 403
    import hashlib
    import hmac as _hmac
    import time as _time
    try:
        ts_str, sig = replay_token.split('.', 1)
        ts = int(ts_str)
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'message': 'Malformed replay token.'}), 403
    # Reject tokens older than 7 days (race weekend buffer + offline replay window)
    if _time.time() - ts > 7 * 24 * 60 * 60:
        return jsonify({'ok': False, 'message': 'Replay token expired.'}), 403
    secret = current_app.config.get('SECRET_KEY', '')
    expected_msg = f'{current_user.id}:{ts}'.encode('utf-8')
    expected_sig = _hmac.new(secret.encode('utf-8'), expected_msg, hashlib.sha256).hexdigest()[:32]
    if not _hmac.compare_digest(sig, expected_sig):
        return jsonify({'ok': False, 'message': 'Invalid replay token.'}), 403

    # Extract tournament_id and heat_id from the original URL stored in the body
    # The form body contains the same fields as the regular score entry form
    tournament_id = request.form.get('tournament_id', type=int)
    heat_id = request.form.get('heat_id', type=int)
    if not tournament_id or not heat_id:
        return jsonify({'ok': False, 'message': 'Missing tournament or heat ID.'}), 400

    tournament = Tournament.query.get(tournament_id)
    if not tournament:
        return jsonify({'ok': False, 'message': 'Tournament not found.'}), 404

    heat = Heat.query.get(heat_id)
    if not heat or not heat.event or heat.event.tournament_id != tournament_id:
        return jsonify({'ok': False, 'message': 'Heat not found.'}), 404

    event = heat.event

    outcome = _save_heat_results_submission(
        tournament_id=tournament_id, heat=heat, event=event
    )
    return jsonify({
        k: outcome[k] for k in ('ok', 'message', 'category') if k in outcome
    }), outcome['status_code']


# ---------------------------------------------------------------------------
# Routes: admin scoring repair (Phase 4 V2.8.0)
# ---------------------------------------------------------------------------

@scoring_bp.route('/admin/repair-points/<int:tournament_id>', methods=['POST'])
def repair_points(tournament_id):
    """
    Admin tool to rebuild every individual_points and team total_points cache
    for a tournament from the EventResult table.

    Use case: a previous deploy or hand-edit left the cache out of sync with
    the source-of-truth EventResult.points_awarded column.  This route walks
    every CollegeCompetitor and Team in the tournament and rebuilds their
    cached totals from SUM(EventResult.points_awarded), with NO recalculation
    of positions — it trusts what's already in the table.

    Admin role required (not just judge).  Returns JSON.  Logged via audit.
    """
    # Tighter than the standard judge gate — repair is admin-only.
    if not (current_user and current_user.is_authenticated
            and getattr(current_user, 'role', None) == 'admin'):
        return jsonify({'ok': False, 'message': 'Admin role required.'}), 403

    tournament = Tournament.query.get_or_404(tournament_id)

    from models.competitor import CollegeCompetitor
    from models.team import Team
    from services.scoring_engine import _rebuild_individual_points

    competitors = CollegeCompetitor.query.filter_by(tournament_id=tournament_id).all()
    competitor_ids = [c.id for c in competitors]

    teams = Team.query.filter_by(tournament_id=tournament_id).all()

    try:
        with db.session.begin_nested():
            # Rebuild every competitor's individual_points from SUM.
            _rebuild_individual_points(competitor_ids)
            # Then rebuild every team's total_points from its members.
            for team in teams:
                team.recalculate_points()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        import logging as _logging
        _logging.getLogger(__name__).error('repair_points failed for tournament %s: %s',
                                           tournament_id, exc)
        return jsonify({'ok': False, 'message': f'Repair failed: {exc}'}), 500

    invalidate_tournament_caches(tournament_id)
    log_action('points_cache_rebuilt', 'tournament', tournament_id, {
        'competitors_rebuilt': len(competitor_ids),
        'teams_rebuilt': len(teams),
        'judge_user_id': _current_user_id(),
    })

    return jsonify({
        'ok': True,
        'tournament_id': tournament_id,
        'tournament_name': tournament.name,
        'competitors_rebuilt': len(competitor_ids),
        'teams_rebuilt': len(teams),
        'message': (f'Rebuilt {len(competitor_ids)} competitor(s) and '
                    f'{len(teams)} team(s) for {tournament.name}.'),
    })
