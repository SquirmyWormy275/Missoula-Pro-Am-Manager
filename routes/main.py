"""
Main routes for dashboard and navigation.
"""
import json
import logging
import time
from urllib.parse import urlsplit

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import text as sql_text

import strings as text
from config import TournamentStatus
from database import db
from models import Event, Flight, Heat, HeatAssignment, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.audit import log_action

try:
    from flask_login import current_user
except ModuleNotFoundError:  # pragma: no cover - fallback for stripped environments
    class _AnonymousCurrentUser:
        is_authenticated = False
        is_judge = False
        is_admin = False

    current_user = _AnonymousCurrentUser()

logger = logging.getLogger(__name__)

main_bp = Blueprint('main', __name__)


def _date_value(value):
    """Return an ISO date string for audit payloads."""
    if value is None:
        return None
    return value.isoformat()


@main_bp.route('/health')
def health():
    """Health check — returns DB connectivity, migration status, version. No auth, no CSRF."""
    db_ok = False
    migration_current = False
    migration_head = None
    migration_current_rev = None
    try:
        db.session.execute(sql_text('SELECT 1'))
        db_ok = True
        # Check if DB is at migration HEAD
        try:
            import os

            from alembic.config import Config as AlembicConfig
            from alembic.script import ScriptDirectory
            from flask_migrate import current as alembic_current
            migration_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'migrations')
            alembic_cfg = AlembicConfig()
            alembic_cfg.set_main_option('script_location', migration_dir)
            script = ScriptDirectory.from_config(alembic_cfg)
            head = script.get_current_head()
            migration_head = head

            row = db.session.execute(sql_text('SELECT version_num FROM alembic_version LIMIT 1')).fetchone()
            if row:
                migration_current_rev = row[0]
                migration_current = (row[0] == head)
        except Exception:
            pass
    except Exception:
        pass
    return jsonify({
        'status': 'ok' if db_ok and migration_current else 'degraded',
        'db': db_ok,
        'migration_current': migration_current,
        'migration_head': migration_head,
        'migration_rev': migration_current_rev,
        'version': '2.14.9',
    })


