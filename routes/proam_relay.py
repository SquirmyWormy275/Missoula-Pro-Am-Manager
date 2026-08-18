"""
Routes for Pro-Am Relay lottery and management.
"""
import hashlib
import hmac
import json
from functools import wraps

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.orm.exc import StaleDataError

from database import db
from models import Tournament
from models.event import Event
from models.relay import RelayTeam
from services.audit import log_action
from services.cache_invalidation import invalidate_tournament_caches
from services.flight_builder import (
    lock_tournament_schedule,
    serialize_sqlite_schedule_writer,
)
from services.proam_relay import (
    RelayStateConflict,
    compute_team_health,
    create_proam_relay_event,
    get_proam_relay,
    relay_payout_summary,
)

# Shared message for the concurrent-edit case — surfaced when SQLAlchemy
# version_id detects a parallel update has advanced the row. Without this
# arm, race-day operators got an opaque 500 instead of a "reload" hint.
_STALE_DATA_FLASH = (
    'Another judge updated this in parallel. Reload the page and try again — '
    'your last input was not saved.'
)
_RELAY_STATE_FLASH = (
    'Relay state changed since this page was loaded. Refresh and review the '
    'current relay before saving again.'
)
_RELAY_PAYOUT_FLASH = (
    'Relay payouts or results changed since this page was loaded. '
    'Refresh and review the current amounts before saving again.'
)

bp = Blueprint('proam_relay', __name__, url_prefix='/tournament/<int:tournament_id>/proam-relay')


def _relay_conflict_response():
    db.session.rollback()
    return _RELAY_STATE_FLASH, 409


def _require_relay_digest(func):
    """Reject tokenless relay writes before validation or lock acquisition."""
    @wraps(func)
    def guarded(*args, **kwargs):
        if request.method == 'POST':
            expected_digest = request.form.get('expected_relay_digest', '').strip()
            if not expected_digest:
                return _relay_conflict_response()
        return func(*args, **kwargs)

    return guarded


def _relay_payout_state_digest(tournament, relay_event) -> str:
    payload = {
        'event_id': relay_event.id,
        'payouts': relay_event.get_payouts(),
        'relay_state': get_proam_relay(tournament).state_digest(),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def _relay_payout_conflict_response():
    db.session.rollback()
    return _RELAY_PAYOUT_FLASH, 409


@bp.route('/')
def relay_dashboard(tournament_id):
    """Pro-Am Relay dashboard."""
    tournament = db.get_or_404(Tournament, tournament_id)
    relay = get_proam_relay(tournament)

    teams = relay.get_teams()
    team_health = {
        t['team_number']: compute_team_health(t, tournament)
        for t in teams
    }

    return render_template('proam_relay/dashboard.html',
                         tournament=tournament,
                         relay=relay,
                         status=relay.get_status(),
                         teams=teams,
                         relay_state_digest=relay.state_digest(),
                         team_health=team_health,
                         capacity=relay.get_lottery_capacity(),
                         eligible_pro=relay.get_eligible_pro_competitors(),
                         eligible_college=relay.get_eligible_college_competitors())


@bp.route('/draw', methods=['POST'])
@_require_relay_digest
@serialize_sqlite_schedule_writer
def draw_lottery(tournament_id):
    """Run the Pro-Am Relay lottery."""
    tournament = lock_tournament_schedule(tournament_id)
    relay = get_proam_relay(tournament)

    try:
        num_teams = int(request.form.get('num_teams', 2))
        if num_teams < 1:
            raise ValueError('num_teams must be at least 1')
    except (TypeError, ValueError):
        flash('Invalid number of teams.', 'error')
        return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))

    try:
        result = relay.run_lottery(
            num_teams=num_teams,
            expected_digest=request.form.get('expected_relay_digest'),
        )
        invalidate_tournament_caches(tournament_id)
        flash(result['message'], 'success')
    except RelayStateConflict:
        return _relay_conflict_response()
    except StaleDataError:
        db.session.rollback()
        flash(_STALE_DATA_FLASH, 'warning')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))


