"""
Handicap mark assignment route.

Provides a judge-facing page to trigger STRATHMARK mark assignment for any
handicap-format event.  Accessible at:

    GET  /scheduling/<tid>/events/<eid>/assign-marks   — status page
    POST /scheduling/<tid>/events/<eid>/assign-marks   — run assignment
"""
from flask import flash, redirect, render_template, request, url_for

from database import db
from models import Event, Tournament
from services.audit import log_action
from services.mark_assignment import assign_handicap_marks, is_mark_assignment_eligible
from services.strathmark_sync import is_configured as strathmark_is_configured

from . import scheduling_bp


@scheduling_bp.route('/<int:tournament_id>/events/<int:event_id>/assign-marks',
                     methods=['GET', 'POST'])
def assign_marks(tournament_id: int, event_id: int):
    """Trigger or display STRATHMARK handicap mark assignment for an event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.filter_by(id=event_id, tournament_id=tournament_id).first_or_404()

    eligible = is_mark_assignment_eligible(event)
    configured = strathmark_is_configured()

    if request.method == 'POST':
        if not eligible:
            flash('This event is not eligible for handicap mark assignment.', 'warning')
            return redirect(url_for('scheduling.assign_marks',
                                    tournament_id=tournament_id, event_id=event_id))

        result = assign_handicap_marks(event)

        if result['status'] == 'unconfigured':
            flash('STRATHMARK is not configured — set STRATHMARK_SUPABASE_URL and '
                  'STRATHMARK_SUPABASE_KEY environment variables.', 'error')
        elif result['status'] == 'not_eligible':
            flash('Event is not eligible for mark assignment.', 'warning')
        elif result['status'] in ('ok', 'partial'):
            try:
                db.session.commit()
            except Exception as exc:
                db.session.rollback()
                flash(f'Database error saving marks: {exc}', 'error')
                return redirect(url_for('scheduling.assign_marks',
                                        tournament_id=tournament_id, event_id=event_id))

            log_action('marks_assigned', 'event', event_id, {
                'assigned': result['assigned'],
                'skipped': result['skipped'],
                'status': result['status'],
            })

            if result['errors']:
                for err in result['errors']:
                    flash(f'Mark assignment warning: {err}', 'warning')

            if result['assigned'] > 0:
                flash(
                    f"Assigned start marks for {result['assigned']} competitor(s); "
                    f"{result['skipped']} skipped (no STRATHMARK profile).",
                    'success',
                )
            else:
                flash(
                    'No marks assigned — competitors may not have STRATHMARK profiles yet.',
                    'warning',
                )
        else:
            for err in result.get('errors', []):
                flash(f'Error: {err}', 'error')
            flash('Mark assignment did not complete successfully.', 'error')

        return redirect(url_for('scheduling.assign_marks',
                                tournament_id=tournament_id, event_id=event_id))

    # GET — load current state
    from models.event import EventResult
    results = (
        EventResult.query
        .filter_by(event_id=event_id)
        .filter(EventResult.status.in_(['pending', 'completed']))
        .order_by(EventResult.competitor_name)
        .all()
    )

    # Annotate each result with whether it has a real mark (not placeholder)
    mark_rows = []
    for r in results:
        hf = r.handicap_factor
        has_mark = hf is not None and hf != 1.0
        mark_rows.append({
            'result': r,
            'has_mark': has_mark,
            'mark_display': f'{hf:.1f}s' if has_mark else 'None (scratch)',
        })

    return render_template(
        'scheduling/assign_marks.html',
        tournament=tournament,
        event=event,
        eligible=eligible,
        configured=configured,
        mark_rows=mark_rows,
    )
