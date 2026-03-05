"""
Registration routes for uploading and managing competitor entries.
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
import json
from database import db
from models import Tournament, Team, CollegeCompetitor, ProCompetitor, Event, EventResult, Heat
import strings as text
from services.audit import log_action
from services.cache_invalidation import invalidate_tournament_caches
from services.upload_security import malware_scan, save_upload, validate_excel_upload
from services.gear_sharing import build_name_index, normalize_person_name, resolve_partner_name

registration_bp = Blueprint('registration', __name__)

ALLOWED_EXTENSIONS = {'xlsx', 'xls'}


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@registration_bp.route('/<int:tournament_id>/college')
def college_registration(tournament_id):
    """College team registration page."""
    tournament = Tournament.query.get_or_404(tournament_id)
    all_teams = tournament.teams.all()
    valid_teams = [t for t in all_teams if t.status != 'invalid']
    invalid_teams = [t for t in all_teams if t.status == 'invalid']

    return render_template('college/registration.html',
                           tournament=tournament,
                           all_teams=all_teams,
                           teams=valid_teams,
                           invalid_teams=invalid_teams)


@registration_bp.route('/<int:tournament_id>/college/upload', methods=['POST'])
def upload_college_entry(tournament_id):
    """Upload and process a college entry form Excel file."""
    tournament = Tournament.query.get_or_404(tournament_id)

    if 'file' not in request.files:
        flash(text.FLASH['no_file'], 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    file = request.files['file']

    if file.filename == '':
        flash(text.FLASH['no_file'], 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    if file and allowed_file(file.filename):
        validation = validate_excel_upload(file, ALLOWED_EXTENSIONS)
        if not validation.ok:
            flash(validation.error, 'error')
            return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

        filepath = save_upload(file, current_app.config['UPLOAD_FOLDER'], validation.safe_name)

        # Import the Excel processing service
        from services.excel_io import process_college_entry_form

        try:
            malware_scan(
                filepath,
                enabled=bool(current_app.config.get('ENABLE_UPLOAD_MALWARE_SCAN', False)),
                command_template=current_app.config.get('MALWARE_SCAN_COMMAND', '')
            )
            result = process_college_entry_form(filepath, tournament)
            valid_teams = result.get('teams', 0)
            invalid_teams = result.get('invalid_teams', 0)
            log_action('college_upload_imported', 'tournament', tournament.id, {
                'teams': valid_teams,
                'invalid_teams': invalid_teams,
                'competitors': result.get('competitors', 0),
                'filename': validation.safe_name,
            })
            invalidate_tournament_caches(tournament_id)
            if invalid_teams:
                flash(
                    text.FLASH['import_success'].format(teams=valid_teams, competitors=result['competitors'])
                    + f' {invalid_teams} team(s) had errors and were saved as invalid — see below.',
                    'warning'
                )
            else:
                flash(text.FLASH['import_success'].format(teams=valid_teams, competitors=result['competitors']), 'success')
        except Exception as e:
            db.session.rollback()
            flash(text.FLASH['import_error'].format(error=str(e)), 'error')

        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    flash(text.FLASH['invalid_file_type'], 'error')
    return redirect(url_for('registration.college_registration', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/college/team/<int:team_id>')
def team_detail(tournament_id, team_id):
    """View and edit team details."""
    tournament = Tournament.query.get_or_404(tournament_id)
    team = Team.query.get_or_404(team_id)
    if team.tournament_id != tournament.id:
        flash('Team not found in this tournament.', 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    members = sorted(
        team.members.all(),
        key=lambda m: (m.status != 'active', -m.individual_points, m.name.lower())
    )
    college_events = Event.query.filter_by(tournament_id=tournament.id, event_type='college').all()
    event_name_by_id = {str(event.id): event.display_name for event in college_events}
    event_name_lookup = {}
    for event in college_events:
        event_name_lookup[event.name.strip().lower()] = event.display_name
        event_name_lookup[event.display_name.strip().lower()] = event.display_name

    member_event_labels = {}
    for member in members:
        labels = []
        for entered_event in member.get_events_entered():
            event_key = str(entered_event).strip()
            if not event_key:
                continue
            if event_key in event_name_by_id:
                labels.append(event_name_by_id[event_key])
            else:
                labels.append(event_name_lookup.get(event_key.lower(), event_key))
        member_event_labels[member.id] = list(dict.fromkeys(labels))

    return render_template('college/team_detail.html',
                           tournament=tournament,
                           team=team,
                           members=members,
                           member_event_labels=member_event_labels)


@registration_bp.route('/<int:tournament_id>/college/competitor/<int:competitor_id>/scratch', methods=['POST'])
def scratch_college_competitor(tournament_id, competitor_id):
    """Scratch a college competitor and remove from uncompleted heats."""
    competitor = CollegeCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament_id:
        flash('Competitor not found in this tournament.', 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    competitor.status = 'scratched'
    _remove_college_competitor_from_unfinished_heats(competitor.id, tournament_id)

    college_event_ids = [e.id for e in Event.query.filter_by(tournament_id=tournament_id, event_type='college').all()]
    if college_event_ids:
        EventResult.query.filter(
            EventResult.event_id.in_(college_event_ids),
            EventResult.competitor_type == 'college',
            EventResult.competitor_id == competitor.id,
            EventResult.status != 'completed'
        ).update({EventResult.status: 'scratched'}, synchronize_session=False)

    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    flash(text.FLASH['competitor_scratched'].format(name=competitor.name), 'warning')
    return redirect(url_for('registration.team_detail', tournament_id=tournament_id, team_id=competitor.team_id))


@registration_bp.route('/<int:tournament_id>/college/competitor/<int:competitor_id>/delete', methods=['POST'])
def delete_college_competitor(tournament_id, competitor_id):
    """Delete a college competitor from registration and schedule."""
    competitor = CollegeCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament_id:
        flash('Competitor not found in this tournament.', 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    team_id = competitor.team_id
    comp_name = competitor.name

    _remove_college_competitor_from_unfinished_heats(competitor.id, tournament_id)
    college_event_ids = [e.id for e in Event.query.filter_by(tournament_id=tournament_id, event_type='college').all()]
    if college_event_ids:
        EventResult.query.filter(
            EventResult.event_id.in_(college_event_ids),
            EventResult.competitor_type == 'college',
            EventResult.competitor_id == competitor.id
        ).delete(synchronize_session=False)

    db.session.delete(competitor)
    db.session.commit()
    invalidate_tournament_caches(tournament_id)

    flash(f'Competitor "{comp_name}" deleted.', 'warning')
    return redirect(url_for('registration.team_detail', tournament_id=tournament_id, team_id=team_id))


@registration_bp.route('/<int:tournament_id>/college/team/<int:team_id>/delete', methods=['POST'])
def delete_college_team(tournament_id, team_id):
    """Delete a college team and all its competitors."""
    team = Team.query.get_or_404(team_id)
    if team.tournament_id != tournament_id:
        flash('Team not found in this tournament.', 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    team_code = team.team_code
    members = team.members.all()
    college_event_ids = [e.id for e in Event.query.filter_by(tournament_id=tournament_id, event_type='college').all()]

    for competitor in members:
        _remove_college_competitor_from_unfinished_heats(competitor.id, tournament_id)
        if college_event_ids:
            EventResult.query.filter(
                EventResult.event_id.in_(college_event_ids),
                EventResult.competitor_type == 'college',
                EventResult.competitor_id == competitor.id
            ).delete(synchronize_session=False)
        db.session.delete(competitor)

    db.session.delete(team)
    db.session.commit()
    invalidate_tournament_caches(tournament_id)

    flash(f'Team "{team_code}" and {len(members)} competitor(s) deleted.', 'warning')
    return redirect(url_for('registration.college_registration', tournament_id=tournament_id))


# ---------------------------------------------------------------------------
# Invalid-team fix routes
# ---------------------------------------------------------------------------

@registration_bp.route('/<int:tournament_id>/college/team/<int:team_id>/revalidate', methods=['POST'])
def revalidate_team(tournament_id, team_id):
    """Re-run constraint validation for a team and promote to active if clean."""
    from services.excel_io import _validate_college_entry_constraints
    team = Team.query.get_or_404(team_id)
    if team.tournament_id != tournament_id:
        flash('Team not found in this tournament.', 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    errors_by_team = _validate_college_entry_constraints({team_id})
    team_errors = errors_by_team.get(team_id, [])
    team.set_validation_errors(team_errors)
    db.session.commit()
    invalidate_tournament_caches(tournament_id)

    if not team_errors:
        flash(f'Team {team.team_code} passed validation and is now active.', 'success')
    else:
        flash(f'Team {team.team_code} still has {len(team_errors)} error(s). Fix them and re-validate.', 'warning')
    return redirect(url_for('registration.team_detail', tournament_id=tournament_id, team_id=team_id))


@registration_bp.route('/<int:tournament_id>/college/competitor/<int:competitor_id>/remove-event', methods=['POST'])
def remove_competitor_event(tournament_id, competitor_id):
    """Remove a single event from a competitor's entry and re-validate the team."""
    from services.excel_io import _validate_college_entry_constraints
    competitor = CollegeCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament_id:
        flash('Competitor not found in this tournament.', 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    event_name = (request.form.get('event_name') or '').strip()
    if not event_name:
        flash('No event specified.', 'error')
        return redirect(url_for('registration.team_detail', tournament_id=tournament_id, team_id=competitor.team_id))

    current_events = competitor.get_events_entered()
    updated_events = [e for e in current_events if str(e).strip() != event_name]
    competitor.set_events_entered(updated_events)

    # Also remove the partner entry for the removed event
    partners = competitor.get_partners()
    if event_name in partners:
        del partners[event_name]
        competitor.partners = json.dumps(partners)

    db.session.flush()

    # Re-validate team
    errors_by_team = _validate_college_entry_constraints({competitor.team_id})
    team = Team.query.get(competitor.team_id)
    if team:
        team_errors = errors_by_team.get(competitor.team_id, [])
        team.set_validation_errors(team_errors)

    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    flash(f'Event "{event_name}" removed from {competitor.name}.', 'info')
    return redirect(url_for('registration.team_detail', tournament_id=tournament_id, team_id=competitor.team_id))


@registration_bp.route('/<int:tournament_id>/college/competitor/<int:competitor_id>/set-partner', methods=['POST'])
def set_competitor_partner(tournament_id, competitor_id):
    """Set or clear a competitor's partner for a specific event, then re-validate the team."""
    from services.excel_io import _validate_college_entry_constraints
    competitor = CollegeCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament_id:
        flash('Competitor not found in this tournament.', 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    event_name = (request.form.get('event_name') or '').strip()
    partner_name = (request.form.get('partner_name') or '').strip()

    if not event_name:
        flash('No event specified.', 'error')
        return redirect(url_for('registration.team_detail', tournament_id=tournament_id, team_id=competitor.team_id))

    competitor.set_partner(event_name, partner_name)
    db.session.flush()

    # Re-validate team
    errors_by_team = _validate_college_entry_constraints({competitor.team_id})
    team = Team.query.get(competitor.team_id)
    if team:
        team_errors = errors_by_team.get(competitor.team_id, [])
        team.set_validation_errors(team_errors)

    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    if partner_name:
        flash(f'Partner updated for {competitor.name} in "{event_name}".', 'info')
    else:
        flash(f'Partner removed for {competitor.name} in "{event_name}".', 'info')
    return redirect(url_for('registration.team_detail', tournament_id=tournament_id, team_id=competitor.team_id))


@registration_bp.route('/<int:tournament_id>/pro')
def pro_registration(tournament_id):
    """Legacy pro registration route now merged into the Pro dashboard."""
    Tournament.query.get_or_404(tournament_id)
    return redirect(url_for('main.pro_dashboard', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/new', methods=['GET', 'POST'])
def new_pro_competitor(tournament_id):
    """Add a new professional competitor."""
    tournament = Tournament.query.get_or_404(tournament_id)

    if request.method == 'POST':
        competitor = ProCompetitor(
            tournament_id=tournament_id,
            name=request.form.get('name'),
            gender=request.form.get('gender'),
            address=request.form.get('address'),
            phone=request.form.get('phone'),
            email=request.form.get('email'),
            shirt_size=request.form.get('shirt_size'),
            is_ala_member=request.form.get('is_ala_member') == 'on',
            pro_am_lottery_opt_in=request.form.get('pro_am_lottery') == 'on',
            is_left_handed_springboard=request.form.get('left_handed') == 'on',
            springboard_slow_heat=request.form.get('springboard_slow_heat') == 'on',
        )

        db.session.add(competitor)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)

        flash(text.FLASH['competitor_added'].format(name=competitor.name), 'success')
        return redirect(url_for('registration.pro_registration', tournament_id=tournament_id))

    return render_template('pro/new_competitor.html', tournament=tournament)


@registration_bp.route('/<int:tournament_id>/pro/<int:competitor_id>')
def pro_competitor_detail(tournament_id, competitor_id):
    """View and edit professional competitor details."""
    tournament = Tournament.query.get_or_404(tournament_id)
    competitor = ProCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament.id:
        flash('Competitor not found in this tournament.', 'error')
        return redirect(url_for('registration.pro_registration', tournament_id=tournament_id))

    pro_events = Event.query.filter_by(tournament_id=tournament.id, event_type='pro').order_by(Event.name, Event.gender).all()
    event_name_by_id = {str(event.id): event.display_name for event in pro_events}
    event_name_lookup = {}
    for event in pro_events:
        event_name_lookup[event.name.strip().lower()] = event.display_name
        event_name_lookup[event.display_name.strip().lower()] = event.display_name

    event_labels = []
    for entered_event in competitor.get_events_entered():
        event_key = str(entered_event).strip()
        if not event_key:
            continue
        if event_key in event_name_by_id:
            event_labels.append((event_key, event_name_by_id[event_key]))
        else:
            event_labels.append((event_key, event_name_lookup.get(event_key.lower(), event_key)))

    gear_sharing_labels = []
    for event_key, partner in competitor.get_gear_sharing().items():
        label = event_name_by_id.get(str(event_key), event_name_lookup.get(str(event_key).strip().lower(), str(event_key)))
        gear_sharing_labels.append((label, partner))

    return render_template('pro/competitor_detail.html',
                           tournament=tournament,
                           competitor=competitor,
                           pro_events=pro_events,
                           event_labels=event_labels,
                           gear_sharing_labels=gear_sharing_labels,
                           partner_map=competitor.get_partners())


@registration_bp.route('/<int:tournament_id>/pro/<int:competitor_id>/update-events', methods=['POST'])
def update_pro_events(tournament_id, competitor_id):
    """Update pro competitor event enrollment, fees, and gear sharing."""
    tournament = Tournament.query.get_or_404(tournament_id)
    competitor = ProCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament.id:
        flash('Competitor not found in this tournament.', 'error')
        return redirect(url_for('registration.pro_registration', tournament_id=tournament_id))

    pro_events = Event.query.filter_by(tournament_id=tournament.id, event_type='pro').all()
    all_pro_names = [c.name for c in ProCompetitor.query.filter_by(tournament_id=tournament.id).all()]
    name_index = build_name_index(all_pro_names)
    selected_ids = set(request.form.getlist('event_ids'))
    competitor.set_events_entered(list(selected_ids))
    competitor.is_left_handed_springboard = request.form.get('left_handed') == 'on'
    competitor.springboard_slow_heat = request.form.get('springboard_slow_heat') == 'on'

    new_fees = {}
    new_paid = {}
    new_gear_sharing = {}
    new_partners = {}

    for event in pro_events:
        eid = str(event.id)
        fee_raw = (request.form.get(f'fee_{eid}') or '').strip()
        try:
            fee = float(fee_raw) if fee_raw else 0.0
        except (TypeError, ValueError):
            fee = 0.0
        paid = request.form.get(f'paid_{eid}') == 'on'
        gear_raw = (request.form.get(f'gear_{eid}') or '').strip()
        partner_raw = (request.form.get(f'partner_{eid}') or '').strip()
        gear = resolve_partner_name(gear_raw, name_index)
        partner = resolve_partner_name(partner_raw, name_index)

        if eid in selected_ids or fee > 0:
            new_fees[eid] = fee
            new_paid[eid] = paid
        if gear and normalize_person_name(gear) != normalize_person_name(competitor.name):
            new_gear_sharing[eid] = gear
        if partner and normalize_person_name(partner) != normalize_person_name(competitor.name):
            new_partners[eid] = partner

    competitor.entry_fees = json.dumps(new_fees)
    competitor.fees_paid = json.dumps(new_paid)
    competitor.gear_sharing = json.dumps(new_gear_sharing)
    competitor.partners = json.dumps(new_partners)

    log_action('pro_events_updated', 'pro_competitor', competitor.id, {
        'tournament_id': tournament_id,
        'events': list(selected_ids),
    })
    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    flash(f'Events updated for {competitor.name}.', 'success')
    return redirect(url_for('registration.pro_competitor_detail', tournament_id=tournament_id, competitor_id=competitor_id))


@registration_bp.route('/<int:tournament_id>/pro/auto-assign-partners', methods=['POST'])
def auto_assign_pro_partners_route(tournament_id):
    """Auto assign partners for pro partnered events."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.partner_matching import auto_assign_pro_partners

    summary = auto_assign_pro_partners(tournament)
    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    log_action('pro_partners_auto_assigned', 'tournament', tournament_id, summary)
    flash(
        f"Auto-assigned {summary['assigned_pairs']} partner pair(s) across {summary['event_count']} event(s). "
        f"Unmatched competitors: {summary['unmatched']}.",
        'success'
    )
    return redirect(url_for('registration.pro_registration', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/<int:competitor_id>/scratch', methods=['POST'])
def scratch_pro_competitor(tournament_id, competitor_id):
    """Scratch a professional competitor."""
    competitor = ProCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament_id:
        flash('Competitor not found in this tournament.', 'error')
        return redirect(url_for('registration.pro_registration', tournament_id=tournament_id))
    competitor.status = 'scratched'
    db.session.commit()
    invalidate_tournament_caches(tournament_id)

    flash(text.FLASH['competitor_scratched'].format(name=competitor.name), 'warning')
    return redirect(url_for('registration.pro_registration', tournament_id=tournament_id))


def _remove_college_competitor_from_unfinished_heats(competitor_id: int, tournament_id: int):
    """Remove a competitor from uncompleted college heats and stand assignments."""
    heats = Heat.query.join(Event).filter(
        Event.tournament_id == tournament_id,
        Event.id == Heat.event_id,
        Event.event_type == 'college',
        Heat.status != 'completed'
    ).all()

    for heat in heats:
        comp_ids = heat.get_competitors()
        if competitor_id in comp_ids:
            heat.remove_competitor(competitor_id)
            assignments = heat.get_stand_assignments()
            if str(competitor_id) in assignments:
                del assignments[str(competitor_id)]
                heat.stand_assignments = json.dumps(assignments)


# ---------------------------------------------------------------------------
# #14 — Competitor headshot upload
# ---------------------------------------------------------------------------

_ALLOWED_IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'webp'}
_ALLOWED_IMAGE_MAGIC = {
    b'\xff\xd8\xff',           # JPEG
    b'\x89PNG',                # PNG
    b'RIFF',                   # WebP (RIFF....WEBP)
}


def _validate_image(file_storage) -> bool:
    """Return True if the file has an allowed extension and image magic bytes."""
    name = file_storage.filename or ''
    if '.' not in name:
        return False
    ext = name.rsplit('.', 1)[1].lower()
    if ext not in _ALLOWED_IMAGE_EXTS:
        return False
    header = file_storage.stream.read(8)
    file_storage.stream.seek(0)
    return any(header.startswith(magic) for magic in _ALLOWED_IMAGE_MAGIC)


@registration_bp.route('/<int:tournament_id>/pro/<int:competitor_id>/upload-headshot', methods=['POST'])
def upload_pro_headshot(tournament_id, competitor_id):
    """Upload a headshot image for a pro competitor."""
    competitor = ProCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament_id:
        flash('Competitor not found in this tournament.', 'error')
        return redirect(url_for('registration.pro_registration', tournament_id=tournament_id))

    f = request.files.get('headshot')
    if not f or not f.filename:
        flash('No image file selected.', 'error')
        return redirect(url_for('registration.pro_competitor_detail',
                                tournament_id=tournament_id, competitor_id=competitor_id))

    if not _validate_image(f):
        flash('Invalid image. Use JPG, PNG, or WebP.', 'error')
        return redirect(url_for('registration.pro_competitor_detail',
                                tournament_id=tournament_id, competitor_id=competitor_id))

    import uuid as _uuid
    import os
    ext = f.filename.rsplit('.', 1)[1].lower()
    filename = f'headshot_{_uuid.uuid4().hex}.{ext}'
    headshots_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'headshots')
    os.makedirs(headshots_dir, exist_ok=True)
    save_path = os.path.join(headshots_dir, filename)
    f.save(save_path)

    # Delete old headshot file if present
    if competitor.headshot_filename:
        old_path = os.path.join(headshots_dir, competitor.headshot_filename)
        try:
            os.remove(old_path)
        except OSError:
            pass

    competitor.headshot_filename = filename
    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    log_action('headshot_uploaded', 'pro_competitor', competitor_id, {'filename': filename})
    flash(f'Headshot uploaded for {competitor.name}.', 'success')
    return redirect(url_for('registration.pro_competitor_detail',
                            tournament_id=tournament_id, competitor_id=competitor_id))


@registration_bp.route('/headshots/<path:filename>')
def serve_headshot(filename):
    """Serve uploaded headshot images."""
    import os
    from flask import send_from_directory, abort as flask_abort
    headshots_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'headshots')
    # Security: disallow path traversal
    if '..' in filename or filename.startswith('/'):
        flask_abort(400)
    return send_from_directory(headshots_dir, filename)
