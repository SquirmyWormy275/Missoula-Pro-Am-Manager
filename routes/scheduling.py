"""
Scheduling routes for heat and flight generation.
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request
from database import db
from models import Tournament, Event, Heat, Flight
import config

scheduling_bp = Blueprint('scheduling', __name__)


@scheduling_bp.route('/<int:tournament_id>/events')
def event_list(tournament_id):
    """List all events for a tournament."""
    tournament = Tournament.query.get_or_404(tournament_id)

    college_events = tournament.events.filter_by(event_type='college').all()
    pro_events = tournament.events.filter_by(event_type='pro').all()

    return render_template('scheduling/events.html',
                           tournament=tournament,
                           college_events=college_events,
                           pro_events=pro_events)


@scheduling_bp.route('/<int:tournament_id>/events/setup', methods=['GET', 'POST'])
def setup_events(tournament_id):
    """Configure events for the tournament."""
    tournament = Tournament.query.get_or_404(tournament_id)

    if request.method == 'POST':
        event_type = request.form.get('event_type')  # 'college' or 'pro'

        if event_type == 'college':
            # Create college events based on config
            _create_college_events(tournament, request.form)
        elif event_type == 'pro':
            # Create pro events based on config
            _create_pro_events(tournament, request.form)

        db.session.commit()
        flash('Events configured successfully!', 'success')
        return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))

    return render_template('scheduling/setup_events.html',
                           tournament=tournament,
                           college_open_events=config.COLLEGE_OPEN_EVENTS,
                           college_closed_events=config.COLLEGE_CLOSED_EVENTS,
                           pro_events=config.PRO_EVENTS)


def _create_college_events(tournament, form_data):
    """Create college events based on form configuration."""
    # Process OPEN events (check if each should be treated as CLOSED)
    for event_config in config.COLLEGE_OPEN_EVENTS:
        # Check if this event should be treated as CLOSED
        is_open = form_data.get(f"open_{event_config['name']}", 'open') == 'open'

        # Create gendered versions if applicable
        if event_config.get('is_partnered') and event_config.get('partner_gender') == 'mixed':
            # Mixed gender events are not gendered
            _create_event(tournament, event_config, 'college', None, is_open)
        else:
            # Create men's and women's versions
            _create_event(tournament, event_config, 'college', 'M', is_open)
            _create_event(tournament, event_config, 'college', 'F', is_open)

    # Process CLOSED events
    for event_config in config.COLLEGE_CLOSED_EVENTS:
        if event_config.get('is_gendered', True):
            # Create men's and women's versions
            _create_event(tournament, event_config, 'college', 'M', False)
            _create_event(tournament, event_config, 'college', 'F', False)
        else:
            _create_event(tournament, event_config, 'college', None, False)


def _create_pro_events(tournament, form_data):
    """Create pro events based on form configuration."""
    for event_config in config.PRO_EVENTS:
        # Check if this event is enabled
        if form_data.get(f"enable_{event_config['name']}") != 'on':
            continue

        if event_config.get('is_gendered', False):
            # Check which genders are enabled
            if form_data.get(f"enable_{event_config['name']}_M") == 'on':
                _create_event(tournament, event_config, 'pro', 'M', False)
            if form_data.get(f"enable_{event_config['name']}_F") == 'on':
                _create_event(tournament, event_config, 'pro', 'F', False)
        else:
            _create_event(tournament, event_config, 'pro', None, False)


def _create_event(tournament, event_config, event_type, gender, is_open):
    """Create a single event from configuration."""
    stand_config = config.STAND_CONFIGS.get(event_config.get('stand_type', ''), {})

    event = Event(
        tournament_id=tournament.id,
        name=event_config['name'],
        event_type=event_type,
        gender=gender,
        scoring_type=event_config['scoring_type'],
        scoring_order='highest_wins' if event_config['scoring_type'] in ['score', 'distance'] else 'lowest_wins',
        is_open=is_open,
        is_partnered=event_config.get('is_partnered', False),
        partner_gender_requirement=event_config.get('partner_gender'),
        requires_dual_runs=event_config.get('requires_dual_runs', False),
        stand_type=event_config.get('stand_type'),
        max_stands=stand_config.get('total'),
        has_prelims=event_config.get('has_prelims', False)
    )

    db.session.add(event)


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/heats')
def event_heats(tournament_id, event_id):
    """View and manage heats for an event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)

    heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()

    return render_template('scheduling/heats.html',
                           tournament=tournament,
                           event=event,
                           heats=heats)


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/generate-heats', methods=['POST'])
def generate_heats(tournament_id, event_id):
    """Generate heats for an event using snake draft distribution."""
    event = Event.query.get_or_404(event_id)

    # Import heat generation service
    from services.heat_generator import generate_event_heats

    try:
        num_heats = generate_event_heats(event)
        flash(f'Generated {num_heats} heat(s) for {event.display_name}.', 'success')
    except Exception as e:
        flash(f'Error generating heats: {str(e)}', 'error')

    return redirect(url_for('scheduling.event_heats',
                            tournament_id=tournament_id,
                            event_id=event_id))


@scheduling_bp.route('/<int:tournament_id>/flights')
def flight_list(tournament_id):
    """View and manage flights for pro competition."""
    tournament = Tournament.query.get_or_404(tournament_id)
    flights = Flight.query.filter_by(tournament_id=tournament_id).order_by(Flight.flight_number).all()

    return render_template('pro/flights.html',
                           tournament=tournament,
                           flights=flights)


@scheduling_bp.route('/<int:tournament_id>/flights/build', methods=['GET', 'POST'])
def build_flights(tournament_id):
    """Build flights for pro competition."""
    tournament = Tournament.query.get_or_404(tournament_id)

    if request.method == 'POST':
        from services.flight_builder import build_pro_flights

        try:
            num_flights = build_pro_flights(tournament)
            flash(f'Built {num_flights} flight(s) for pro competition.', 'success')
        except Exception as e:
            flash(f'Error building flights: {str(e)}', 'error')

        return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))

    # Get available heats
    pro_events = tournament.events.filter_by(event_type='pro').all()

    return render_template('pro/build_flights.html',
                           tournament=tournament,
                           events=pro_events)
