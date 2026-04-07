"""
Handicap mark assignment route.

Provides a judge-facing page to trigger STRATHMARK mark assignment for any
handicap-format event.  Accessible at:

    GET  /scheduling/<tid>/events/<eid>/assign-marks   — status page
    POST /scheduling/<tid>/events/<eid>/assign-marks   — three actions:
        action=assign        run live STRATHMARK calculator (default for legacy buttons)
        action=upload_csv    parse pre-computed marks CSV → render preview table
        action=confirm_csv   write the previewed marks to EventResult.handicap_factor

The CSV path exists so judges can pre-compute marks locally with the full
STRATHMARK cascade (including Ollama + Gemini) and upload them to a Railway
deployment that has no Ollama access.
"""
from flask import flash, redirect, render_template, request, url_for

from database import db
from models import Event, Tournament
from services.audit import log_action
from services.mark_assignment import (
    assign_handicap_marks,
    is_mark_assignment_eligible,
    parse_marks_csv,
)
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

    # Pre-load active result rows up front — both the GET render and the POST
    # CSV path need them.
    from models.event import EventResult
    results = (
        EventResult.query
        .filter_by(event_id=event_id)
        .filter(EventResult.status.in_(['pending', 'completed']))
        .order_by(EventResult.competitor_name)
        .all()
    )

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()

        # ----- CSV upload: parse + render preview -----------------------
        if action == 'upload_csv':
            if not eligible:
                flash('This event is not eligible for handicap mark assignment.', 'warning')
                return redirect(url_for('scheduling.assign_marks',
                                        tournament_id=tournament_id, event_id=event_id))

            csv_file = request.files.get('marks_csv')
            if csv_file is None or not getattr(csv_file, 'filename', ''):
                flash('Please choose a CSV file to upload.', 'warning')
                return redirect(url_for('scheduling.assign_marks',
                                        tournament_id=tournament_id, event_id=event_id))

            preview_rows, parse_errors = parse_marks_csv(csv_file, results)
            for err in parse_errors:
                flash(f'CSV: {err}', 'error')
            if parse_errors:
                return redirect(url_for('scheduling.assign_marks',
                                        tournament_id=tournament_id, event_id=event_id))

            mark_rows = _build_mark_rows(results)
            return render_template(
                'scheduling/assign_marks.html',
                tournament=tournament,
                event=event,
                eligible=eligible,
                configured=configured,
                mark_rows=mark_rows,
                csv_preview_rows=preview_rows,
            )

        # ----- CSV confirm: write the previewed marks -------------------
        if action == 'confirm_csv':
            if not eligible:
                flash('This event is not eligible for handicap mark assignment.', 'warning')
                return redirect(url_for('scheduling.assign_marks',
                                        tournament_id=tournament_id, event_id=event_id))

            results_by_id = {r.id: r for r in results}
            written = 0
            skipped = 0
            for r in results:
                key = f'mark_{r.id}'
                raw = (request.form.get(key) or '').strip()
                if not raw:
                    skipped += 1
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    flash(
                        f'Invalid mark for {r.competitor_name}: {raw!r} — skipped.',
                        'warning',
                    )
                    skipped += 1
                    continue
                if val < 0:
                    flash(
                        f'Negative mark for {r.competitor_name} ({val:.2f}s) — clamped to 0.',
                        'warning',
                    )
                    val = 0.0
                r.handicap_factor = val
                # Predicted-time is unknown for CSV-imported marks; clear it so
                # the residual logger doesn't compare against a stale value.
                r.predicted_time = None
                written += 1

            try:
                db.session.commit()
            except Exception as exc:
                db.session.rollback()
                flash(f'Database error saving CSV marks: {exc}', 'error')
                return redirect(url_for('scheduling.assign_marks',
                                        tournament_id=tournament_id, event_id=event_id))

            log_action('marks_assigned', 'event', event_id, {
                'source': 'csv_upload',
                'written': written,
                'skipped': skipped,
            })

            if written > 0:
                flash(
                    f"Imported {written} mark(s) from CSV; {skipped} row(s) skipped.",
                    'success',
                )
            else:
                flash('No marks were imported from CSV.', 'warning')

            return redirect(url_for('scheduling.assign_marks',
                                    tournament_id=tournament_id, event_id=event_id))

        # ----- Default: live STRATHMARK calculator path -----------------
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
        elif result['status'] == 'no_wood_config':
            flash(
                'No WoodConfig is set for this event — configure the wood species '
                'and block diameter on the tournament Wood Specs page before '
                'assigning marks.  STRATHMARK predictions depend on species '
                'hardness and diameter, so we will not guess.',
                'warning',
            )
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

    # GET — render the status page using the pre-loaded results.
    mark_rows = _build_mark_rows(results)

    return render_template(
        'scheduling/assign_marks.html',
        tournament=tournament,
        event=event,
        eligible=eligible,
        configured=configured,
        mark_rows=mark_rows,
    )


def _build_mark_rows(results):
    """Annotate each EventResult with display state for the marks table.

    Extracted so the GET path and the CSV-preview POST path render the
    "Current Start Marks" table from the same data shape.
    """
    mark_rows = []
    for r in results:
        hf = r.handicap_factor
        has_mark = hf is not None and hf != 1.0
        mark_rows.append({
            'result': r,
            'has_mark': has_mark,
            'mark_display': f'{hf:.1f}s' if has_mark else 'None (scratch)',
        })
    return mark_rows
