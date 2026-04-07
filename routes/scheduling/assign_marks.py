"""
Handicap mark assignment route.

Provides a judge-facing page to assign STRATHMARK handicap start marks for any
handicap-format event.  Three input paths are supported, dispatched via the
``action`` form field on POST:

    action=strathmark    Run the live STRATHMARK HandicapCalculator (requires
                         the strathmark package + a reachable Ollama endpoint).
    action=manual_save   Read per-row mark inputs from the table and write them
                         directly to EventResult.handicap_factor.  No external
                         dependencies — works offline / on Railway where Ollama
                         is unreachable.
    action=csv_import    Parse a pasted CSV/TSV block of "name,mark_seconds"
                         lines and apply by competitor name match.  Same offline
                         guarantee as manual_save.

The two manual paths exist as race-day fallbacks: judges can pre-compute marks
on a laptop where Ollama runs and either type or paste them in.

URL:
    GET  /scheduling/<tid>/events/<eid>/assign-marks   — status page
    POST /scheduling/<tid>/events/<eid>/assign-marks   — assign (action-dispatched):

        action=strathmark    Run the live STRATHMARK HandicapCalculator (legacy
                             default; requires the strathmark package + a
                             reachable Supabase backend).
        action=manual_save   Read per-row mark inputs from the inline edit
                             table and write them directly to
                             EventResult.handicap_factor.  No external
                             dependencies — works offline / on Railway where
                             Ollama is unreachable.
        action=upload_csv    Parse a pre-computed marks CSV file upload and
                             render a preview table for judge review.
        action=confirm_csv   Write the previewed CSV marks to EventResult.

The two manual paths (manual_save + CSV) exist as race-day fallbacks: judges
can pre-compute marks on a laptop where Ollama runs and either type them in
or paste them via CSV upload, then save to a deployed Pro-Am Manager that
has no Ollama access.
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

# Sentinel: handicap_factor == 1.0 is the DB placeholder treated as scratch
# (0.0 start mark) by scoring_engine._metric().  Anything else is a real mark.
_SCRATCH_PLACEHOLDER = 1.0


def _parse_mark(raw: str) -> float | None:
    """Parse a single mark string to a float >= 0.

    Returns None if the input is blank/unparseable.  Negative values are
    clamped to 0.0 (scratch).  Values are interpreted as seconds.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Tolerate trailing 's' (e.g. "3.5s")
    if s.lower().endswith('s'):
        s = s[:-1].strip()
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v < 0:
        v = 0.0
    return v


def _load_event_results(event_id: int):
    from models.event import EventResult
    return (
        EventResult.query
        .filter_by(event_id=event_id)
        .filter(EventResult.status.in_(['pending', 'completed']))
        .all()
    )


def _commit_or_flash(success_msg: str, audit_action: str, event_id: int, details: dict) -> bool:
    """Commit the current session, flashing errors and logging on success.

    Returns True on success, False on rollback.
    """
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        flash(f'Database error saving marks: {exc}', 'error')
        return False
    log_action(audit_action, 'event', event_id, details)
    flash(success_msg, 'success')
    return True


def _handle_strathmark_run(event) -> None:
    """Trigger live STRATHMARK HandicapCalculator and surface every status."""
    result = assign_handicap_marks(event)

    if result['status'] == 'unconfigured':
        flash('STRATHMARK is not configured — set STRATHMARK_SUPABASE_URL and '
              'STRATHMARK_SUPABASE_KEY environment variables.', 'error')
        return
    if result['status'] == 'not_eligible':
        flash('Event is not eligible for mark assignment.', 'warning')
        return
    if result['status'] == 'no_wood_config':
        # Bug 3 fix surface — refused to silently guess Pine 300mm
        flash(
            'No WoodConfig is set for this event — configure the wood species '
            'and block diameter on the tournament Wood Specs page before '
            'assigning marks.  STRATHMARK predictions depend on species '
            'hardness and diameter, so we will not guess.',
            'warning',
        )
        return
    if result['status'] in ('ok', 'partial'):
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            flash(f'Database error saving marks: {exc}', 'error')
            return
        log_action('marks_assigned', 'event', event.id, {
            'assigned': result['assigned'],
            'skipped': result['skipped'],
            'status': result['status'],
            'source': 'strathmark',
        })
        for err in result.get('errors', []):
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
        return

    for err in result.get('errors', []):
        flash(f'Error: {err}', 'error')
    flash('Mark assignment did not complete successfully.', 'error')


