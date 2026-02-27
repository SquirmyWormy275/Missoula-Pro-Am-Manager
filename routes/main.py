"""
Main routes for dashboard and navigation.
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request
from urllib.parse import urlsplit
from database import db
from models import Tournament, Event, Heat, HeatAssignment, Flight
import strings as text
try:
    from flask_login import current_user
except ModuleNotFoundError:  # pragma: no cover - fallback for stripped environments
    class _AnonymousCurrentUser:
        is_authenticated = False
        is_judge = False
        is_admin = False

    current_user = _AnonymousCurrentUser()

main_bp = Blueprint('main', __name__)


def _can_access_arapaho_mode() -> bool:
    endpoint = request.endpoint or ''
    if endpoint.startswith('portal.') or endpoint == 'main.index':
        return False
    if not getattr(current_user, 'is_authenticated', False):
        return False
    return bool(getattr(current_user, 'is_judge', False) or getattr(current_user, 'is_admin', False))


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
    """Public entry page where users choose judge/competitor/spectator mode."""
    active_tournament = Tournament.query.filter(
        Tournament.status.in_(['setup', 'college_active', 'pro_active'])
    ).order_by(Tournament.year.desc()).first()
    tournaments = Tournament.query.order_by(Tournament.year.desc()).all()
    return render_template(
        'role_entry.html',
        active_tournament=active_tournament,
        tournaments=tournaments,
    )


@main_bp.route('/judge')
def judge_dashboard():
    """Judge dashboard - show active tournament or tournament selection."""
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
    if lang_code == 'arp' and not _can_access_arapaho_mode():
        text.set_language('en')
        flash(text.FLASH['arapaho_restricted'], 'warning')
    elif text.set_language(lang_code):
        flash(
            text.FLASH['language_changed'].format(language=text.get_language_name(lang_code)),
            'success'
        )
    else:
        flash(text.FLASH['invalid_language'], 'error')

    next_page = _safe_redirect_target(request.args.get('next'))
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
        'completed_events': tournament.events.filter_by(status='completed').count(),
        'heats_generated': Heat.query.join(Event).filter(Event.tournament_id == tournament_id).count(),
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


@main_bp.route('/tournament/<int:tournament_id>/delete', methods=['POST'])
def delete_tournament(tournament_id):
    """Delete a tournament from the dashboard list."""
    tournament = Tournament.query.get_or_404(tournament_id)
    tournament_name = f'{tournament.name} {tournament.year}'
    confirmation = request.form.get('confirm_delete', '').strip()

    if confirmation != 'DELETE':
        flash(f'Deletion cancelled for "{tournament_name}". Type DELETE to confirm.', 'warning')
        return redirect(url_for('main.judge_dashboard'))

    try:
        # Clear heat assignments that are not ORM-linked for cascade delete.
        event_ids = [eid for (eid,) in tournament.events.with_entities(Event.id).all()]
        if event_ids:
            heat_ids = [hid for (hid,) in Heat.query.filter(Heat.event_id.in_(event_ids)).with_entities(Heat.id).all()]
            if heat_ids:
                HeatAssignment.query.filter(HeatAssignment.heat_id.in_(heat_ids)).delete(synchronize_session=False)

        # Remove flight references before deleting flights.
        flight_ids = [fid for (fid,) in Flight.query.filter_by(tournament_id=tournament_id).with_entities(Flight.id).all()]
        if flight_ids:
            Heat.query.filter(Heat.flight_id.in_(flight_ids)).update(
                {Heat.flight_id: None},
                synchronize_session=False
            )
            Flight.query.filter(Flight.id.in_(flight_ids)).delete(synchronize_session=False)

        db.session.delete(tournament)
        db.session.commit()
        flash(f'Deleted tournament: {tournament_name}', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Could not delete tournament "{tournament_name}": {exc}', 'error')

    return redirect(url_for('main.judge_dashboard'))


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
    completed_events = tournament.events.filter_by(event_type='college', status='completed').all()
    live_event_leaders = []
    for event in completed_events:
        winner = event.results.filter_by(final_position=1).first()
        if winner:
            live_event_leaders.append({
                'event_name': event.display_name,
                'competitor': winner.competitor_name,
                'result': winner.result_value,
                'scoring_type': event.scoring_type,
            })
    live_event_leaders = live_event_leaders[:5]

    return render_template('college/dashboard.html',
                           tournament=tournament,
                           teams=teams,
                           events=events,
                           bull=bull,
                           belle=belle,
                           team_standings=team_standings,
                           live_event_leaders=live_event_leaders)


@main_bp.route('/tournament/<int:tournament_id>/pro')
def pro_dashboard(tournament_id):
    """Professional competition dashboard."""
    tournament = Tournament.query.get_or_404(tournament_id)

    competitors = tournament.pro_competitors.all()
    events = tournament.events.filter_by(event_type='pro').all()

    # Calculate fee summary
    total_fees = sum(c.total_fees_owed for c in competitors)
    collected_fees = sum(c.total_fees_paid for c in competitors)
    top_earners = sorted(competitors, key=lambda c: c.total_earnings, reverse=True)[:5]
    completed_events = tournament.events.filter_by(event_type='pro', status='completed').all()
    live_event_leaders = []
    for event in completed_events:
        winner = event.results.filter_by(final_position=1).first()
        if winner:
            live_event_leaders.append({
                'event_name': event.display_name,
                'competitor': winner.competitor_name,
                'result': winner.result_value,
                'scoring_type': event.scoring_type,
            })
    live_event_leaders = live_event_leaders[:5]

    return render_template('pro/dashboard.html',
                           tournament=tournament,
                           competitors=competitors,
                           events=events,
                           total_fees=total_fees,
                           collected_fees=collected_fees,
                           top_earners=top_earners,
                           live_event_leaders=live_event_leaders)
