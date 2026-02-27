"""
Pro entry importer routes.

Upload -> Review -> Confirm flow for importing Google Forms xlsx exports
into the ProCompetitor table.
"""
import json
import os
import uuid
from datetime import datetime

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)

from database import db
from models import Event, EventResult, ProCompetitor, Tournament
from services.audit import log_action
from services.upload_security import malware_scan, save_upload, validate_excel_upload

import_pro_bp = Blueprint('import_pro', __name__)

_ALLOWED = {'xlsx', 'xls'}

# Fee lookup: canonical event name -> amount (used when writing entry_fees JSON)
_EVENT_FEES = {
    'Springboard (L)':          10,
    'Springboard (R)':          10,
    '1-Board Springboard':      10,
    "Men's Underhand":          10,
    "Women's Underhand":        10,
    "Women's Standing Block":   10,
    "Men's Single Buck":         5,
    "Women's Single Buck":       5,
    "Men's Double Buck":         5,
    'Jack & Jill':               5,
    'Hot Saw':                   5,
    'Obstacle Pole':             5,
    'Speed Climb':               5,
    'Cookie Stack':              5,
    'Partnered Axe Throw':       5,
}


def _allowed(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in _ALLOWED


def _session_key(tournament_id: int) -> str:
    return f'pro_import_{tournament_id}'


def _temp_path(filename: str) -> str:
    return os.path.join(current_app.config['UPLOAD_FOLDER'], filename)


# ---------------------------------------------------------------------------
# GET /import/<id>/pro-entries  — show upload form
# POST /import/<id>/pro-entries — process uploaded file, redirect to review
# ---------------------------------------------------------------------------
@import_pro_bp.route('/<int:tournament_id>/pro-entries', methods=['GET', 'POST'])
def upload_pro_entries(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    if request.method == 'GET':
        return render_template('pro/import_upload.html', tournament=tournament)

    # --- POST: receive file ---
    if 'file' not in request.files:
        flash('No file selected.', 'error')
        return redirect(url_for('import_pro.upload_pro_entries', tournament_id=tournament_id))

    f = request.files['file']
    if f.filename == '':
        flash('No file selected.', 'error')
        return redirect(url_for('import_pro.upload_pro_entries', tournament_id=tournament_id))

    if not _allowed(f.filename):
        flash('File must be an .xlsx or .xls spreadsheet.', 'error')
        return redirect(url_for('import_pro.upload_pro_entries', tournament_id=tournament_id))

    validation = validate_excel_upload(f, _ALLOWED)
    if not validation.ok:
        flash(validation.error, 'error')
        return redirect(url_for('import_pro.upload_pro_entries', tournament_id=tournament_id))

    upload_path = save_upload(f, current_app.config['UPLOAD_FOLDER'], validation.safe_name)

    # Parse
    try:
        malware_scan(
            upload_path,
            enabled=bool(current_app.config.get('ENABLE_UPLOAD_MALWARE_SCAN', False)),
            command_template=current_app.config.get('MALWARE_SCAN_COMMAND', '')
        )
        from services.pro_entry_importer import compute_review_flags, parse_pro_entries
        entries = parse_pro_entries(upload_path)
    except Exception as exc:
        flash(f'Could not parse file: {exc}', 'error')
        return redirect(url_for('import_pro.upload_pro_entries', tournament_id=tournament_id))

    if not entries:
        flash('No competitor entries found in the file.', 'warning')
        return redirect(url_for('import_pro.upload_pro_entries', tournament_id=tournament_id))

    # Compute review flags
    compute_review_flags(entries)

    # Persist parsed data to a temp JSON file (avoids cookie-size limits)
    temp_name = f'pro_import_{tournament_id}_{uuid.uuid4().hex}.json'
    with open(_temp_path(temp_name), 'w', encoding='utf-8') as fh:
        json.dump(entries, fh, ensure_ascii=False)

    # Store only the filename in the session
    session[_session_key(tournament_id)] = temp_name
    log_action('pro_upload_parsed', 'tournament', tournament_id, {'entries': len(entries), 'filename': validation.safe_name})
    db.session.commit()

    return redirect(url_for('import_pro.review_pro_entries', tournament_id=tournament_id))


# ---------------------------------------------------------------------------
# GET /import/<id>/pro-entries/review — show review table
# ---------------------------------------------------------------------------
@import_pro_bp.route('/<int:tournament_id>/pro-entries/review')
def review_pro_entries(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    temp_name = session.get(_session_key(tournament_id))
    if not temp_name:
        flash('No import data found. Please upload the file again.', 'error')
        return redirect(url_for('import_pro.upload_pro_entries', tournament_id=tournament_id))

    try:
        with open(_temp_path(temp_name), encoding='utf-8') as fh:
            entries = json.load(fh)
    except (OSError, json.JSONDecodeError):
        flash('Import data is missing or corrupt. Please upload the file again.', 'error')
        session.pop(_session_key(tournament_id), None)
        return redirect(url_for('import_pro.upload_pro_entries', tournament_id=tournament_id))

    total_competitors = len(entries)
    total_fees        = sum(e['total_fees'] for e in entries)
    has_warnings      = any(e.get('flags') for e in entries)

    return render_template(
        'pro/import_review.html',
        tournament=tournament,
        entries=entries,
        total_competitors=total_competitors,
        total_fees=total_fees,
        has_warnings=has_warnings,
    )


# ---------------------------------------------------------------------------
# POST /import/<id>/pro-entries/confirm — write to database
# ---------------------------------------------------------------------------
@import_pro_bp.route('/<int:tournament_id>/pro-entries/confirm', methods=['POST'])
def confirm_pro_entries(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    temp_name = session.get(_session_key(tournament_id))
    if not temp_name:
        flash('No import data found. Please upload the file again.', 'error')
        return redirect(url_for('import_pro.upload_pro_entries', tournament_id=tournament_id))

    try:
        with open(_temp_path(temp_name), encoding='utf-8') as fh:
            entries = json.load(fh)
    except (OSError, json.JSONDecodeError):
        flash('Import data is missing or corrupt. Please upload the file again.', 'error')
        session.pop(_session_key(tournament_id), None)
        return redirect(url_for('import_pro.upload_pro_entries', tournament_id=tournament_id))

    # Build event name -> Event lookup for this tournament
    pro_events = Event.query.filter_by(tournament_id=tournament_id, event_type='pro').all()
    event_by_name = {e.name.strip(): e for e in pro_events}

    imported = 0
    updated  = 0
    errors   = []
    now      = datetime.utcnow()

    for entry in entries:
        try:
            # ---- Find or create competitor ----
            competitor = None
            if entry.get('email'):
                competitor = ProCompetitor.query.filter_by(
                    tournament_id=tournament_id,
                    email=entry['email']
                ).first()

            is_new = competitor is None
            if is_new:
                competitor = ProCompetitor(tournament_id=tournament_id)

            # ---- Scalar fields ----
            competitor.name          = entry['name']
            competitor.gender        = entry['gender']
            competitor.email         = entry.get('email')
            competitor.phone         = entry.get('phone')
            competitor.address       = entry.get('mailing_address')  # existing column
            competitor.is_ala_member = entry.get('ala_member', False)
            competitor.pro_am_lottery_opt_in = entry.get('relay_lottery', False)
            competitor.waiver_accepted    = entry.get('waiver_accepted', False)
            competitor.waiver_signature   = entry.get('waiver_signature')
            competitor.gear_sharing_details = entry.get('gear_sharing_details')
            competitor.notes          = entry.get('notes')
            competitor.total_fees     = entry.get('total_fees', 0)
            competitor.import_timestamp = now

            if entry.get('submission_timestamp'):
                try:
                    competitor.submission_timestamp = datetime.fromisoformat(
                        entry['submission_timestamp']
                    )
                except (ValueError, TypeError):
                    pass

            # ---- Event entry list ----
            # Prefer event IDs when the event exists; fall back to name strings.
            event_ids_or_names = []
            for event_name in entry.get('events', []):
                ev = event_by_name.get(event_name)
                event_ids_or_names.append(ev.id if ev else event_name)
            competitor.set_events_entered(event_ids_or_names)

            # ---- Entry fees JSON ----
            for event_name in entry.get('events', []):
                ev  = event_by_name.get(event_name)
                key = str(ev.id) if ev else event_name
                competitor.set_entry_fee(key, _EVENT_FEES.get(event_name, 0))
            if entry.get('relay_lottery'):
                competitor.set_entry_fee('relay', 5)

            # ---- Partners JSON ----
            for event_name, partner_name in entry.get('partners', {}).items():
                ev  = event_by_name.get(event_name)
                key = str(ev.id) if ev else event_name
                competitor.set_partner(key, partner_name)

            if is_new:
                db.session.add(competitor)

            db.session.flush()  # ensure competitor.id is available

            # ---- EventResult records (only when event exists in tournament) ----
            for event_name in entry.get('events', []):
                ev = event_by_name.get(event_name)
                if ev is None:
                    continue  # event not yet configured — skip result row
                existing_result = EventResult.query.filter_by(
                    event_id=ev.id,
                    competitor_id=competitor.id,
                    competitor_type='pro'
                ).first()
                if not existing_result:
                    partner_name = entry.get('partners', {}).get(event_name)
                    db.session.add(EventResult(
                        event_id=ev.id,
                        competitor_id=competitor.id,
                        competitor_type='pro',
                        competitor_name=competitor.name,
                        partner_name=partner_name,
                        status='pending',
                    ))

            if is_new:
                imported += 1
            else:
                updated += 1

        except Exception as exc:
            errors.append(f"{entry.get('name', 'Unknown')}: {exc}")

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        flash(f'Database error during commit: {exc}', 'error')
        return redirect(url_for('import_pro.review_pro_entries', tournament_id=tournament_id))

    # Clean up temp file and session key
    try:
        os.remove(_temp_path(temp_name))
    except OSError:
        pass
    session.pop(_session_key(tournament_id), None)

    summary = f'Import complete: {imported} added, {updated} updated.'
    log_action('pro_import_confirmed', 'tournament', tournament_id, {
        'imported': imported,
        'updated': updated,
        'errors': len(errors),
    })
    db.session.commit()
    if errors:
        summary += f'  {len(errors)} error(s): ' + '; '.join(errors[:5])
        if len(errors) > 5:
            summary += f' (and {len(errors) - 5} more)'
        flash(summary, 'warning')
    else:
        flash(summary, 'success')

    return redirect(url_for('main.pro_dashboard', tournament_id=tournament_id))