def _handle_manual_save(event) -> None:
    """Write per-row mark inputs from the assign_marks table.

    Form field convention: ``mark_<result_id>`` (blank or 'scratch' = clear to
    placeholder; otherwise float seconds).
    """
    results = _load_event_results(event.id)
    by_id = {r.id: r for r in results}

    assigned = 0
    cleared = 0
    skipped = 0
    bad: list[str] = []

    for key, raw in request.form.items():
        if not key.startswith('mark_'):
            continue
        try:
            rid = int(key[len('mark_'):])
        except (TypeError, ValueError):
            continue
        result = by_id.get(rid)
        if result is None:
            continue

        s = (raw or '').strip()
        if not s:
            # Blank input = leave as-is.  Use the explicit "scratch" sentinel
            # if a judge wants to wipe a mark.
            skipped += 1
            continue
        if s.lower() in ('scratch', 'clear', 'none', '-'):
            result.handicap_factor = _SCRATCH_PLACEHOLDER
            result.predicted_time = None
            cleared += 1
            continue

        mark = _parse_mark(s)
        if mark is None:
            bad.append(f'{result.competitor_name}: "{raw}"')
            continue

        result.handicap_factor = mark
        # Manual marks have no STRATHMARK prediction backing them.
        result.predicted_time = None
        assigned += 1

    if not assigned and not cleared:
        if bad:
            for b in bad:
                flash(f'Could not parse mark — {b}', 'warning')
            flash('No marks were saved.', 'warning')
        else:
            flash('No mark changes to save.', 'warning')
        db.session.rollback()
        return

    msg_parts = []
    if assigned:
        msg_parts.append(f'{assigned} mark(s) saved')
    if cleared:
        msg_parts.append(f'{cleared} cleared to scratch')
    msg = '; '.join(msg_parts) + '.'

    if _commit_or_flash(msg, 'marks_assigned', event.id, {
        'assigned': assigned,
        'cleared': cleared,
        'source': 'manual',
    }):
        for b in bad:
            flash(f'Could not parse mark — {b} (skipped)', 'warning')


def _handle_csv_import(event) -> None:
    """Parse pasted CSV/TSV "name,mark" lines and apply by name match.

    Accepts comma, tab, or whitespace as the delimiter.  Header row optional —
    detected and skipped if the first row's second field is non-numeric.
    Match is case-insensitive on competitor_name; first exact match wins.
    Unmatched names are reported but do not block the rest of the import.
    """
    blob = (request.form.get('csv_blob') or '').strip()
    if not blob:
        flash('CSV input was empty.', 'warning')
        return

    results = _load_event_results(event.id)
    by_name = {(r.competitor_name or '').strip().lower(): r for r in results}

    assigned = 0
    unmatched: list[str] = []
    bad: list[str] = []

    lines = [ln for ln in blob.splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        # Split on tab first, then comma, then whitespace
        if '\t' in line:
            parts = [p.strip() for p in line.split('\t')]
        elif ',' in line:
            parts = [p.strip() for p in line.split(',')]
        else:
            parts = line.rsplit(None, 1)

        if len(parts) < 2:
            bad.append(f'line {idx + 1}: "{line}"')
            continue

        name_raw, mark_raw = parts[0], parts[-1]
        # Header detection on the first line: skip if mark column isn't numeric
        if idx == 0 and _parse_mark(mark_raw) is None:
            continue

        mark = _parse_mark(mark_raw)
        if mark is None:
            bad.append(f'line {idx + 1}: "{line}"')
            continue

        result = by_name.get(name_raw.strip().lower())
        if result is None:
            unmatched.append(name_raw)
            continue

        result.handicap_factor = mark
        result.predicted_time = None
        assigned += 1

    if not assigned:
        db.session.rollback()
        flash('No marks imported — no rows matched a competitor in this event.', 'warning')
        for u in unmatched[:10]:
            flash(f'Unmatched name: {u}', 'warning')
        for b in bad[:10]:
            flash(f'Unparseable {b}', 'warning')
        return

    if _commit_or_flash(
        f'Imported {assigned} mark(s) from CSV.',
        'marks_assigned',
        event.id,
        {'assigned': assigned, 'unmatched': len(unmatched), 'source': 'csv'},
    ):
        for u in unmatched[:10]:
            flash(f'Unmatched name (skipped): {u}', 'warning')
        if len(unmatched) > 10:
            flash(f'... and {len(unmatched) - 10} more unmatched names', 'warning')
        for b in bad[:10]:
            flash(f'Unparseable {b}', 'warning')


@scheduling_bp.route('/<int:tournament_id>/events/<int:event_id>/assign-marks',
                     methods=['GET', 'POST'])
def assign_marks(tournament_id: int, event_id: int):
    """Display or assign handicap start marks for an event.

    POST dispatches on the ``action`` form field:
      strathmark    — run live STRATHMARK HandicapCalculator
      manual_save   — write per-row mark inputs from the table
      csv_import    — parse pasted "name,mark" CSV/TSV block
    """
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

        # ----- Other actions: dispatch to per-action helpers ------------
        if not eligible:
            flash('This event is not eligible for handicap mark assignment.', 'warning')
            return redirect(url_for('scheduling.assign_marks',
                                    tournament_id=tournament_id, event_id=event_id))

        if action == 'manual_save':
            _handle_manual_save(event)
        elif action == 'csv_import':
            # Blob-paste path: judge pastes "name,mark" lines into a textarea
            # and we apply by name match.  See _handle_csv_import for the
            # parser rules.  This is distinct from upload_csv/confirm_csv
            # (file upload + preview workflow above).
            _handle_csv_import(event)
        else:
            # Default and explicit `action=strathmark` -> live calculator
            _handle_strathmark_run(event)

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
