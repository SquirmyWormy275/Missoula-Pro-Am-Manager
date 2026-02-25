"""
Registration routes for uploading and managing competitor entries.
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from werkzeug.utils import secure_filename
import os
import json
from database import db
from models import Tournament, Team, CollegeCompetitor, ProCompetitor, Event, EventResult, Heat
import strings as text

registration_bp = Blueprint('registration', __name__)

ALLOWED_EXTENSIONS = {'xlsx', 'xls'}


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@registration_bp.route('/<int:tournament_id>/college')
def college_registration(tournament_id):
    """College team registration page."""
    tournament = Tournament.query.get_or_404(tournament_id)
    teams = tournament.teams.all()

    return render_template('college/registration.html',
                           tournament=tournament,
                           teams=teams)


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
        filename = secure_filename(file.filename)
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        # Import the Excel processing service
        from services.excel_io import process_college_entry_form

        try:
            result = process_college_entry_form(filepath, tournament)
            flash(text.FLASH['import_success'].format(teams=result["teams"], competitors=result["competitors"]), 'success')
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

    flash(f'Team "{team_code}" and {len(members)} competitor(s) deleted.', 'warning')
    return redirect(url_for('registration.college_registration', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/pro')
def pro_registration(tournament_id):
    """Professional competitor registration page."""
    tournament = Tournament.query.get_or_404(tournament_id)
    competitors = tournament.pro_competitors.all()

    return render_template('pro/registration.html',
                           tournament=tournament,
                           competitors=competitors)


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
            is_left_handed_springboard=request.form.get('left_handed') == 'on'
        )

        db.session.add(competitor)
        db.session.commit()

        flash(text.FLASH['competitor_added'].format(name=competitor.name), 'success')
        return redirect(url_for('registration.pro_registration', tournament_id=tournament_id))

    return render_template('pro/new_competitor.html', tournament=tournament)


@registration_bp.route('/<int:tournament_id>/pro/<int:competitor_id>')
def pro_competitor_detail(tournament_id, competitor_id):
    """View and edit professional competitor details."""
    tournament = Tournament.query.get_or_404(tournament_id)
    competitor = ProCompetitor.query.get_or_404(competitor_id)

    return render_template('pro/competitor_detail.html',
                           tournament=tournament,
                           competitor=competitor)


@registration_bp.route('/<int:tournament_id>/pro/<int:competitor_id>/scratch', methods=['POST'])
def scratch_pro_competitor(tournament_id, competitor_id):
    """Scratch a professional competitor."""
    competitor = ProCompetitor.query.get_or_404(competitor_id)
    competitor.status = 'scratched'
    db.session.commit()

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
