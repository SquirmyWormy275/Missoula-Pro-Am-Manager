"""
Main routes for dashboard and navigation.
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request
from urllib.parse import urlsplit
from database import db
from models import Tournament
import strings as text

main_bp = Blueprint('main', __name__)


def _safe_redirect_target(target: str | None):
    """Only allow local relative redirects."""
    if not target:
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    if not target.startswith('/'):
        return None
    return target


@main_bp.route('/')
def index():
    """Main dashboard - show active tournament or tournament selection."""
    tournaments = Tournament.query.order_by(Tournament.year.desc()).all()
    active_tournament = Tournament.query.filter(
        Tournament.status.in_(['setup', 'college_active', 'pro_active'])
    ).first()

    return render_template('dashboard.html',
                           tournaments=tournaments,
                           active_tournament=active_tournament)


@main_bp.route('/language/<lang_code>')
def set_language(lang_code):
    """Update UI language and return user to the previous page."""
    if text.set_language(lang_code):
        flash(
            text.FLASH['language_changed'].format(language=text.get_language_name(lang_code)),
            'success'
        )
    else:
        flash(text.FLASH['invalid_language'], 'error')

    next_page = _safe_redirect_target(request.args.get('next'))
    if not next_page:
        next_page = _safe_redirect_target(request.referrer)
    return redirect(next_page or url_for('main.index'))


@main_bp.route('/tournament/new', methods=['GET', 'POST'])
def new_tournament():
    """Create a new tournament."""
    if request.method == 'POST':
        name = request.form.get('name', 'Missoula Pro Am')
        year = request.form.get('year', 2026)

        tournament = Tournament(
            name=name,
            year=int(year),
            status='setup'
        )
        db.session.add(tournament)
        db.session.commit()

        flash(text.FLASH['tournament_created'].format(name=name, year=year), 'success')
        return redirect(url_for('main.tournament_detail', tournament_id=tournament.id))

    return render_template('tournament_new.html')


@main_bp.route('/tournament/<int:tournament_id>')
def tournament_detail(tournament_id):
    """Tournament detail and management page."""
    tournament = Tournament.query.get_or_404(tournament_id)

    # Get summary statistics
    stats = {
        'college_teams': tournament.college_team_count,
        'college_competitors': tournament.college_competitor_count,
        'pro_competitors': tournament.pro_competitor_count,
        'events': tournament.events.count(),
        'completed_events': tournament.events.filter_by(status='completed').count()
    }

    return render_template('tournament_detail.html',
                           tournament=tournament,
                           stats=stats)


@main_bp.route('/tournament/<int:tournament_id>/activate/<competition_type>')
def activate_competition(tournament_id, competition_type):
    """Activate college or pro competition for a tournament."""
    tournament = Tournament.query.get_or_404(tournament_id)

    if competition_type == 'college':
        tournament.status = 'college_active'
        flash(text.FLASH['college_active'], 'success')
    elif competition_type == 'pro':
        tournament.status = 'pro_active'
        flash(text.FLASH['pro_active'], 'success')
    else:
        flash(text.FLASH['invalid_comp_type'], 'error')

    db.session.commit()
    return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))


@main_bp.route('/tournament/<int:tournament_id>/college')
def college_dashboard(tournament_id):
    """College competition dashboard."""
    tournament = Tournament.query.get_or_404(tournament_id)

    teams = tournament.teams.all()
    events = tournament.events.filter_by(event_type='college').all()

    # Get top performers
    bull = tournament.get_bull_of_woods(5)
    belle = tournament.get_belle_of_woods(5)
    team_standings = tournament.get_team_standings()[:5]

    return render_template('college/dashboard.html',
                           tournament=tournament,
                           teams=teams,
                           events=events,
                           bull=bull,
                           belle=belle,
                           team_standings=team_standings)


@main_bp.route('/tournament/<int:tournament_id>/pro')
def pro_dashboard(tournament_id):
    """Professional competition dashboard."""
    tournament = Tournament.query.get_or_404(tournament_id)

    competitors = tournament.pro_competitors.all()
    events = tournament.events.filter_by(event_type='pro').all()

    # Calculate fee summary
    total_fees = sum(c.total_fees_owed for c in competitors)
    collected_fees = sum(c.total_fees_paid for c in competitors)

    return render_template('pro/dashboard.html',
                           tournament=tournament,
                           competitors=competitors,
                           events=events,
                           total_fees=total_fees,
                           collected_fees=collected_fees)