@main_bp.route('/health/diag')
def health_diag():
    """Diagnostic endpoint — exposes the full runtime config state.

    Designed so the operator can curl this from anywhere and see exactly
    what config the deployed app booted with, without needing access to
    Railway log scrollback. Surfaces:

      - Which Config class was selected (Development vs Production)
      - Whether validate_runtime() recorded any soft warnings
      - DB dialect (sqlite vs postgresql) — confirms postgres on prod
      - Whether SECRET_KEY is strong enough
      - Whether STRATHMARK env vars are set
      - Whether Railway env vars are detected
      - CSP nonce + HSTS state from current request
      - Migration head + current revision

    No auth required — the response contains no secrets, only boolean
    yes/no flags. The actual SECRET_KEY value is never exposed.

    Use this to debug "why is HSTS missing on prod" or "why isn't
    STRATHMARK running" without needing Railway dashboard access.
    """
    import os as _os

    from flask import current_app
    from flask import request as _request

    cfg = current_app.config

    # SECRET_KEY strength check (mirrors validate_runtime logic but never raises).
    secret = cfg.get('SECRET_KEY', '') or ''
    weak_values = {'changeme', 'secret', 'default'}
    secret_strong = bool(
        secret
        and len(secret) >= 16
        and secret.lower() not in weak_values
    )

    db_uri = cfg.get('SQLALCHEMY_DATABASE_URI', '') or ''
    db_dialect = (
        'postgresql' if db_uri.startswith('postgresql://')
        else 'sqlite' if db_uri.startswith('sqlite')
        else 'unknown'
    )

    return jsonify({
        'env_name': cfg.get('ENV_NAME', 'unknown'),
        'production_warnings': cfg.get('_PRODUCTION_WARNINGS', []),
        'config': {
            'secret_key_strong': secret_strong,
            'secret_key_length': len(secret),
            'db_dialect': db_dialect,
            'session_cookie_secure': bool(cfg.get('SESSION_COOKIE_SECURE')),
            'session_cookie_httponly': bool(cfg.get('SESSION_COOKIE_HTTPONLY')),
            'session_cookie_samesite': cfg.get('SESSION_COOKIE_SAMESITE'),
        },
        'integrations': {
            'strathmark_supabase_url_set': bool(_os.environ.get('STRATHMARK_SUPABASE_URL')),
            'strathmark_supabase_key_set': bool(_os.environ.get('STRATHMARK_SUPABASE_KEY')),
            'twilio_configured': bool(cfg.get('TWILIO_ACCOUNT_SID') and cfg.get('TWILIO_AUTH_TOKEN')),
            's3_backup_configured': bool(cfg.get('BACKUP_S3_BUCKET') and cfg.get('AWS_ACCESS_KEY_ID')),
            'sentry_dsn_set': bool(cfg.get('SENTRY_DSN')),
        },
        'runtime': {
            'railway_environment': _os.environ.get('RAILWAY_ENVIRONMENT', ''),
            'railway_environment_name': _os.environ.get('RAILWAY_ENVIRONMENT_NAME', ''),
            'flask_env': _os.environ.get('FLASK_ENV', ''),
            'production_env_var': _os.environ.get('PRODUCTION', ''),
            'has_csp_nonce': bool(getattr(_request, '_csp_nonce_seen', None) is None
                                  or hasattr(_request, 'csp_nonce')),
        },
        'security_headers': {
            # These are what the response will carry — re-derived not from g
            # but from what set_security_headers() writes. The response of
            # this very endpoint will have them, so just confirming the
            # response context is correct.
            'hsts_will_be_set': cfg.get('ENV_NAME') == 'production',
            'csp_will_be_set': True,
        },
        'version': '2.14.9',
    })


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
        Tournament.status.in_(TournamentStatus.ACTIVE_STATUSES)
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
        Tournament.status.in_(TournamentStatus.ACTIVE_STATUSES)
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
        try:
            year = int(request.form.get('year', 2026))
        except (TypeError, ValueError):
            flash('Invalid year value. Please enter a four-digit year.', 'error')
            return render_template('tournament_new.html')

        tournament = Tournament(
            name=name,
            year=year,
            status='setup'
        )
        db.session.add(tournament)
        db.session.flush()
        log_action('tournament_created', 'tournament', tournament.id, {
            'tournament_id': tournament.id,
            'name': tournament.name,
            'year': tournament.year,
            'status': tournament.status,
        })
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
    active_tab = request.args.get('tab', 'payouts')

    # Events tab data — helpers live in scheduling.py
    import config as app_config
    from routes.scheduling import _get_existing_event_config, _with_field_key
    college_open_events = [_with_field_key(e) for e in app_config.COLLEGE_OPEN_EVENTS]
    college_closed_events = [_with_field_key(e) for e in app_config.COLLEGE_CLOSED_EVENTS]
    pro_events = [_with_field_key(e) for e in app_config.PRO_EVENTS]
    existing_config = _get_existing_event_config(tournament)

    # Prize money tab data
    from models.payout_template import PayoutTemplate
    from services import scoring_engine as engine
    pro_events_payout = (Event.query
                         .filter_by(tournament_id=tournament_id, event_type='pro')
                         .order_by(Event.name)
                         .all())
    payout_templates = engine.list_payout_templates()
    payout_summaries = []
    total_purse = 0.0
    configured_count = 0
    for ev in pro_events_payout:
        payouts = ev.get_payouts()
        purse = sum(float(v) for v in payouts.values()) if payouts else 0.0
        places_paid = len([v for v in payouts.values() if float(v) > 0]) if payouts else 0
        first_place = float(payouts.get('1', 0)) if payouts else 0.0
        total_purse += purse
        if purse > 0:
            configured_count += 1
        payout_summaries.append({
            'event': ev,
            'payouts': payouts,
            'purse': purse,
            'places_paid': places_paid,
            'first_place': first_place,
        })

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
        # payouts
        payout_summaries=payout_summaries,
        payout_templates=payout_templates,
        total_purse=total_purse,
        configured_count=configured_count,
        total_payout_events=len(pro_events_payout),
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

    log_action('tournament_settings_updated', 'tournament', tournament.id, {
        'tournament_id': tournament.id,
        'name': tournament.name,
        'year': tournament.year,
        'college_date': _date_value(tournament.college_date),
        'pro_date': _date_value(tournament.pro_date),
        'friday_feature_date': _date_value(tournament.friday_feature_date),
        'providing_shirts': tournament.providing_shirts,
    })
    db.session.commit()
    flash('Tournament settings saved.', 'success')
    return redirect(url_for('main.tournament_setup', tournament_id=tournament_id, tab='settings'))


