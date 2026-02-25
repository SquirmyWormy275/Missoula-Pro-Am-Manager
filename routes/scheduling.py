"""
Scheduling routes for heat and flight generation.
"""
import re
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from database import db
from models import Tournament, Event, Heat, Flight
from models.competitor import CollegeCompetitor, ProCompetitor
import config
import strings as text

scheduling_bp = Blueprint('scheduling', __name__)
LIST_ONLY_EVENT_NAMES = {
    'axethrow',
    'peaveylogroll',
    'cabertoss',
    'pulptoss',
}


@scheduling_bp.route('/<int:tournament_id>/events')
def event_list(tournament_id):
    """List all events for a tournament."""
    tournament = Tournament.query.get_or_404(tournament_id)

    college_events = tournament.events.filter_by(event_type='college').all()
    pro_events = tournament.events.filter_by(event_type='pro').all()
    assignment_details = _build_assignment_details(tournament, college_events + pro_events)
    entrant_counts = {
        event.id: len(_signed_up_competitors(event))
        for event in (college_events + pro_events)
    }

    return render_template('scheduling/events.html',
                           tournament=tournament,
                           college_events=college_events,
                           pro_events=pro_events,
                           assignment_details=assignment_details,
                           entrant_counts=entrant_counts)


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


def _normalize_name(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def _normalize_person_name(value: str) -> str:
    return str(value or '').strip().lower()


def _is_list_only_event(event: Event) -> bool:
    return event.event_type == 'college' and _normalize_name(event.name) in LIST_ONLY_EVENT_NAMES


def _build_assignment_details(tournament: Tournament, events: list[Event]) -> dict:
    details = {}
    for event in events:
        if _is_list_only_event(event):
            signup_rows = _build_signup_rows(event)
            details[event.id] = {
                'mode': 'signup',
                'rows': signup_rows,
                'count': len(signup_rows),
            }
            continue

        heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
        all_comp_ids = []
        for heat in heats:
            all_comp_ids.extend(heat.get_competitors())
        comp_lookup = _load_competitor_lookup(event, all_comp_ids)

        heat_rows = []
        for heat in heats:
            assignments = heat.get_stand_assignments()
            competitors = []
            for comp_id in heat.get_competitors():
                comp = comp_lookup.get(comp_id)
                competitors.append({
                    'name': comp.name if comp else f'Unknown ({comp_id})',
                    'stand': assignments.get(str(comp_id)),
                })
            heat_rows.append({
                'heat_number': heat.heat_number,
                'run_number': heat.run_number,
                'competitors': competitors,
            })

        details[event.id] = {
            'mode': 'heats',
            'rows': heat_rows,
            'count': len(heat_rows),
        }

    return details


def _load_competitor_lookup(event: Event, competitor_ids: list[int]) -> dict:
    ids = sorted(set(int(cid) for cid in competitor_ids if cid is not None))
    if not ids:
        return {}
    if event.event_type == 'college':
        competitors = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(ids)).all()
    else:
        competitors = ProCompetitor.query.filter(ProCompetitor.id.in_(ids)).all()
    return {c.id: c for c in competitors}


def _build_signup_rows(event: Event) -> list[str]:
    competitors = _signed_up_competitors(event)
    if not event.is_partnered:
        return [c.name for c in competitors]

    rows = []
    used = set()
    by_name = {_normalize_person_name(c.name): c for c in competitors}
    for comp in competitors:
        if comp.id in used:
            continue
        partner_name = _resolve_partner_name(comp, event)
        partner = by_name.get(_normalize_person_name(partner_name)) if partner_name else None
        if partner and partner.id not in used:
            rows.append(f'{comp.name} + {partner.name}')
            used.add(comp.id)
            used.add(partner.id)
        else:
            rows.append(comp.name)
            used.add(comp.id)

    return rows


def _signed_up_competitors(event: Event) -> list:
    if event.event_type == 'college':
        all_comps = CollegeCompetitor.query.filter_by(
            tournament_id=event.tournament_id,
            status='active'
        ).all()
    else:
        all_comps = ProCompetitor.query.filter_by(
            tournament_id=event.tournament_id,
            status='active'
        ).all()

    signed = []
    for comp in all_comps:
        entered = comp.get_events_entered() if hasattr(comp, 'get_events_entered') else []
        if _competitor_entered_event(event, entered):
            if event.gender and getattr(comp, 'gender', None) != event.gender:
                continue
            signed.append(comp)

    return sorted(signed, key=lambda c: c.name.lower())


def _competitor_entered_event(event: Event, entered_events: list) -> bool:
    entered = entered_events if isinstance(entered_events, list) else []
    target_id = str(event.id)
    target_name = _normalize_name(event.name)
    target_display_name = _normalize_name(event.display_name)

    for raw in entered:
        value = str(raw).strip()
        if not value:
            continue
        if value == target_id:
            return True
        normalized = _normalize_name(value)
        if normalized in {target_name, target_display_name}:
            return True
    return False


def _resolve_partner_name(competitor, event: Event) -> str:
    partners = competitor.get_partners() if hasattr(competitor, 'get_partners') else {}
    if not isinstance(partners, dict):
        return ''
    candidates = [
        str(event.id),
        event.name,
        event.display_name,
        event.name.lower(),
        event.display_name.lower(),
    ]
    for key in candidates:
        value = partners.get(key)
        if str(value or '').strip():
            return str(value).strip()
    return ''


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
    from services.heat_generator import generate_event_heats
    from services.flight_builder import build_pro_flights

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
        action = request.form.get('action', 'generate_schedule')
        friday_pro_event_ids = [int(eid) for eid in request.form.getlist('friday_pro_event_ids') if str(eid).strip()]
        saturday_college_event_ids = [int(eid) for eid in request.form.getlist('saturday_college_event_ids') if str(eid).strip()]
        saved = {
            'friday_pro_event_ids': friday_pro_event_ids,
            'saturday_college_event_ids': saturday_college_event_ids,
        }
        session[session_key] = saved
        session.modified = True
        if action == 'generate_all':
            _generate_all_heats(tournament, generate_event_heats)
            flights = _build_pro_flights_if_possible(tournament, build_pro_flights)
            if flights is not None:
                flash(f'Built {flights} pro flight(s).', 'success')
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
    detailed_schedule = _hydrate_schedule_for_display(tournament, schedule)

    return render_template(
        'scheduling/day_schedule.html',
        tournament=tournament,
        pro_events=pro_events,
        college_sat_options=college_sat_options,
        selected_friday_pro_event_ids=saved['friday_pro_event_ids'],
        selected_saturday_college_event_ids=saved['saturday_college_event_ids'],
        schedule=schedule,
        detailed_schedule=detailed_schedule
    )


@scheduling_bp.route('/<int:tournament_id>/day-schedule/print')
def day_schedule_print(tournament_id):
    """Printable day schedule with heat/stand assignments."""
    from services.schedule_builder import build_day_schedule

    tournament = Tournament.query.get_or_404(tournament_id)
    session_key = f'schedule_options_{tournament_id}'
    saved = session.get(session_key, {})
    friday_pro_event_ids = [int(eid) for eid in saved.get('friday_pro_event_ids', [])]
    saturday_college_event_ids = [int(eid) for eid in saved.get('saturday_college_event_ids', [])]

    schedule = build_day_schedule(
        tournament,
        friday_pro_event_ids=friday_pro_event_ids,
        saturday_college_event_ids=saturday_college_event_ids
    )
    detailed_schedule = _hydrate_schedule_for_display(tournament, schedule)

    return render_template(
        'scheduling/day_schedule_print.html',
        tournament=tournament,
        schedule=schedule,
        detailed_schedule=detailed_schedule
    )


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/heats')
def event_heats(tournament_id, event_id):
    """View and manage heats for an event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)

    heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
    signup_list_mode = _is_list_only_event(event)
    signup_rows = _build_signup_rows(event) if signup_list_mode else []

    return render_template('scheduling/heats.html',
                           tournament=tournament,
                           event=event,
                           heats=heats,
                           signup_rows=signup_rows,
                           signup_list_mode=signup_list_mode)


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/generate-heats', methods=['POST'])
def generate_heats(tournament_id, event_id):
    """Generate heats for an event using snake draft distribution."""
    event = Event.query.get_or_404(event_id)

    # Import heat generation service
    from services.heat_generator import generate_event_heats

    try:
        num_heats = generate_event_heats(event)
        if _is_list_only_event(event):
            flash(f'{event.display_name} uses signups only (no heats).', 'success')
        else:
            flash(text.FLASH['heats_generated'].format(num_heats=num_heats, event_name=event.display_name), 'success')
    except Exception as e:
        flash(text.FLASH['heats_error'].format(error=str(e)), 'error')

    return redirect(url_for('scheduling.event_heats',
                            tournament_id=tournament_id,
                            event_id=event_id))


def _generate_all_heats(tournament: Tournament, generate_event_heats_fn):
    """Generate heats for all configured events."""
    events = tournament.events.order_by(Event.event_type, Event.name, Event.gender).all()
    generated = 0
    skipped = 0
    errors = 0

    for event in events:
        try:
            generate_event_heats_fn(event)
            generated += 1
        except Exception as exc:
            if 'No competitors entered' in str(exc):
                skipped += 1
            else:
                errors += 1
                flash(f'Heat generation error for {event.display_name}: {exc}', 'error')

    flash(f'Heats generated for {generated} event(s). Skipped {skipped} without entrants.', 'success')
    if errors:
        flash(f'Failed to generate heats for {errors} event(s).', 'error')


def _build_pro_flights_if_possible(tournament: Tournament, build_pro_flights_fn):
    """Build pro flights if there are any pro heats."""
    pro_heats = Heat.query.join(Event).filter(
        Event.tournament_id == tournament.id,
        Event.event_type == 'pro',
        Heat.run_number == 1
    ).count()
    if pro_heats == 0:
        flash('No pro heats available yet, so no flights were built.', 'warning')
        return None
    return build_pro_flights_fn(tournament)


def _hydrate_schedule_for_display(tournament: Tournament, schedule: dict) -> dict:
    """Attach heat + stand assignment details to schedule entries for display/print."""
    return {
        'friday_day': _hydrate_schedule_entries(tournament, schedule.get('friday_day', [])),
        'friday_feature': _hydrate_schedule_entries(tournament, schedule.get('friday_feature', [])),
        'saturday_show': _hydrate_schedule_entries(tournament, schedule.get('saturday_show', [])),
    }


def _hydrate_schedule_entries(tournament: Tournament, entries: list[dict]) -> list[dict]:
    hydrated = []
    for item in entries:
        event = Event.query.get(item.get('event_id')) if item.get('event_id') else None
        detail_heats = []
        if event:
            if item.get('heat_id'):
                heat = Heat.query.get(item['heat_id'])
                if heat:
                    detail_heats = [_serialize_heat_detail(tournament, event, heat)]
            else:
                event_heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
                detail_heats = [_serialize_heat_detail(tournament, event, h) for h in event_heats]

        hydrated.append({
            **item,
            'heats': detail_heats,
        })
    return hydrated


def _serialize_heat_detail(tournament: Tournament, event: Event, heat: Heat) -> dict:
    assignments = heat.get_stand_assignments()
    comp_lookup = _load_competitor_lookup(event, heat.get_competitors())
    competitors = []
    for comp_id in heat.get_competitors():
        comp = comp_lookup.get(comp_id)
        competitors.append({
            'name': comp.name if comp else f'Unknown ({comp_id})',
            'stand': assignments.get(str(comp_id)),
        })
    return {
        'heat_id': heat.id,
        'heat_number': heat.heat_number,
        'run_number': heat.run_number,
        'competitors': competitors,
    }


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