@bp.route('/redraw', methods=['POST'])
@_require_relay_digest
@serialize_sqlite_schedule_writer
def redraw_lottery(tournament_id):
    """Clear and redraw the lottery."""
    tournament = lock_tournament_schedule(tournament_id)
    relay = get_proam_relay(tournament)

    existing_team_count = len(relay.get_teams()) or 2
    raw = request.form.get('num_teams')
    if raw is None or raw == '':
        num_teams = existing_team_count
    else:
        try:
            num_teams = int(raw)
            if num_teams < 1:
                raise ValueError('num_teams must be at least 1')
        except (TypeError, ValueError):
            flash('Invalid number of teams.', 'error')
            return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))

    try:
        result = relay.redraw_lottery(
            num_teams=num_teams,
            expected_digest=request.form.get('expected_relay_digest'),
        )
        invalidate_tournament_caches(tournament_id)
        flash(f"Lottery has been redrawn with {num_teams} team(s).", 'success')
    except RelayStateConflict:
        return _relay_conflict_response()
    except StaleDataError:
        db.session.rollback()
        flash(_STALE_DATA_FLASH, 'warning')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))


@bp.route('/teams')
def view_teams(tournament_id):
    """View the relay teams."""
    tournament = db.get_or_404(Tournament, tournament_id)
    relay = get_proam_relay(tournament)
    teams = relay.get_teams()
    team_health = {t['team_number']: compute_team_health(t, tournament) for t in teams}
    assigned_ids = {
        'pro': {
            member['id']
            for team in teams
            for member in team.get('pro_members', [])
        },
        'college': {
            member['id']
            for team in teams
            for member in team.get('college_members', [])
        },
    }
    replacement_options = {'pro': {'M': [], 'F': []}, 'college': {'M': [], 'F': []}}
    for competitor_type, eligible in (
        ('pro', relay.get_eligible_pro_competitors()),
        ('college', relay.get_eligible_college_competitors()),
    ):
        for competitor in eligible:
            if competitor['id'] not in assigned_ids[competitor_type]:
                replacement_options[competitor_type].setdefault(
                    competitor['gender'], []
                ).append(competitor)

    return render_template('proam_relay/teams.html',
                         tournament=tournament,
                         relay=relay,
                         teams=teams,
                         team_state_digests={
                             team['team_number']: relay.team_state_digest(
                                 team['team_number']
                             )
                             for team in teams
                         },
                         replacement_options=replacement_options,
                         team_health=team_health,
                         status=relay.get_status())


@bp.route('/results', methods=['GET', 'POST'])
@_require_relay_digest
@serialize_sqlite_schedule_writer
def enter_results(tournament_id):
    """Enter relay total times per team."""
    tournament = db.get_or_404(Tournament, tournament_id)

    if request.method == 'POST':
        tournament = lock_tournament_schedule(tournament)
        relay = get_proam_relay(tournament)
        try:
            team_number = int(request.form.get('team_number'))
        except (TypeError, ValueError):
            flash('Invalid team number.', 'error')
            return redirect(url_for('proam_relay.enter_results', tournament_id=tournament_id))

        # Parse time input (MM:SS.ms or just seconds)
        time_input = request.form.get('time_seconds', '').strip()

        try:
            if ':' in time_input:
                parts = time_input.split(':')
                minutes = int(parts[0])
                seconds = float(parts[1])
                total_seconds = minutes * 60 + seconds
            else:
                total_seconds = float(time_input)

            relay.record_total_time(
                team_number,
                total_seconds,
                expected_digest=request.form.get('expected_relay_digest'),
            )
            invalidate_tournament_caches(tournament_id)
            flash(f'Time recorded for Team {team_number}.', 'success')
        except RelayStateConflict:
            return _relay_conflict_response()
        except StaleDataError:
            db.session.rollback()
            flash(_STALE_DATA_FLASH, 'warning')
        except ValueError:
            flash('Invalid time format. Use seconds (45.67) or MM:SS.ms (1:23.45).', 'danger')

        return redirect(url_for('proam_relay.enter_results', tournament_id=tournament_id))

    relay = get_proam_relay(tournament)
    teams = relay.get_teams()
    return render_template('proam_relay/results.html',
                         tournament=tournament,
                         relay=relay,
                         teams=teams,
                         team_state_digests={
                             team['team_number']: relay.team_state_digest(
                                 team['team_number']
                             )
                             for team in teams
                         },
                         status=relay.get_status())


@bp.route('/standings')
def standings(tournament_id):
    """View relay standings/results."""
    tournament = db.get_or_404(Tournament, tournament_id)
    relay = get_proam_relay(tournament)

    return render_template('proam_relay/standings.html',
                         tournament=tournament,
                         relay=relay,
                         results=relay.get_results(),
                         teams=relay.get_teams(),
                         status=relay.get_status())


@bp.route('/manual-teams', methods=['GET'])
def manual_teams(tournament_id):
    """Manual team builder with drag-and-drop."""
    tournament = db.get_or_404(Tournament, tournament_id)
    relay = get_proam_relay(tournament)

    return render_template('proam_relay/manual_teams.html',
                         tournament=tournament,
                         relay=relay,
                         status=relay.get_status(),
                         teams=relay.get_teams(),
                         relay_state_digest=relay.state_digest(),
                         eligible_pro=relay.get_eligible_pro_competitors(),
                         eligible_college=relay.get_eligible_college_competitors())


@bp.route('/manual-teams/save', methods=['POST'])
@_require_relay_digest
@serialize_sqlite_schedule_writer
def save_manual_teams(tournament_id):
    """Save manually assigned teams."""
    tournament = lock_tournament_schedule(tournament_id)
    relay = get_proam_relay(tournament)

    try:
        import json
        teams_json = request.form.get('teams_json', '[]')
        team_assignments = json.loads(teams_json)

        if not isinstance(team_assignments, list) or not team_assignments:
            flash('No team assignments provided.', 'warning')
            return redirect(url_for('proam_relay.manual_teams', tournament_id=tournament_id))

        result = relay.set_teams_manually(
            team_assignments,
            expected_digest=request.form.get('expected_relay_digest'),
        )
        invalidate_tournament_caches(tournament_id)
        flash(result['message'], 'success')
    except RelayStateConflict:
        return _relay_conflict_response()
    except StaleDataError:
        db.session.rollback()
        flash(_STALE_DATA_FLASH, 'warning')
    except (ValueError, json.JSONDecodeError) as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))


@bp.route('/replace-competitor', methods=['POST'])
@_require_relay_digest
@serialize_sqlite_schedule_writer
def replace_competitor(tournament_id):
    """Replace a competitor on a team (e.g., due to injury)."""
    tournament = lock_tournament_schedule(tournament_id)
    relay = get_proam_relay(tournament)

    try:
        team_number = int(request.form.get('team_number'))
        old_competitor_id = int(request.form.get('old_competitor_id'))
        new_competitor_id = int(request.form.get('new_competitor_id'))
    except (TypeError, ValueError):
        flash('Invalid competitor or team ID.', 'error')
        return redirect(url_for('proam_relay.view_teams', tournament_id=tournament_id))

    competitor_type = request.form.get('competitor_type')  # 'pro' or 'college'

    try:
        relay.replace_competitor(
            team_number,
            old_competitor_id,
            new_competitor_id,
            competitor_type,
            expected_digest=request.form.get('expected_relay_digest'),
        )
        invalidate_tournament_caches(tournament_id)
        flash('Competitor replaced successfully', 'success')
    except RelayStateConflict:
        return _relay_conflict_response()
    except StaleDataError:
        db.session.rollback()
        flash(_STALE_DATA_FLASH, 'warning')
        return redirect(url_for('proam_relay.view_teams', tournament_id=tournament_id))
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.view_teams', tournament_id=tournament_id))


