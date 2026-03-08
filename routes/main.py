"""
Main routes for dashboard and navigation.
"""
import time
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from urllib.parse import urlsplit
from database import db
from models import Tournament, Event, Heat, HeatAssignment, Flight
from models.competitor import CollegeCompetitor, ProCompetitor
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
    if getattr(current_user, 'is_authenticated', False) and (
        getattr(current_user, 'is_judge', False) or getattr(current_user, 'is_admin', False)
    ):
        return redirect(url_for('main.judge_dashboard'))

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
    current_lang = text.get_language()
    now_ts = time.time()
    lock_until = session.get('arapaho_language_lock_until')
    if isinstance(lock_until, (int, float)) and now_ts >= lock_until:
        session.pop('arapaho_language_lock_until', None)
        lock_until = None

    if (
        current_lang == 'arp'
        and lang_code != 'arp'
        and isinstance(lock_until, (int, float))
        and now_ts < lock_until
    ):
        remaining = int(lock_until - now_ts)
        flash(f"Northern Arapaho mode is locked for {remaining} more seconds.", 'warning')
        text.set_language('arp')
        next_page = _safe_redirect_target(request.args.get('next'))
        return redirect(next_page or url_for('main.index'))

    if lang_code == 'arp' and not _can_access_arapaho_mode():
        text.set_language('en')
        flash(text.FLASH['arapaho_restricted'], 'warning')
    elif text.set_language(lang_code):
        if current_lang == 'en' and lang_code == 'arp':
            session['arapaho_language_lock_until'] = now_ts + 410
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

    from models.wood_config import WoodConfig
    # Get summary statistics
    flights_built = Flight.query.filter_by(tournament_id=tournament_id).count() > 0
    stats = {
        'college_teams': tournament.college_team_count,
        'college_competitors': tournament.college_competitor_count,
        'pro_competitors': tournament.pro_competitor_count,
        'events': tournament.events.count(),
        'completed_events': tournament.events.filter_by(status='completed').count(),
        'heats_generated': Heat.query.join(Event).filter(Event.tournament_id == tournament_id).count(),
        'wood_configured': WoodConfig.query.filter_by(tournament_id=tournament_id).first() is not None,
        'flights_built': flights_built,
    }

    return render_template('tournament_detail.html',
                           tournament=tournament,
                           stats=stats)


@main_bp.route('/tournament/<int:tournament_id>/setup', methods=['GET'])
def tournament_setup(tournament_id):
    """Consolidated setup page: events, wood specs, and tournament dates."""
    tournament = Tournament.query.get_or_404(tournament_id)
    active_tab = request.args.get('tab', 'events')

    # Events tab data — helpers live in scheduling.py
    from routes.scheduling import _with_field_key, _get_existing_event_config
    import config as app_config
    college_open_events = [_with_field_key(e) for e in app_config.COLLEGE_OPEN_EVENTS]
    college_closed_events = [_with_field_key(e) for e in app_config.COLLEGE_CLOSED_EVENTS]
    pro_events = [_with_field_key(e) for e in app_config.PRO_EVENTS]
    existing_config = _get_existing_event_config(tournament)

    # Wood specs tab data
    import services.woodboss as woodboss_svc
    configs = woodboss_svc._get_configs(tournament_id)
    block_rows = woodboss_svc.calculate_blocks(tournament_id, configs=configs)
    general_cfg = configs.get(woodboss_svc.LOG_GENERAL_KEY)
    stock_cfg = configs.get(woodboss_svc.LOG_STOCK_KEY)
    op_cfg = configs.get(woodboss_svc.LOG_OP_KEY)
    cookie_cfg = configs.get(woodboss_svc.LOG_COOKIE_KEY)
    all_tournaments = Tournament.query.order_by(Tournament.year.desc(), Tournament.name).all()
    other_tournaments = [t for t in all_tournaments if t.id != tournament_id]

    return render_template(
        'tournament_setup.html',
        tournament=tournament,
        active_tab=active_tab,
        # events
        college_open_events=college_open_events,
        college_closed_events=college_closed_events,
        pro_events=pro_events,
        existing_config=existing_config,
        stand_configs=app_config.STAND_CONFIGS,
        # wood
        block_rows=block_rows,
        general_cfg=general_cfg,
        stock_cfg=stock_cfg,
        op_cfg=op_cfg,
        cookie_cfg=cookie_cfg,
        configs=configs,
        other_tournaments=other_tournaments,
    )


