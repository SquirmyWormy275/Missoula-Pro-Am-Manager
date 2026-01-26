"""
Registration routes for uploading and managing competitor entries.
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from werkzeug.utils import secure_filename
import os
from database import db
from models import Tournament, Team, CollegeCompetitor, ProCompetitor

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
        flash('No file selected.', 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    file = request.files['file']

    if file.filename == '':
        flash('No file selected.', 'error')
        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        # Import the Excel processing service
        from services.excel_io import process_college_entry_form

        try:
            result = process_college_entry_form(filepath, tournament)
            flash(f'Successfully imported {result["teams"]} team(s) with {result["competitors"]} competitor(s).', 'success')
        except Exception as e:
            flash(f'Error processing file: {str(e)}', 'error')

        return redirect(url_for('registration.college_registration', tournament_id=tournament_id))

    flash('Invalid file type. Please upload an Excel file (.xlsx or .xls).', 'error')
    return redirect(url_for('registration.college_registration', tournament_id=tournament_id))


@registration_bp.route('/<int:tournament_id>/college/team/<int:team_id>')
def team_detail(tournament_id, team_id):
    """View and edit team details."""
    tournament = Tournament.query.get_or_404(tournament_id)
    team = Team.query.get_or_404(team_id)

    return render_template('college/team_detail.html',
                           tournament=tournament,
                           team=team)


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

        flash(f'Competitor "{competitor.name}" added successfully!', 'success')
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

    flash(f'Competitor "{competitor.name}" has been scratched.', 'warning')
    return redirect(url_for('registration.pro_registration', tournament_id=tournament_id))