@main_bp.route('/tournament/<int:tournament_id>/activate/<competition_type>', methods=['POST'])
def activate_competition(tournament_id, competition_type):
    """Activate college or pro competition for a tournament."""
    tournament = Tournament.query.get_or_404(tournament_id)

    if competition_type == 'college':
        tournament.status = TournamentStatus.COLLEGE_ACTIVE
        log_action('competition_activated', 'tournament', tournament.id, {
            'tournament_id': tournament.id,
            'competition_type': competition_type,
            'status': tournament.status,
        })
        flash(text.FLASH['college_active'], 'success')
    elif competition_type == 'pro':
        tournament.status = TournamentStatus.PRO_ACTIVE
        log_action('competition_activated', 'tournament', tournament.id, {
            'tournament_id': tournament.id,
            'competition_type': competition_type,
            'status': tournament.status,
        })
        flash(text.FLASH['pro_active'], 'success')
    else:
        log_action('competition_activation_rejected', 'tournament', tournament.id, {
            'tournament_id': tournament.id,
            'competition_type': competition_type,
            'reason': 'invalid_competition_type',
        })
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
        log_action('tournament_delete_denied', 'tournament', tournament.id, {
            'tournament_id': tournament.id,
            'name': tournament_name,
            'reason': 'confirmation_mismatch',
        })
        db.session.commit()
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

        log_action('tournament_deleted', 'tournament', tournament.id, {
            'tournament_id': tournament.id,
            'name': tournament_name,
        })
        db.session.delete(tournament)
        db.session.commit()
        flash(f'Deleted tournament: {tournament_name}', 'success')
    except Exception as exc:
        db.session.rollback()
        log_action('tournament_delete_failed', 'tournament', tournament_id, {
            'tournament_id': tournament_id,
            'name': tournament_name,
            'error': str(exc),
        })
        db.session.commit()
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

    # Copy teams. school_abbreviation is NOT NULL on Team — must be copied.
    from models.team import Team
    team_id_map = {}
    for team in source.teams.all():
        new_team = Team(
            tournament_id=new_tournament.id,
            school_name=team.school_name,
            school_abbreviation=team.school_abbreviation,
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

    # Copy events (no heats or results). Several event types store workflow
    # state in the `payouts` JSON field rather than payout amounts — copying
    # that state across tournaments drags stale pairs / bracket rows / relay
    # teams along with competitor IDs from the source tournament that don't
    # exist in the clone. Reset those to empty state during clone.
    _STATEFUL_EVENT_NAMES = {'Partnered Axe Throw', 'Pro-Am Relay'}
    for event in source.events.all():
        is_state_event = (
            event.name in _STATEFUL_EVENT_NAMES
            or (event.stand_type or '').lower() == 'birling'
        )
        new_event = Event(
            tournament_id=new_tournament.id,
            name=event.name,
            event_type=event.event_type,
            gender=event.gender,
            scoring_type=event.scoring_type,
            scoring_order=event.scoring_order,
            is_open=event.is_open,
            is_handicap=event.is_handicap,
            is_partnered=event.is_partnered,
            partner_gender_requirement=event.partner_gender_requirement,
            requires_dual_runs=event.requires_dual_runs,
            requires_triple_runs=event.requires_triple_runs,
            stand_type=event.stand_type,
            max_stands=event.max_stands,
            has_prelims=event.has_prelims,
            payouts='{}' if is_state_event else event.payouts,
            status='pending',
            is_finalized=False,
        )
        db.session.add(new_event)

    log_action('tournament_cloned', 'tournament', new_tournament.id, {
        'tournament_id': new_tournament.id,
        'source_id': source.id,
        'source_name': source.name,
    })
    db.session.commit()
    flash(f'Tournament cloned as "{new_tournament.name}". Update the name and dates before use.', 'success')
    return redirect(url_for('main.tournament_detail', tournament_id=new_tournament.id))


# ---------------------------------------------------------------------------
# Race-Day Operations Dashboard (Unit 12 capstone)
# ---------------------------------------------------------------------------

@main_bp.route('/tournament/<int:tid>/ops-dashboard')
def ops_dashboard(tid):
    """GET: Race-day operations dashboard — single-page mission control.

    Read-only.  Requires judge role (main is in MANAGEMENT_BLUEPRINTS).
    Auto-refreshes every 30 seconds via JS.
    """
    tournament = Tournament.query.get_or_404(tid)

    from models.audit_log import AuditLog
    from models.event import EventResult

    # ------------------------------------------------------------------
    # Section 1: Live scratch feed — last 20 competitor_scratched entries
    # ------------------------------------------------------------------
    # Filter to this tournament by checking details_json for the tournament_id.
    # AuditLog has no tournament_id column, so we filter in Python.
    _all_scratches = (
        AuditLog.query
        .filter_by(action='competitor_scratched')
        .order_by(AuditLog.created_at.desc())
        .limit(100)
        .all()
    )
    scratch_feed = []
    for entry in _all_scratches:
        try:
            details = json.loads(entry.details_json or '{}')
            if details.get('tournament_id') == tid:
                scratch_feed.append(entry)
                if len(scratch_feed) >= 20:
                    break
        except (json.JSONDecodeError, TypeError):
            continue

    # ------------------------------------------------------------------
    # Section 2: Relay team health
    # ------------------------------------------------------------------
    relay_event = Event.query.filter_by(
        tournament_id=tid, name='Pro-Am Relay'
    ).first()
    team_health = []
    if relay_event:
        try:
            import json as _json

            from services.proam_relay import compute_team_health
            raw = relay_event.event_state or relay_event.payouts or '{}'
            relay_data = _json.loads(raw)
            for team in relay_data.get('teams', []):
                health = compute_team_health(team, tournament)
                team_health.append({'team': team, 'health': health})
        except Exception:
            logger.warning('ops_dashboard: relay health computation failed', exc_info=True)

    # ------------------------------------------------------------------
    # Section 3: Standings integrity warnings
    # ------------------------------------------------------------------
    integrity_warnings = []
    # Unfinalized events that have at least one completed result
    all_events = tournament.events.all()
    for ev in all_events:
        if not ev.is_finalized:
            has_completed = ev.results.filter_by(status='completed').first() is not None
            if has_completed:
                integrity_warnings.append({
                    'type': 'unfinalized',
                    'message': f'{ev.display_name} has completed results but is not finalized',
                    'event_id': ev.id,
                })

    # Scratched competitors that still have completed EventResults
    event_ids = [ev.id for ev in all_events]
    if event_ids:
        from models.competitor import CollegeCompetitor, ProCompetitor
        scratched_college_ids = [
            c.id for c in tournament.college_competitors.filter_by(status='scratched').all()
        ]
        scratched_pro_ids = [
            c.id for c in tournament.pro_competitors.filter_by(status='scratched').all()
        ]
        if scratched_college_ids:
            ghost_count = (
                EventResult.query
                .filter(
                    EventResult.event_id.in_(event_ids),
                    EventResult.competitor_type == 'college',
                    EventResult.competitor_id.in_(scratched_college_ids),
                    EventResult.status == 'completed',
                )
                .count()
            )
            if ghost_count:
                integrity_warnings.append({
                    'type': 'scratched_has_results',
                    'message': (
                        f'{ghost_count} completed result(s) belong to scratched college competitor(s)'
                    ),
                })
        if scratched_pro_ids:
            ghost_count = (
                EventResult.query
                .filter(
                    EventResult.event_id.in_(event_ids),
                    EventResult.competitor_type == 'pro',
                    EventResult.competitor_id.in_(scratched_pro_ids),
                    EventResult.status == 'completed',
                )
                .count()
            )
            if ghost_count:
                integrity_warnings.append({
                    'type': 'scratched_has_results',
                    'message': (
                        f'{ghost_count} completed result(s) belong to scratched pro competitor(s)'
                    ),
                })

    # ------------------------------------------------------------------
    # Section 4: Payout status — pro events only
    # ------------------------------------------------------------------
    pro_event_ids = [ev.id for ev in all_events if ev.event_type == 'pro']
    total_purse = 0.0
    total_settled = 0.0
    if pro_event_ids:
        from sqlalchemy import case, func
        row = (
            EventResult.query
            .filter(EventResult.event_id.in_(pro_event_ids))
            .with_entities(
                func.coalesce(func.sum(EventResult.payout_amount), 0.0).label('total'),
                func.coalesce(
                    func.sum(
                        case(
                            (EventResult.payout_settled == True, EventResult.payout_amount),  # noqa: E712
                            else_=0.0,
                        )
                    ),
                    0.0,
                ).label('settled'),
            )
            .first()
        )
        if row:
            total_purse = float(row.total or 0.0)
            total_settled = float(row.settled or 0.0)
    payout_outstanding = total_purse - total_settled
    payout_pct = int(total_settled / total_purse * 100) if total_purse > 0 else 0
    payout_summary = {
        'total_purse': total_purse,
        'total_settled': total_settled,
        'outstanding': payout_outstanding,
        'pct': payout_pct,
    }

    # ------------------------------------------------------------------
    # Section 5: Event finalization strip — all events with status info
    # ------------------------------------------------------------------
    events = all_events

    # ------------------------------------------------------------------
    # Section 6: Async jobs — recent background work touching this tournament
    # ------------------------------------------------------------------
    from services.background_jobs import list_recent as list_recent_jobs

    recent_jobs = [
        job
        for job in list_recent_jobs(limit=30)
        if int((job.get('metadata') or {}).get('tournament_id', -1)) == tid
    ]
    job_summary = {
        'queued': sum(1 for job in recent_jobs if job['status'] == 'queued'),
        'running': sum(1 for job in recent_jobs if job['status'] == 'running'),
        'failed': sum(1 for job in recent_jobs if job['status'] == 'failed'),
    }

    return render_template(
        'ops_dashboard.html',
        tournament=tournament,
        scratch_feed=scratch_feed,
        team_health=team_health,
        relay_event=relay_event,
        integrity_warnings=integrity_warnings,
        payout_summary=payout_summary,
        events=events,
        recent_jobs=recent_jobs,
        job_summary=job_summary,
    )


@main_bp.route('/tournament/<int:tournament_id>/export-config')
def export_tournament_config(tournament_id):
    """Export the complete tournament configuration as a JSON file.

    Serializes event types, scoring rules, heat gen settings, payout templates,
    and schedule config. Seeds the modular platform — extract REAL config from a
    WORKING tournament instead of building abstract config from scratch.
    """
    tournament = Tournament.query.get_or_404(tournament_id)
    events = Event.query.filter_by(tournament_id=tournament_id).order_by(Event.name).all()

    event_configs = []
    for e in events:
        event_configs.append({
            'name': e.name,
            'event_type': e.event_type,
            'gender': e.gender,
            'scoring_type': e.scoring_type,
            'scoring_order': e.scoring_order,
            'is_open': e.is_open,
            'is_handicap': getattr(e, 'is_handicap', False),
            'is_partnered': e.is_partnered,
            'partner_gender_requirement': e.partner_gender_requirement,
            'requires_dual_runs': e.requires_dual_runs,
            'requires_triple_runs': getattr(e, 'requires_triple_runs', False),
            'stand_type': e.stand_type,
            'max_stands': e.max_stands,
            'has_prelims': e.has_prelims,
            'payouts': e.get_payouts(),
        })

    # Wood config
    from models.wood_config import WoodConfig
    wood_configs = WoodConfig.query.filter_by(tournament_id=tournament_id).all()
    wood_data = []
    for w in wood_configs:
        wood_data.append({
            'config_key': w.config_key,
            'species': w.species,
            'size_value': w.size_value,
            'size_unit': w.size_unit,
            'notes': w.notes,
            'count_override': w.count_override,
        })

    # Schedule config
    schedule_config = tournament.get_schedule_config() if hasattr(tournament, 'get_schedule_config') else {}

    export = {
        'export_version': '1.0',
        'exported_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source': 'Missoula Pro-Am Manager',
        'tournament': {
            'name': tournament.name,
            'year': tournament.year,
            'status': tournament.status,
            'providing_shirts': getattr(tournament, 'providing_shirts', False),
        },
        'events': event_configs,
        'wood_configs': wood_data,
        'schedule_config': schedule_config,
    }

    import json

    from flask import Response
    filename = f'tournament_config_{tournament.name.replace(" ", "_")}_{tournament.year}.json'
    return Response(
        json.dumps(export, indent=2, ensure_ascii=False),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )
