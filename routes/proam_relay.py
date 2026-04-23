"""
Routes for Pro-Am Relay lottery and management.
"""
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from database import db
from models import Tournament
from models.event import Event
from services.cache_invalidation import invalidate_tournament_caches
from services.proam_relay import compute_team_health, create_proam_relay_event, get_proam_relay

bp = Blueprint('proam_relay', __name__, url_prefix='/tournament/<int:tournament_id>/proam-relay')


@bp.route('/')
def relay_dashboard(tournament_id):
    """Pro-Am Relay dashboard."""
    tournament = Tournament.query.get_or_404(tournament_id)
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
                         team_health=team_health,
                         capacity=relay.get_lottery_capacity(),
                         eligible_pro=relay.get_eligible_pro_competitors(),
                         eligible_college=relay.get_eligible_college_competitors())


@bp.route('/draw', methods=['POST'])
def draw_lottery(tournament_id):
    """Run the Pro-Am Relay lottery."""
    tournament = Tournament.query.get_or_404(tournament_id)
    relay = get_proam_relay(tournament)

    try:
        num_teams = int(request.form.get('num_teams', 2))
        if num_teams < 1:
            raise ValueError('num_teams must be at least 1')
    except (TypeError, ValueError):
        flash('Invalid number of teams.', 'error')
        return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))

    try:
        result = relay.run_lottery(num_teams=num_teams)
        invalidate_tournament_caches(tournament_id)
        flash(result['message'], 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))


@bp.route('/redraw', methods=['POST'])
def redraw_lottery(tournament_id):
    """Clear and redraw the lottery."""
    tournament = Tournament.query.get_or_404(tournament_id)
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
        result = relay.redraw_lottery(num_teams=num_teams)
        invalidate_tournament_caches(tournament_id)
        flash(f"Lottery has been redrawn with {num_teams} team(s).", 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))


@bp.route('/teams')
def view_teams(tournament_id):
    """View the relay teams."""
    tournament = Tournament.query.get_or_404(tournament_id)
    relay = get_proam_relay(tournament)
    teams = relay.get_teams()
    team_health = {t['team_number']: compute_team_health(t, tournament) for t in teams}

    return render_template('proam_relay/teams.html',
                         tournament=tournament,
                         relay=relay,
                         teams=teams,
                         team_health=team_health,
                         status=relay.get_status())


@bp.route('/results', methods=['GET', 'POST'])
def enter_results(tournament_id):
    """Enter relay total times per team."""
    tournament = Tournament.query.get_or_404(tournament_id)
    relay = get_proam_relay(tournament)

    if request.method == 'POST':
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

            relay.record_total_time(team_number, total_seconds)
            invalidate_tournament_caches(tournament_id)
            flash(f'Time recorded for Team {team_number}.', 'success')
        except ValueError:
            flash('Invalid time format. Use seconds (45.67) or MM:SS.ms (1:23.45).', 'danger')

        return redirect(url_for('proam_relay.enter_results', tournament_id=tournament_id))

    return render_template('proam_relay/results.html',
                         tournament=tournament,
                         relay=relay,
                         teams=relay.get_teams(),
                         status=relay.get_status())


@bp.route('/standings')
def standings(tournament_id):
    """View relay standings/results."""
    tournament = Tournament.query.get_or_404(tournament_id)
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
    tournament = Tournament.query.get_or_404(tournament_id)
    relay = get_proam_relay(tournament)

    return render_template('proam_relay/manual_teams.html',
                         tournament=tournament,
                         relay=relay,
                         status=relay.get_status(),
                         teams=relay.get_teams(),
                         eligible_pro=relay.get_eligible_pro_competitors(),
                         eligible_college=relay.get_eligible_college_competitors())


@bp.route('/manual-teams/save', methods=['POST'])
def save_manual_teams(tournament_id):
    """Save manually assigned teams."""
    tournament = Tournament.query.get_or_404(tournament_id)
    relay = get_proam_relay(tournament)

    try:
        import json
        teams_json = request.form.get('teams_json', '[]')
        team_assignments = json.loads(teams_json)

        if not isinstance(team_assignments, list) or not team_assignments:
            flash('No team assignments provided.', 'warning')
            return redirect(url_for('proam_relay.manual_teams', tournament_id=tournament_id))

        result = relay.set_teams_manually(team_assignments)
        invalidate_tournament_caches(tournament_id)
        flash(result['message'], 'success')
    except (ValueError, json.JSONDecodeError) as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))


@bp.route('/replace-competitor', methods=['POST'])
def replace_competitor(tournament_id):
    """Replace a competitor on a team (e.g., due to injury)."""
    tournament = Tournament.query.get_or_404(tournament_id)
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
        relay.replace_competitor(team_number, old_competitor_id, new_competitor_id, competitor_type)
        invalidate_tournament_caches(tournament_id)
        flash('Competitor replaced successfully', 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.view_teams', tournament_id=tournament_id))


# ---------------------------------------------------------------------------
# Relay payout configuration
# ---------------------------------------------------------------------------

@bp.route('/payouts', methods=['GET'])
def relay_payouts(tournament_id):
    """Show relay payout configuration form."""
    tournament = Tournament.query.get_or_404(tournament_id)
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
    )


@bp.route('/payouts', methods=['POST'])
def save_relay_payouts(tournament_id):
    """Save per-team lump sum payout amounts."""
    Tournament.query.get_or_404(tournament_id)
    relay_event = Event.query.filter_by(
        tournament_id=tournament_id, name='Pro-Am Relay'
    ).first()
    if relay_event is None:
        abort(404)

    payouts = {}
    for i in range(1, 9):
        raw = request.form.get(f'payout_{i}')
        if raw:
            try:
                amount = max(0.0, float(raw))
                payouts[str(i)] = amount
            except (TypeError, ValueError):
                flash(f'Invalid payout amount for position {i}: {raw!r}', 'error')
                return redirect(url_for('proam_relay.relay_payouts',
                                        tournament_id=tournament_id))

    relay_event.set_payouts(payouts)
    db.session.commit()
    invalidate_tournament_caches(tournament_id)
    flash('Relay payouts saved.', 'success')
    return redirect(url_for('proam_relay.relay_payouts', tournament_id=tournament_id))


# API endpoints for AJAX calls
@bp.route('/api/status')
def api_status(tournament_id):
    """Get relay status as JSON."""
    tournament = Tournament.query.get_or_404(tournament_id)
    relay = get_proam_relay(tournament)

    return jsonify({
        'status': relay.get_status(),
        'teams': relay.get_teams(),
        'results': relay.get_results()
    })