@main_bp.route('/tournament/<int:tournament_id>/setup/settings', methods=['POST'])
def save_tournament_settings(tournament_id):
    """Save tournament name, year, and dates."""
    from datetime import date as date_type
    tournament = Tournament.query.get_or_404(tournament_id)

    name = request.form.get('name', '').strip()
    if name:
        tournament.name = name

    try:
        year_raw = request.form.get('year', '').strip()
        if year_raw:
            tournament.year = int(year_raw)
    except (ValueError, TypeError):
        flash('Invalid year value.', 'error')
        return redirect(url_for('main.tournament_setup', tournament_id=tournament_id, tab='settings'))

    for field in ('college_date', 'pro_date'):
        raw = request.form.get(field, '').strip()
        if raw:
            try:
                setattr(tournament, field, date_type.fromisoformat(raw))
            except ValueError:
                flash(f'Invalid date for {field.replace("_", " ")}.', 'error')
                return redirect(url_for('main.tournament_setup', tournament_id=tournament_id, tab='settings'))
        else:
            setattr(tournament, field, None)

    # Friday Night Feature always occurs on the same day as the college day
    tournament.friday_feature_date = tournament.college_date

    # Shirt logistics checkbox — present in form means True
    tournament.providing_shirts = bool(request.form.get('providing_shirts'))

    db.session.commit()
    flash('Tournament settings saved.', 'success')
    return redirect(url_for('main.tournament_setup', tournament_id=tournament_id, tab='settings'))


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


# ---------------------------------------------------------------------------
# #10 — Tournament clone
# ---------------------------------------------------------------------------

@main_bp.route('/tournament/<int:tournament_id>/clone', methods=['POST'])
def clone_tournament(tournament_id):
    """Clone a tournament: copy events (no heats/results) and all competitor/team records."""
    source = Tournament.query.get_or_404(tournament_id)

    # Create new tournament
    new_tournament = Tournament(
        name=f'Copy of {source.name}',
        year=source.year,
        status='setup',
    )
    db.session.add(new_tournament)
    db.session.flush()

    # Copy teams
    from models.team import Team
    team_id_map = {}
    for team in source.teams.all():
        new_team = Team(
            tournament_id=new_tournament.id,
            school_name=team.school_name,
            team_code=team.team_code,
            total_points=0,
        )
        db.session.add(new_team)
        db.session.flush()
        team_id_map[team.id] = new_team.id

    # Copy college competitors (reset earned data)
    for comp in source.college_competitors.all():
        new_comp = CollegeCompetitor(
            tournament_id=new_tournament.id,
            team_id=team_id_map.get(comp.team_id),
            name=comp.name,
            gender=comp.gender,
            individual_points=0,
            events_entered='[]',
            partners='{}',
            gear_sharing='{}',
            portal_pin_hash=None,
            status='active',
        )
        db.session.add(new_comp)

    # Copy pro competitors (reset earned data)
    for comp in source.pro_competitors.all():
        new_comp = ProCompetitor(
            tournament_id=new_tournament.id,
            name=comp.name,
            gender=comp.gender,
            address=comp.address,
            phone=comp.phone,
            email=comp.email,
            shirt_size=comp.shirt_size,
            is_ala_member=comp.is_ala_member,
            is_left_handed_springboard=comp.is_left_handed_springboard,
            events_entered='[]',
            entry_fees='{}',
            fees_paid='{}',
            gear_sharing='{}',
            partners='{}',
            total_earnings=0.0,
            portal_pin_hash=None,
            status='active',
        )
        db.session.add(new_comp)

    # Copy events (no heats or results)
    for event in source.events.all():
        new_event = Event(
            tournament_id=new_tournament.id,
            name=event.name,
            event_type=event.event_type,
            gender=event.gender,
            scoring_type=event.scoring_type,
            scoring_order=event.scoring_order,
            is_open=event.is_open,
            is_partnered=event.is_partnered,
            partner_gender_requirement=event.partner_gender_requirement,
            requires_dual_runs=event.requires_dual_runs,
            stand_type=event.stand_type,
            max_stands=event.max_stands,
            has_prelims=event.has_prelims,
            payouts=event.payouts,
            status='pending',
        )
        db.session.add(new_event)

    db.session.commit()
    from services.audit import log_action
    log_action('tournament_cloned', 'tournament', new_tournament.id, {
        'source_id': source.id,
        'source_name': source.name,
    })
    flash(f'Tournament cloned as "{new_tournament.name}". Update the name and dates before use.', 'success')
    return redirect(url_for('main.tournament_detail', tournament_id=new_tournament.id))