# ---------------------------------------------------------------------------
# Relay payout configuration
# ---------------------------------------------------------------------------

@bp.route('/payouts', methods=['GET'])
def relay_payouts(tournament_id):
    """Show relay payout configuration form."""
    tournament = db.get_or_404(Tournament, tournament_id)
    relay_event = Event.query.filter_by(
        tournament_id=tournament_id, name='Pro-Am Relay'
    ).first()
    if relay_event is None:
        abort(404)

    current_payouts = relay_event.get_payouts()
    return render_template(
        'proam_relay/configure_payouts.html',
        tournament=tournament,
        relay_event=relay_event,
        current_payouts=current_payouts,
        expected_payout_digest=_relay_payout_state_digest(
            tournament,
            relay_event,
        ),
    )


@bp.route('/payouts', methods=['POST'])
@serialize_sqlite_schedule_writer
def save_relay_payouts(tournament_id):
    """Save per-team lump sum payout amounts."""
    tournament = lock_tournament_schedule(tournament_id)
    relay_event = Event.query.filter_by(
        tournament_id=tournament_id, name='Pro-Am Relay'
    ).populate_existing().with_for_update().first()
    if relay_event is None:
        abort(404)
    expected_digest = request.form.get('expected_payout_digest', '').strip()
    current_digest = _relay_payout_state_digest(tournament, relay_event)
    if not expected_digest or not hmac.compare_digest(
        expected_digest,
        current_digest,
    ):
        return _relay_payout_conflict_response()

    payouts = {}
    for i in range(1, 9):
        raw = request.form.get(f'payout_{i}')
        if raw:
            try:
                amount = max(0.0, float(raw))
                payouts[str(i)] = amount
            except (TypeError, ValueError):
                db.session.rollback()
                flash(f'Invalid payout amount for position {i}: {raw!r}', 'error')
                return redirect(url_for('proam_relay.relay_payouts',
                                        tournament_id=tournament_id))

    relay_event.set_payouts(payouts)
    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    flash('Relay payouts saved.', 'success')
    return redirect(url_for('proam_relay.relay_payouts', tournament_id=tournament_id))


@bp.route('/team/<int:team_id>/toggle-settled', methods=['POST'])
def toggle_relay_settlement(tournament_id, team_id):
    """Toggle payment status for one final, payable Relay team."""
    if not getattr(current_user, 'is_admin', False):
        abort(403)

    tournament = db.get_or_404(Tournament, tournament_id)
    payout_row = next(
        (
            row for row in relay_payout_summary(tournament)['rows']
            if row['team'].id == team_id
        ),
        None,
    )
    if payout_row is None:
        abort(404)

    team = db.get_or_404(RelayTeam, team_id)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    team.payout_settled = not team.payout_settled
    try:
        db.session.commit()
    except StaleDataError:
        db.session.rollback()
        if is_ajax:
            return jsonify({'ok': False, 'message': _STALE_DATA_FLASH}), 409
        flash(_STALE_DATA_FLASH, 'warning')
        return redirect(url_for('reporting.pro_payout_summary', tournament_id=tournament_id))

    invalidate_tournament_caches(tournament_id)
    log_action(
        'relay_payout_settlement_toggled',
        'relay_team',
        team.id,
        {
            'settled': team.payout_settled,
            'team_name': team.name,
            'team_number': team.team_number,
            'payout_amount': payout_row['payout_amount'],
        },
    )

    if is_ajax:
        return jsonify({'ok': True, 'settled': team.payout_settled})
    return redirect(url_for('reporting.pro_payout_summary', tournament_id=tournament_id))


# API endpoints for AJAX calls
@bp.route('/api/status')
def api_status(tournament_id):
    """Get relay status as JSON."""
    tournament = db.get_or_404(Tournament, tournament_id)
    relay = get_proam_relay(tournament)

    return jsonify({
        'status': relay.get_status(),
        'teams': relay.get_teams(),
        'results': relay.get_results()
    })
