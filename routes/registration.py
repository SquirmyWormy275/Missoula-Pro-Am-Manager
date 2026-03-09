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
        except (ValueError, KeyError, TypeError, OSError) as e:
            db.session.rollback()
            # Log full detail server-side; show only a safe summary to the user.
            current_app.logger.exception('College upload failed for tournament %s', tournament_id)
            flash(text.FLASH['import_error'].format(error='File could not be processed. Check the format and try again.'), 'error')
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Unexpected error during college upload for tournament %s', tournament_id)
            flash('An unexpected error occurred during import. Please contact an administrator.', 'error')

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

    # Build per-member event details for the inline event editor
    member_event_details = {}
    for member in members:
        partners = member.get_partners()
        details = []
        for entered_event in member.get_events_entered():
            event_key = str(entered_event).strip()
            if not event_key:
                continue
            if event_key in event_name_by_id:
                display = event_name_by_id[event_key]
            else:
                display = event_name_lookup.get(event_key.lower(), event_key)
            partner = partners.get(event_key, '') or partners.get(display, '')
            details.append({'key': event_key, 'display': display, 'partner': partner})
        member_event_details[member.id] = details

    return render_template('college/team_detail.html',
                           tournament=tournament,
                           team=team,
                           members=members,
                           college_events=college_events,
                           member_event_labels=member_event_labels,
                           member_event_details=member_event_details)


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

    # Remove gear-sharing entries on active college competitors that reference this person.
    from services.gear_sharing import cleanup_scratched_gear_entries
    tournament = Tournament.query.get_or_404(tournament_id)
    gear_result = cleanup_scratched_gear_entries(tournament, scratched_competitor=competitor, competitor_type='college')

    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    msg = text.FLASH['competitor_scratched'].format(name=competitor.name)
    if gear_result['cleaned']:
        msg += f' Removed {gear_result["cleaned"]} gear-sharing reference(s) from {len(gear_result["affected"])} competitor(s).'
    flash(msg, 'warning')
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


@registration_bp.route('/<int:tournament_id>/college/competitor/<int:competitor_id>/add-event', methods=['POST'])
def add_competitor_event(tournament_id, competitor_id):
    """Add a single event to a competitor's entry and re-validate the team."""
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

    current_events = competitor.get_events_entered()
    if event_name not in current_events:
        current_events.append(event_name)
        competitor.set_events_entered(current_events)

    if partner_name:
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
    flash(f'Event "{event_name}" added to {competitor.name}.', 'info')
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

    # Build gear_sharing_labels with reciprocal status.
    from services.gear_sharing import normalize_person_name as _norm_name
    all_pro_comps_active = {
        _norm_name(c.name): c
        for c in ProCompetitor.query.filter_by(tournament_id=tournament.id, status='active').all()
    }
    gear_sharing_labels = []
    for event_key, partner in competitor.get_gear_sharing().items():
        label = event_name_by_id.get(str(event_key), event_name_lookup.get(str(event_key).strip().lower(), str(event_key)))
        partner_text = str(partner or '').strip()
        partner_on_roster = _norm_name(partner_text) in all_pro_comps_active if partner_text else False
        # Reciprocal is always Yes if the partner is on the active roster.
        gear_sharing_labels.append({
            'event_label': label,
            'event_key': event_key,
            'partner': partner_text,
            'reciprocal': partner_on_roster,
        })

    # Last 10 gear-related audit log entries for this competitor.
    from models.audit_log import AuditLog
    gear_audit = (
        AuditLog.query
        .filter_by(entity_type='pro_competitor', entity_id=competitor.id)
        .filter(AuditLog.action.like('gear%'))
        .order_by(AuditLog.created_at.desc())
        .limit(10)
        .all()
    )

    return render_template('pro/competitor_detail.html',
                           tournament=tournament,
                           competitor=competitor,
                           pro_events=pro_events,
                           event_labels=event_labels,
                           gear_sharing_labels=gear_sharing_labels,
                           gear_audit=gear_audit,
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

    old_gear = competitor.get_gear_sharing()
    competitor.entry_fees = json.dumps(new_fees)
    competitor.fees_paid = json.dumps(new_paid)
    competitor.gear_sharing = json.dumps(new_gear_sharing)
    competitor.partners = json.dumps(new_partners)

    # Write reciprocals and clear removed gear entries on partner competitors.
    from services.gear_sharing import sync_all_gear_for_competitor
    all_pro_comps = ProCompetitor.query.filter_by(tournament_id=tournament.id, status='active').all()
    pro_comps_by_norm = {normalize_person_name(c.name): c for c in all_pro_comps}
    sync_all_gear_for_competitor(competitor, pro_comps_by_norm, old_gear=old_gear)

    log_action('pro_events_updated', 'pro_competitor', competitor.id, {
        'tournament_id': tournament_id,
        'events': list(selected_ids),
    })
    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    flash(f'Events updated for {competitor.name}.', 'success')
    return redirect(url_for('registration.pro_competitor_detail', tournament_id=tournament_id, competitor_id=competitor_id))


# ---------------------------------------------------------------------------
# Pro gear-sharing manager
# ---------------------------------------------------------------------------

@registration_bp.route('/<int:tournament_id>/pro/gear-sharing')
def pro_gear_manager(tournament_id):
    """Gear-sharing audit and management dashboard for pro competitors."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.gear_sharing import build_gear_report, get_gear_groups
    report = build_gear_report(tournament)
    gear_groups = get_gear_groups(tournament)
    pro_comps = ProCompetitor.query.filter_by(
        tournament_id=tournament_id, status='active'
    ).order_by(ProCompetitor.name).all()
    pro_events = Event.query.filter_by(
        tournament_id=tournament_id, event_type='pro'
    ).order_by(Event.name, Event.gender).all()
    college_events = Event.query.filter_by(
        tournament_id=tournament_id, event_type='college'
    ).order_by(Event.name, Event.gender).all()
    return render_template(
        'pro/gear_sharing.html',
        tournament=tournament,
        report=report,
        gear_groups=gear_groups,
        pro_comps=pro_comps,
        pro_events=pro_events,
        college_events=college_events,
    )


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/parse', methods=['POST'])
def pro_gear_parse(tournament_id):
    """Parse free-text gear_sharing_details fields into structured gear_sharing maps."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.gear_sharing import parse_all_gear_details
    try:
        result = parse_all_gear_details(tournament)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        msg = f'Gear parser complete: {result["parsed"]} competitor(s) structured'
        if result['skipped']:
            msg += f', {result["skipped"]} skipped (already structured)'
        if result['warnings']:
            msg += f', {len(result["warnings"])} warning(s)'
        msg += '.'
        flash(msg, 'success' if result['parsed'] > 0 else 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Gear parser error: {e}', 'error')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/sync-heats', methods=['POST'])
def pro_gear_sync_heats(tournament_id):
    """Detect and auto-fix gear-sharing conflicts in existing pro heats."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.gear_sharing import fix_heat_gear_conflicts
    try:
        result = fix_heat_gear_conflicts(tournament)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        msg = f'Heat sync complete: {result["fixed"]} conflict(s) resolved.'
        if result['failed']:
            msg += (
                f' {len(result["failed"])} conflict(s) could not be auto-resolved'
                ' — no compatible target heat was available.'
            )
        flash(msg, 'warning' if result['failed'] else 'success')
        log_action('gear_heat_sync', 'tournament', tournament_id, {
            'fixed': result['fixed'],
            'failed': len(result['failed']),
        })
    except Exception as e:
        db.session.rollback()
        flash(f'Heat sync error: {e}', 'error')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/update', methods=['POST'])
def pro_gear_update(tournament_id):
    """Set or clear a single gear-sharing entry for a pro competitor."""
    tournament = Tournament.query.get_or_404(tournament_id)
    try:
        competitor_id = int(request.form.get('competitor_id', ''))
    except (TypeError, ValueError):
        flash('Invalid competitor ID.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    competitor = ProCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament_id:
        flash('Competitor not found in this tournament.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    raw_event_key = (request.form.get('event_key') or '').strip()
    partner_name = (request.form.get('partner_name') or '').strip()

    if not raw_event_key:
        flash('No event key specified.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    from services.gear_sharing import (
        normalize_gear_key_to_event_id, sync_gear_bidirectional,
        normalize_person_name,
    )
    pro_events = Event.query.filter_by(tournament_id=tournament_id, event_type='pro').all()
    event_key = normalize_gear_key_to_event_id(raw_event_key, pro_events)

    sharing = competitor.get_gear_sharing()
    if partner_name:
        partner_norm = normalize_person_name(partner_name)
        # Find partner on active roster for bidirectional sync.
        partner_comp = next(
            (c for c in ProCompetitor.query.filter_by(
                tournament_id=tournament_id, status='active').all()
             if normalize_person_name(c.name) == partner_norm),
            None,
        )
        if partner_comp and partner_comp.id != competitor.id:
            sync_gear_bidirectional(competitor, partner_comp, event_key)
            flash(
                f'Gear sharing set: {competitor.name} + {partner_comp.name}'
                f' (key "{event_key}") — reciprocal written.',
                'success',
            )
        else:
            sharing[event_key] = partner_name
            competitor.gear_sharing = json.dumps(sharing)
            flash(
                f'Gear sharing set: {competitor.name} + {partner_name}'
                f' (key "{event_key}").',
                'success',
            )
        # Warn when heats already exist for the affected event.
        event_obj = next((e for e in pro_events if str(e.id) == str(event_key)), None)
        if event_obj and event_obj.heats.count() > 0:
            flash(
                f'Note: heats already exist for {event_obj.display_name}. '
                'Run "Sync Heat Conflicts" to update gear-conflict placement.',
                'warning',
            )
    else:
        sharing.pop(event_key, None)
        competitor.gear_sharing = json.dumps(sharing)
        flash(f'Gear sharing entry removed for {competitor.name} (key "{event_key}").', 'info')

    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    log_action('gear_sharing_updated', 'pro_competitor', competitor_id, {
        'event_key': event_key,
        'partner': partner_name,
    })
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/update-ajax', methods=['POST'])
def pro_gear_update_ajax(tournament_id):
    """AJAX JSON endpoint for inline gear-sharing edits in the dashboard unresolved table."""
    from flask import jsonify
    Tournament.query.get_or_404(tournament_id)
    try:
        competitor_id = int(request.form.get('competitor_id', ''))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Invalid competitor ID'}), 400

    competitor = ProCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament_id:
        return jsonify({'ok': False, 'error': 'Competitor not found in this tournament'}), 403

    raw_event_key = (request.form.get('event_key') or '').strip()
    partner_name = (request.form.get('partner_name') or '').strip()

    if not raw_event_key:
        return jsonify({'ok': False, 'error': 'No event key specified'}), 400

    from services.gear_sharing import (
        normalize_gear_key_to_event_id, normalize_person_name,
        sync_gear_bidirectional,
    )
    pro_events = Event.query.filter_by(tournament_id=tournament_id, event_type='pro').all()
    event_key = normalize_gear_key_to_event_id(raw_event_key, pro_events)

    sharing = competitor.get_gear_sharing()
    if partner_name:
        partner_norm = normalize_person_name(partner_name)
        partner_comp = ProCompetitor.query.filter_by(tournament_id=tournament_id, status='active').all()
        partner_comp = next(
            (c for c in partner_comp if normalize_person_name(c.name) == partner_norm),
            None
        )
        if partner_comp and partner_comp.id != competitor.id:
            sync_gear_bidirectional(competitor, partner_comp, event_key)
        else:
            sharing[event_key] = partner_name
            competitor.gear_sharing = json.dumps(sharing)
    else:
        sharing.pop(event_key, None)
        competitor.gear_sharing = json.dumps(sharing)

    try:
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('gear_sharing_updated', 'pro_competitor', competitor_id, {
            'event_key': event_key,
            'partner': partner_name,
            'via': 'ajax',
        })
        return jsonify({'ok': True, 'partner_saved': partner_name, 'event_key': event_key})
    except Exception as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc)}), 500


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/remove', methods=['POST'])
def pro_gear_remove(tournament_id):
    """Remove a gear-sharing entry from a pro competitor."""
    Tournament.query.get_or_404(tournament_id)
    try:
        competitor_id = int(request.form.get('competitor_id', ''))
    except (TypeError, ValueError):
        flash('Invalid competitor ID.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    competitor = ProCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament_id:
        flash('Competitor not found.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    event_key = (request.form.get('event_key') or '').strip()
    sharing = competitor.get_gear_sharing()
    if event_key in sharing:
        del sharing[event_key]
        competitor.gear_sharing = json.dumps(sharing)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('gear_sharing_removed', 'pro_competitor', competitor_id, {'event_key': event_key})
        flash(f'Gear sharing entry removed for {competitor.name}.', 'info')
    else:
        flash('Entry not found — nothing removed.', 'warning')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


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


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/complete-pairs', methods=['POST'])
def pro_gear_complete_pairs(tournament_id):
    """Write reciprocal gear-sharing entries for all one-sided pairs."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.gear_sharing import complete_one_sided_pairs
    try:
        result = complete_one_sided_pairs(tournament)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('gear_complete_pairs', 'tournament', tournament_id, result)
        flash(f'Reciprocals written for {result["completed"]} one-sided gear pair(s).', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error completing pairs: {e}', 'error')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/cleanup-scratched', methods=['POST'])
def pro_gear_cleanup_scratched(tournament_id):
    """Remove gear-sharing entries referencing scratched competitors."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.gear_sharing import cleanup_scratched_gear_entries
    try:
        result = cleanup_scratched_gear_entries(tournament)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('gear_cleanup_scratched', 'tournament', tournament_id, {
            'cleaned': result['cleaned'],
            'affected': result['affected'],
        })
        if result['cleaned']:
            flash(
                f'Cleaned {result["cleaned"]} stale gear reference(s) from'
                f' {len(result["affected"])} competitor(s).',
                'success',
            )
        else:
            flash('No stale gear references found.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Error cleaning up scratched entries: {e}', 'error')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/auto-partners', methods=['POST'])
def pro_gear_auto_partners(tournament_id):
    """Copy gear_sharing entries into partners for partnered events."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.gear_sharing import auto_populate_partners_from_gear
    try:
        result = auto_populate_partners_from_gear(tournament)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('gear_auto_partners', 'tournament', tournament_id, result)
        flash(
            f'Partner fields auto-populated from gear sharing for {result["updated"]} competitor(s).',
            'success',
        )
    except Exception as e:
        db.session.rollback()
        flash(f'Error auto-populating partners: {e}', 'error')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/parse-review')
def pro_gear_parse_review(tournament_id):
    """Show proposed gear-sharing parse results for review before committing."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.gear_sharing import build_parse_review
    rows = build_parse_review(tournament)
    return render_template(
        'pro/gear_parse_review.html',
        tournament=tournament,
        rows=rows,
    )


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/parse-confirm', methods=['POST'])
def pro_gear_parse_confirm(tournament_id):
    """Commit approved parse rows from the parse-review page."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.gear_sharing import (
        build_parse_review, sync_all_gear_for_competitor, normalize_person_name,
    )
    all_pro_comps = ProCompetitor.query.filter_by(
        tournament_id=tournament_id, status='active'
    ).all()
    pro_comps_by_norm = {normalize_person_name(c.name): c for c in all_pro_comps}

    try:
        rows = build_parse_review(tournament)
        confirmed = 0
        for row in rows:
            comp = row['competitor']
            field_name = f'confirm_{comp.id}'
            if request.form.get(field_name) != 'on':
                continue
            old_gear = comp.get_gear_sharing()
            # Merge proposed gear into existing map (don't wipe existing entries).
            merged = dict(old_gear)
            merged.update(row['proposed_gear_map'])
            comp.gear_sharing = json.dumps(merged)
            sync_all_gear_for_competitor(comp, pro_comps_by_norm, old_gear=old_gear)
            log_action('gear_parse_confirmed', 'pro_competitor', comp.id, {
                'proposed': row['proposed_gear_map'],
            })
            confirmed += 1

        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        flash(f'Gear parse confirmed for {confirmed} competitor(s).', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error confirming parse: {e}', 'error')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/group-create', methods=['POST'])
def pro_gear_group_create(tournament_id):
    """Create or update a gear-sharing group (multiple pairs sharing one piece of equipment)."""
    tournament = Tournament.query.get_or_404(tournament_id)
    group_name = (request.form.get('group_name') or '').strip()
    event_key = (request.form.get('event_key') or '').strip()
    competitor_ids_raw = request.form.getlist('competitor_ids')

    if not group_name or not event_key:
        flash('Group name and event key are required.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    try:
        comp_ids = [int(v) for v in competitor_ids_raw if str(v).strip()]
    except (TypeError, ValueError):
        flash('Invalid competitor ID in group selection.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    if len(comp_ids) < 2:
        flash('Select at least 2 competitors to form a gear group.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    comps = ProCompetitor.query.filter(
        ProCompetitor.id.in_(comp_ids),
        ProCompetitor.tournament_id == tournament_id,
        ProCompetitor.status == 'active',
    ).all()
    if len(comps) < 2:
        flash('Could not find the selected competitors.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    from services.gear_sharing import create_gear_group, normalize_gear_key_to_event_id
    pro_events = Event.query.filter_by(tournament_id=tournament_id, event_type='pro').all()
    resolved_key = normalize_gear_key_to_event_id(event_key, pro_events)

    try:
        count = create_gear_group(comps, resolved_key, group_name)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('gear_group_created', 'tournament', tournament_id, {
            'group_name': group_name,
            'event_key': resolved_key,
            'competitor_ids': comp_ids,
        })
        flash(f'Gear group "{group_name}" set for {count} competitor(s).', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error creating gear group: {e}', 'error')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/group-remove', methods=['POST'])
def pro_gear_group_remove(tournament_id):
    """Remove a gear-sharing group entry from one or all members."""
    Tournament.query.get_or_404(tournament_id)
    group_name = (request.form.get('group_name') or '').strip()
    event_key = (request.form.get('event_key') or '').strip()
    remove_all = request.form.get('remove_all') == 'on'

    try:
        competitor_id = int(request.form.get('competitor_id', '') or 0)
    except (TypeError, ValueError):
        competitor_id = 0

    group_value = f'group:{group_name}'
    removed = 0
    if remove_all:
        targets = ProCompetitor.query.filter_by(
            tournament_id=tournament_id, status='active'
        ).all()
        for comp in targets:
            gear = comp.get_gear_sharing()
            if gear.get(event_key) == group_value:
                del gear[event_key]
                comp.gear_sharing = json.dumps(gear)
                removed += 1
    elif competitor_id:
        comp = ProCompetitor.query.get(competitor_id)
        if comp and comp.tournament_id == tournament_id:
            gear = comp.get_gear_sharing()
            if gear.get(event_key) == group_value:
                del gear[event_key]
                comp.gear_sharing = json.dumps(gear)
                removed += 1

    if removed:
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('gear_group_removed', 'tournament', tournament_id, {
            'group_name': group_name,
            'event_key': event_key,
            'competitor_id': competitor_id,
            'remove_all': remove_all,
        })
        flash(f'Removed {removed} competitor(s) from gear group "{group_name}".', 'info')
    else:
        flash('No matching gear group entries found.', 'warning')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/college/gear-sharing/update', methods=['POST'])
def college_gear_update(tournament_id):
    """Set a gear-sharing entry for a college competitor."""
    Tournament.query.get_or_404(tournament_id)
    from models.competitor import CollegeCompetitor
    try:
        competitor_id = int(request.form.get('competitor_id', ''))
    except (TypeError, ValueError):
        flash('Invalid competitor ID.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    comp = CollegeCompetitor.query.get_or_404(competitor_id)
    if comp.tournament_id != tournament_id:
        flash('Competitor not found.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    event_key = (request.form.get('event_key') or '').strip()
    partner_name = (request.form.get('partner_name') or '').strip()

    if not event_key:
        flash('No event key specified.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    gear = comp.get_gear_sharing()
    if partner_name:
        gear[event_key] = partner_name
        comp.gear_sharing = json.dumps(gear)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('college_gear_updated', 'college_competitor', competitor_id, {
            'event_key': event_key, 'partner': partner_name,
        })
        flash(f'College gear sharing set: {comp.name} + {partner_name}.', 'success')
    else:
        flash('No partner name — use Remove to delete the entry.', 'warning')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/college/gear-sharing/remove', methods=['POST'])
def college_gear_remove(tournament_id):
    """Remove a gear-sharing entry from a college competitor."""
    Tournament.query.get_or_404(tournament_id)
    from models.competitor import CollegeCompetitor
    try:
        competitor_id = int(request.form.get('competitor_id', ''))
    except (TypeError, ValueError):
        flash('Invalid competitor ID.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    comp = CollegeCompetitor.query.get_or_404(competitor_id)
    if comp.tournament_id != tournament_id:
        flash('Competitor not found.', 'error')
        return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))

    event_key = (request.form.get('event_key') or '').strip()
    gear = comp.get_gear_sharing()
    if event_key in gear:
        del gear[event_key]
        comp.gear_sharing = json.dumps(gear)
        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('college_gear_removed', 'college_competitor', competitor_id, {'event_key': event_key})
        flash(f'College gear sharing entry removed for {comp.name}.', 'info')
    else:
        flash('Entry not found.', 'warning')
    return redirect(url_for('registration.pro_gear_manager', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro/gear-sharing/print')
def pro_gear_print(tournament_id):
    """Printable gear-sharing report grouped by equipment category."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.gear_sharing import build_gear_report, get_gear_groups
    report = build_gear_report(tournament)
    gear_groups = get_gear_groups(tournament)
    pro_events = Event.query.filter_by(
        tournament_id=tournament_id, event_type='pro'
    ).order_by(Event.name, Event.gender).all()
    return render_template(
        'pro/gear_sharing_print.html',
        tournament=tournament,
        report=report,
        gear_groups=gear_groups,
        pro_events=pro_events,
    )


@registration_bp.route('/<int:tournament_id>/pro/<int:competitor_id>/scratch', methods=['POST'])
def scratch_pro_competitor(tournament_id, competitor_id):
    """Scratch a professional competitor."""
    tournament = Tournament.query.get_or_404(tournament_id)
    competitor = ProCompetitor.query.get_or_404(competitor_id)
    if competitor.tournament_id != tournament_id:
        flash('Competitor not found in this tournament.', 'error')
        return redirect(url_for('registration.pro_registration', tournament_id=tournament_id))
    competitor.status = 'scratched'
    # Remove gear-sharing entries on active competitors that reference this person.
    from services.gear_sharing import cleanup_scratched_gear_entries
    result = cleanup_scratched_gear_entries(tournament, scratched_competitor=competitor)
    db.session.commit()
    invalidate_tournament_caches(tournament_id)

    msg = text.FLASH['competitor_scratched'].format(name=competitor.name)
    if result['cleaned']:
        msg += f' Removed {result["cleaned"]} gear-sharing reference(s) from {len(result["affected"])} competitor(s).'
    flash(msg, 'warning')
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
