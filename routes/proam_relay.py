"""
Routes for Pro-Am Relay lottery and management.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from database import db
from models import Tournament
from services.proam_relay import get_proam_relay, create_proam_relay_event

bp = Blueprint('proam_relay', __name__, url_prefix='/tournament/<int:tournament_id>/proam-relay')


@bp.route('/')
def relay_dashboard(tournament_id):
    """Pro-Am Relay dashboard."""
    tournament = Tournament.query.get_or_404(tournament_id)
    relay = get_proam_relay(tournament)

    return render_template('proam_relay/dashboard.html',
                         tournament=tournament,
                         relay=relay,
                         status=relay.get_status(),
                         teams=relay.get_teams(),
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
        flash(result['message'], 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))


@bp.route('/redraw', methods=['POST'])
def redraw_lottery(tournament_id):
    """Clear and redraw the lottery."""
    tournament = Tournament.query.get_or_404(tournament_id)
    relay = get_proam_relay(tournament)

    try:
        existing_team_count = len(relay.get_teams()) or 2
        result = relay.redraw_lottery(num_teams=existing_team_count)
        flash('Lottery has been redrawn!', 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.relay_dashboard', tournament_id=tournament_id))


@bp.route('/teams')
def view_teams(tournament_id):
    """View the relay teams."""
    tournament = Tournament.query.get_or_404(tournament_id)
    relay = get_proam_relay(tournament)

    return render_template('proam_relay/teams.html',
                         tournament=tournament,
                         relay=relay,
                         teams=relay.get_teams(),
                         status=relay.get_status())


@bp.route('/results', methods=['GET', 'POST'])
def enter_results(tournament_id):
    """Enter relay event results."""
    tournament = Tournament.query.get_or_404(tournament_id)
    relay = get_proam_relay(tournament)

    if request.method == 'POST':
        try:
            team_number = int(request.form.get('team_number'))
        except (TypeError, ValueError):
            flash('Invalid team number.', 'error')
            return redirect(url_for('proam_relay.enter_results', tournament_id=tournament_id))

        event_name = request.form.get('event_name')

        # Parse time input (MM:SS.ms or just seconds)
        time_input = request.form.get('time_seconds', '').strip()

        try:
            if ':' in time_input:
                # MM:SS.ms format
                parts = time_input.split(':')
                minutes = int(parts[0])
                seconds = float(parts[1])
                time_seconds = minutes * 60 + seconds
            else:
                time_seconds = float(time_input)

            relay.record_event_result(team_number, event_name, time_seconds)
            flash(f'Result recorded for Team {team_number} - {event_name}', 'success')
        except ValueError:
            flash('Invalid time format. Use seconds or MM:SS.ms', 'danger')

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
        flash('Competitor replaced successfully', 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('proam_relay.view_teams', tournament_id=tournament_id))


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
