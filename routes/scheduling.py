"""
Scheduling routes for heat and flight generation.
"""
import re
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from database import db
from models import Tournament, Event, Heat, Flight
import config
import strings as text

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
    college_open_events = [_with_field_key(e) for e in config.COLLEGE_OPEN_EVENTS]
    college_closed_events = [_with_field_key(e) for e in config.COLLEGE_CLOSED_EVENTS]
    pro_events = [_with_field_key(e) for e in config.PRO_EVENTS]

    if request.method == 'POST':
        event_type = request.form.get('event_type')  # 'college' or 'pro'

        if event_type == 'college':
            skipped = _create_college_events(tournament, request.form, college_open_events, college_closed_events)
            if skipped:
                flash(
                    f'Skipped removing {skipped} college event(s) because heats/results already exist.',
                    'warning'
                )
        elif event_type == 'pro':
            skipped = _create_pro_events(tournament, request.form, pro_events)
            if skipped:
                flash(
                    f'Skipped removing {skipped} pro event(s) because heats/results already exist.',
                    'warning'
                )

        db.session.commit()
        flash(text.FLASH['events_configured'], 'success')
        return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))

    existing_config = _get_existing_event_config(tournament)

    return render_template('scheduling/setup_events.html',
                           tournament=tournament,
                           college_open_events=college_open_events,
                           college_closed_events=college_closed_events,
                           pro_events=pro_events,
                           existing_config=existing_config)


def _create_college_events(tournament, form_data, college_open_events, college_closed_events):
    """Create/update college events based on form configuration and remove deselected events."""
    selected_signatures = set()

    # Process OPEN events (check if each should be treated as CLOSED)
    for event_config in college_open_events:
        # Check if this event should be treated as CLOSED
        is_open = form_data.get(f"open_{event_config['field_key']}", 'open') == 'open'

        # Create gendered versions if applicable
        if event_config.get('is_partnered') and event_config.get('partner_gender') == 'mixed':
            # Mixed gender events are not gendered
            event = _upsert_event(tournament, event_config, 'college', None, is_open)
            selected_signatures.add(_event_signature(event.name, event.event_type, event.gender))
        else:
            # Create men's and women's versions
            event_m = _upsert_event(tournament, event_config, 'college', 'M', is_open)
            event_f = _upsert_event(tournament, event_config, 'college', 'F', is_open)
            selected_signatures.add(_event_signature(event_m.name, event_m.event_type, event_m.gender))
            selected_signatures.add(_event_signature(event_f.name, event_f.event_type, event_f.gender))

    # Process CLOSED events
    for event_config in college_closed_events:
        if form_data.get(f"enable_{event_config['field_key']}") != 'on':
            continue

        if event_config.get('is_gendered', True):
            # Create men's and women's versions
            event_m = _upsert_event(tournament, event_config, 'college', 'M', False)
            event_f = _upsert_event(tournament, event_config, 'college', 'F', False)
            selected_signatures.add(_event_signature(event_m.name, event_m.event_type, event_m.gender))
            selected_signatures.add(_event_signature(event_f.name, event_f.event_type, event_f.gender))
        else:
            event = _upsert_event(tournament, event_config, 'college', None, False)
            selected_signatures.add(_event_signature(event.name, event.event_type, event.gender))

    return _remove_deselected_events(tournament, 'college', selected_signatures)


def _create_pro_events(tournament, form_data, pro_events):
    """Create/update pro events based on form configuration and remove deselected events."""
    selected_signatures = set()

    for event_config in pro_events:
        # Check if this event is enabled
        if form_data.get(f"enable_{event_config['field_key']}") != 'on':
            continue

        if event_config.get('is_gendered', False):
            # Check which genders are enabled
            if form_data.get(f"enable_{event_config['field_key']}_M") == 'on':
                event_m = _upsert_event(tournament, event_config, 'pro', 'M', False)
                selected_signatures.add(_event_signature(event_m.name, event_m.event_type, event_m.gender))
            if form_data.get(f"enable_{event_config['field_key']}_F") == 'on':
                event_f = _upsert_event(tournament, event_config, 'pro', 'F', False)
                selected_signatures.add(_event_signature(event_f.name, event_f.event_type, event_f.gender))
        else:
            event = _upsert_event(tournament, event_config, 'pro', None, False)
            selected_signatures.add(_event_signature(event.name, event.event_type, event.gender))

    return _remove_deselected_events(tournament, 'pro', selected_signatures)


def _upsert_event(tournament, event_config, event_type, gender, is_open):
    """Create or update a single event from configuration."""
    stand_config = config.STAND_CONFIGS.get(event_config.get('stand_type', ''), {})

    event = tournament.events.filter_by(
        name=event_config['name'],
        event_type=event_type,
        gender=gender
    ).first()

    if not event:
        event = Event(
            tournament_id=tournament.id,
            name=event_config['name'],
            event_type=event_type,
            gender=gender
        )
        db.session.add(event)

    event.scoring_type = event_config['scoring_type']
    event.scoring_order = 'highest_wins' if event_config['scoring_type'] in ['score', 'distance'] else 'lowest_wins'
    event.is_open = is_open
    event.is_partnered = event_config.get('is_partnered', False)
    event.partner_gender_requirement = event_config.get('partner_gender')
    event.requires_dual_runs = event_config.get('requires_dual_runs', False)
    event.stand_type = event_config.get('stand_type')
    event.max_stands = stand_config.get('total')
    event.has_prelims = event_config.get('has_prelims', False)

    return event


def _with_field_key(event_config):
    """Add a safe key used for form field names and IDs."""
    event = dict(event_config)
    event['field_key'] = _field_key(event_config['name'])
    return event


def _field_key(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(name).lower()).strip('_')


def _event_signature(name, event_type, gender):
    return f"{event_type}|{name}|{gender or ''}"


def _remove_deselected_events(tournament, event_type, selected_signatures):
    """Remove deselected events unless they already contain generated data."""
    skipped = 0
    existing_events = tournament.events.filter_by(event_type=event_type).all()

    for event in existing_events:
        sig = _event_signature(event.name, event.event_type, event.gender)
        if sig in selected_signatures:
            continue
        if event.heats.count() > 0 or event.results.count() > 0:
            skipped += 1
            continue
        db.session.delete(event)

    return skipped


def _get_existing_event_config(tournament):
    """Build current configuration state for setup checkboxes/radios."""
    events = tournament.events.all()
    has_any_college = any(e.event_type == 'college' for e in events)

    open_state = {}
    for cfg in config.COLLEGE_OPEN_EVENTS:
        matching = [e for e in events if e.event_type == 'college' and e.name == cfg['name']]
        if matching:
            open_state[cfg['name']] = bool(matching[0].is_open)
        else:
            open_state[cfg['name']] = True

    closed_enabled = {}
    for cfg in config.COLLEGE_CLOSED_EVENTS:
        key = cfg['name']
        matching = [e for e in events if e.event_type == 'college' and e.name == key]
        closed_enabled[key] = bool(matching) if has_any_college else True

    pro_enabled = {}
    pro_gender = {}
    for cfg in config.PRO_EVENTS:
        key = cfg['name']
        matching = [e for e in events if e.event_type == 'pro' and e.name == key]
        pro_enabled[key] = bool(matching)
        pro_gender[key] = {
            'M': any(e.gender == 'M' for e in matching),
            'F': any(e.gender == 'F' for e in matching),
        }

    return {
        'college_open_state': open_state,
        'college_closed_enabled': closed_enabled,
        'pro_enabled': pro_enabled,
        'pro_gender': pro_gender,
    }


@scheduling_bp.route('/<int:tournament_id>/day-schedule', methods=['GET', 'POST'])
def day_schedule(tournament_id):
    """Generate a Friday/Saturday schedule using Missoula Pro Am rules."""
    from services.schedule_builder import build_day_schedule, COLLEGE_SATURDAY_PRIORITY

    tournament = Tournament.query.get_or_404(tournament_id)
    pro_events = tournament.events.filter_by(event_type='pro').order_by(Event.name, Event.gender).all()

    priority_index = {priority: idx for idx, priority in enumerate(COLLEGE_SATURDAY_PRIORITY)}
    college_events = tournament.events.filter_by(event_type='college').all()
    college_sat_options = []
    for event in college_events:
        key = (event.name, event.gender)
        if key in priority_index:
            college_sat_options.append(event)
    college_sat_options.sort(key=lambda e: priority_index[(e.name, e.gender)])

    session_key = f'schedule_options_{tournament_id}'
    saved = session.get(session_key, {})

    if request.method == 'POST':
        friday_pro_event_ids = [int(eid) for eid in request.form.getlist('friday_pro_event_ids')]
        saturday_college_event_ids = [int(eid) for eid in request.form.getlist('saturday_college_event_ids')]
        saved = {
            'friday_pro_event_ids': friday_pro_event_ids,
            'saturday_college_event_ids': saturday_college_event_ids,
        }
        session[session_key] = saved
        session.modified = True
    else:
        saved = {
            'friday_pro_event_ids': [int(eid) for eid in saved.get('friday_pro_event_ids', [])],
            'saturday_college_event_ids': [int(eid) for eid in saved.get('saturday_college_event_ids', [])],
        }

    schedule = build_day_schedule(
        tournament,
        friday_pro_event_ids=saved['friday_pro_event_ids'],
        saturday_college_event_ids=saved['saturday_college_event_ids']
    )

    return render_template(
        'scheduling/day_schedule.html',
        tournament=tournament,
        pro_events=pro_events,
        college_sat_options=college_sat_options,
        selected_friday_pro_event_ids=saved['friday_pro_event_ids'],
        selected_saturday_college_event_ids=saved['saturday_college_event_ids'],
        schedule=schedule
    )


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
        flash(text.FLASH['heats_generated'].format(num_heats=num_heats, event_name=event.display_name), 'success')
    except Exception as e:
        flash(text.FLASH['heats_error'].format(error=str(e)), 'error')

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
            flash(text.FLASH['flights_built'].format(num_flights=num_flights), 'success')
        except Exception as e:
            flash(text.FLASH['flights_error'].format(error=str(e)), 'error')

        return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))

    # Get available heats
    pro_events = tournament.events.filter_by(event_type='pro').all()

    return render_template('pro/build_flights.html',
                           tournament=tournament,
                           events=pro_events)
