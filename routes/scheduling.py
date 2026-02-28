"""
Scheduling routes for heat and flight generation.
"""
import re
import json
from flask import Blueprint, render_template, redirect, url_for, flash, request, session, abort
from database import db
from models import Tournament, Event, Heat, HeatAssignment, Flight
from models.competitor import CollegeCompetitor, ProCompetitor
import config
import strings as text
from services.audit import log_action
from services.background_jobs import submit as submit_job

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
    all_events = college_events + pro_events
    assignment_details = _build_assignment_details(tournament, college_events + pro_events)
    entrant_counts = {
        event.id: len(_signed_up_competitors(event))
        for event in all_events
    }
    event_progress = {
        event.id: _build_event_progress(event, entrant_counts[event.id])
        for event in all_events
    }

    return render_template('scheduling/events.html',
                           tournament=tournament,
                           college_events=college_events,
                           pro_events=pro_events,
                           assignment_details=assignment_details,
                           entrant_counts=entrant_counts,
                           event_progress=event_progress)


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
        try:
            friday_pro_event_ids = [int(eid) for eid in request.form.getlist('friday_pro_event_ids') if str(eid).strip()]
            saturday_college_event_ids = [int(eid) for eid in request.form.getlist('saturday_college_event_ids') if str(eid).strip()]
        except (TypeError, ValueError):
            flash('Invalid event ID in schedule submission.', 'error')
            return redirect(url_for('scheduling.day_schedule', tournament_id=tournament_id))
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
        elif action == 'rebuild_flights':
            flights = _build_pro_flights_if_possible(tournament, build_pro_flights)
            if flights is not None:
                flash(f'Rebuilt {flights} pro flight(s).', 'success')
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
    if event.tournament_id != tournament.id:
        abort(404)

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
    if event.tournament_id != tournament_id:
        abort(404)

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


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/move-competitor', methods=['POST'])
def move_competitor_between_heats(tournament_id, event_id):
    """Move a competitor between heats (and mirrored dual run heat, if needed)."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    try:
        competitor_id = int(request.form.get('competitor_id', ''))
        from_heat_id = int(request.form.get('from_heat_id', ''))
        to_heat_id = int(request.form.get('to_heat_id', ''))
    except (TypeError, ValueError):
        flash('Invalid move request.', 'error')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    from_heat = Heat.query.get_or_404(from_heat_id)
    to_heat = Heat.query.get_or_404(to_heat_id)
    if from_heat.event_id != event.id or to_heat.event_id != event.id:
        abort(404)
    if from_heat.id == to_heat.id:
        flash('Select a different destination heat.', 'warning')
        return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))

    run_numbers = [1, 2] if event.requires_dual_runs else [from_heat.run_number]
    from_pairs = []
    to_pairs = []
    for run_number in run_numbers:
        source = event.heats.filter_by(heat_number=from_heat.heat_number, run_number=run_number).first()
        target = event.heats.filter_by(heat_number=to_heat.heat_number, run_number=run_number).first()
        if not source or not target:
            flash('Could not find matching source/destination heats for move.', 'error')
            return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))
        from_pairs.append(source)
        to_pairs.append(target)

    for source, target in zip(from_pairs, to_pairs):
        source_ids = source.get_competitors()
        if competitor_id not in source_ids:
            flash('Competitor is not in the selected source heat.', 'error')
            return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))
        target_ids = target.get_competitors()
        if competitor_id in target_ids:
            continue
        source.remove_competitor(competitor_id)
        target.add_competitor(competitor_id)

        source_assignments = source.get_stand_assignments()
        source_assignments.pop(str(competitor_id), None)
        source.stand_assignments = json.dumps(source_assignments)

        target_assignments = target.get_stand_assignments()
        target_assignments[str(competitor_id)] = _next_open_stand(target_ids, target_assignments, event)
        target.stand_assignments = json.dumps(target_assignments)

    db.session.commit()
    flash('Competitor moved successfully.', 'success')
    return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))


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


def _build_event_progress(event: Event, entrant_count: int) -> dict:
    """Build progress metrics for event list tables."""
    heat_count = event.heats.count()
    completed_heats = event.heats.filter_by(status='completed').count()
    results_completed = event.results.filter_by(status='completed').count()
    heat_pct = int((completed_heats / heat_count) * 100) if heat_count else 0
    result_pct = int((results_completed / entrant_count) * 100) if entrant_count else 0
    ready_to_finalize = entrant_count > 0 and results_completed >= entrant_count
    return {
        'heat_count': heat_count,
        'completed_heats': completed_heats,
        'heat_pct': heat_pct,
        'results_completed': results_completed,
        'result_pct': result_pct,
        'ready_to_finalize': ready_to_finalize,
    }


def _next_open_stand(target_ids: list[int], assignments: dict, event: Event) -> int | None:
    """Return next available stand number for a target heat."""
    stand_config = config.STAND_CONFIGS.get(event.stand_type or '', {})
    total = stand_config.get('total', max(len(target_ids), 1))
    if event.stand_type == 'saw_hand':
        total = min(total, 4)
    if event.event_type == 'college' and _normalize_name(event.name) == _normalize_name('Stock Saw'):
        available = [7, 8]
    elif stand_config.get('specific_stands'):
        available = list(stand_config['specific_stands'])
    else:
        available = list(range(1, total + 1))
    used = {int(v) for v in assignments.values() if str(v).isdigit()}
    for stand in available:
        if stand not in used:
            return stand
    return available[0] if available else None


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

        if request.form.get('run_async') == '1':
            job_id = submit_job('build_pro_flights', build_pro_flights, tournament)
            log_action('flight_build_job_started', 'tournament', tournament_id, {'job_id': job_id})
            db.session.commit()
            flash('Flight build started in the background.', 'success')
            return redirect(url_for('reporting.export_results_job_status', tournament_id=tournament_id, job_id=job_id))

        try:
            num_flights = build_pro_flights(tournament)
            log_action('flights_built', 'tournament', tournament_id, {'count': num_flights})
            db.session.commit()
            flash(text.FLASH['flights_built'].format(num_flights=num_flights), 'success')
        except Exception as e:
            flash(text.FLASH['flights_error'].format(error=str(e)), 'error')

        return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))

    # Get available heats
    pro_events = tournament.events.filter_by(event_type='pro').all()

    return render_template('pro/build_flights.html',
                           tournament=tournament,
                           events=pro_events)


# ---------------------------------------------------------------------------
# Friday Night Feature scheduling
# ---------------------------------------------------------------------------

def _fnf_config_path(tournament_id: int) -> str:
    """Return path to the per-tournament Friday Night Feature JSON config."""
    import os
    instance_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance')
    os.makedirs(instance_dir, exist_ok=True)
    return os.path.join(instance_dir, f'friday_feature_{tournament_id}.json')


def _load_fnf_config(tournament_id: int) -> dict:
    """Load persisted Friday Night Feature selections for a tournament."""
    import os
    path = _fnf_config_path(tournament_id)
    if not os.path.exists(path):
        return {'event_ids': [], 'notes': ''}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'event_ids': [], 'notes': ''}


def _save_fnf_config(tournament_id: int, data: dict) -> None:
    """Persist Friday Night Feature selections."""
    path = _fnf_config_path(tournament_id)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)


@scheduling_bp.route('/<int:tournament_id>/friday-night', methods=['GET', 'POST'])
def friday_feature(tournament_id):
    """Configure and view Friday Night Feature events."""
    tournament = Tournament.query.get_or_404(tournament_id)

    # Eligible events: pro events whose name matches FRIDAY_NIGHT_EVENTS candidates
    eligible_names = set(config.FRIDAY_NIGHT_EVENTS)
    pro_events = tournament.events.filter_by(event_type='pro').order_by(Event.name, Event.gender).all()
    eligible_events = [e for e in pro_events if e.name in eligible_names]

    fnf_config = _load_fnf_config(tournament_id)

    if request.method == 'POST':
        selected_ids = [int(x) for x in request.form.getlist('event_ids') if x.isdigit()]
        notes = (request.form.get('notes') or '').strip()

        # Update tournament Friday Night Feature date if provided
        date_str = (request.form.get('friday_feature_date') or '').strip()
        if date_str:
            from datetime import date as date_type
            try:
                yr, mo, dy = (int(p) for p in date_str.split('-'))
                tournament.friday_feature_date = date_type(yr, mo, dy)
                db.session.commit()
            except (TypeError, ValueError):
                flash('Invalid date format. Use YYYY-MM-DD.', 'error')

        _save_fnf_config(tournament_id, {'event_ids': selected_ids, 'notes': notes})
        log_action('friday_feature_configured', 'tournament', tournament_id, {
            'event_count': len(selected_ids),
        })
        db.session.commit()
        flash('Friday Night Feature schedule saved.', 'success')
        return redirect(url_for('scheduling.friday_feature', tournament_id=tournament_id))

    return render_template(
        'scheduling/friday_feature.html',
        tournament=tournament,
        eligible_events=eligible_events,
        selected_ids=set(fnf_config.get('event_ids', [])),
        notes=fnf_config.get('notes', ''),
    )


# ---------------------------------------------------------------------------
# #19 — HeatAssignment sync check / fix
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/heats/sync-check')
def heat_sync_check(tournament_id, event_id):
    """Return JSON showing mismatches between Heat.competitors JSON and HeatAssignment rows."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    mismatches = []
    for heat in event.heats.order_by(Heat.heat_number, Heat.run_number).all():
        json_ids = set(heat.get_competitors())
        table_ids = set(
            a.competitor_id
            for a in HeatAssignment.query.filter_by(heat_id=heat.id).all()
        )
        if json_ids != table_ids:
            mismatches.append({
                'heat_id': heat.id,
                'heat_number': heat.heat_number,
                'run_number': heat.run_number,
                'json_only': sorted(json_ids - table_ids),
                'table_only': sorted(table_ids - json_ids),
            })

    from flask import jsonify
    return jsonify({'event_id': event_id, 'mismatches': mismatches, 'ok': len(mismatches) == 0})


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/heats/sync-fix', methods=['POST'])
def heat_sync_fix(tournament_id, event_id):
    """Reconcile HeatAssignment rows to match authoritative Heat.competitors JSON."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    fixed = 0
    for heat in event.heats.all():
        json_ids = heat.get_competitors()
        HeatAssignment.query.filter_by(heat_id=heat.id).delete()
        comp_type = event.event_type  # 'pro' or 'college'
        assignments = heat.get_stand_assignments()
        for comp_id in json_ids:
            ha = HeatAssignment(
                heat_id=heat.id,
                competitor_id=comp_id,
                competitor_type=comp_type,
                stand_number=assignments.get(str(comp_id)),
            )
            db.session.add(ha)
        fixed += 1

    db.session.commit()
    log_action('heat_assignments_synced', 'event', event_id, {'heats_fixed': fixed})
    flash(f'HeatAssignment table synced for {fixed} heats.', 'success')
    return redirect(url_for('scheduling.event_heats', tournament_id=tournament_id, event_id=event_id))


# ---------------------------------------------------------------------------
# #7 — Heat sheet print page
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/heat-sheets')
def heat_sheets(tournament_id):
    """Print-ready heat sheets for all flights and events."""
    tournament = Tournament.query.get_or_404(tournament_id)

    # Build ordered heat data: flights first, then ungrouped events
    flights = Flight.query.filter_by(tournament_id=tournament_id).order_by(Flight.flight_number).all()

    flight_data = []
    for flight in flights:
        heats_in_flight = flight.heats.order_by(Heat.id).all()
        heat_rows = []
        for heat in heats_in_flight:
            comp_ids = heat.get_competitors()
            assignments = heat.get_stand_assignments()
            event = Event.query.get(heat.event_id)
            if not event:
                continue
            if event.event_type == 'college':
                from models.competitor import CollegeCompetitor
                comps = {c.id: c for c in CollegeCompetitor.query.filter(
                    CollegeCompetitor.id.in_(comp_ids)).all()} if comp_ids else {}
            else:
                from models.competitor import ProCompetitor
                comps = {c.id: c for c in ProCompetitor.query.filter(
                    ProCompetitor.id.in_(comp_ids)).all()} if comp_ids else {}
            heat_rows.append({
                'heat': heat,
                'event': event,
                'competitors': [
                    {'name': comps[cid].name if cid in comps else f'ID:{cid}',
                     'stand': assignments.get(str(cid), '?')}
                    for cid in comp_ids
                ],
            })
        if heat_rows:
            flight_data.append({'flight': flight, 'heats': heat_rows})

    # Also gather heats with no flight (college events, standalone)
    no_flight_heats = []
    for event in tournament.events.order_by(Event.event_type, Event.name).all():
        event_heats = event.heats.filter_by(flight_id=None).order_by(
            Heat.heat_number, Heat.run_number).all()
        if not event_heats:
            continue
        heat_rows = []
        for heat in event_heats:
            comp_ids = heat.get_competitors()
            assignments = heat.get_stand_assignments()
            if event.event_type == 'college':
                from models.competitor import CollegeCompetitor
                comps = {c.id: c for c in CollegeCompetitor.query.filter(
                    CollegeCompetitor.id.in_(comp_ids)).all()} if comp_ids else {}
            else:
                from models.competitor import ProCompetitor
                comps = {c.id: c for c in ProCompetitor.query.filter(
                    ProCompetitor.id.in_(comp_ids)).all()} if comp_ids else {}
            heat_rows.append({
                'heat': heat,
                'event': event,
                'competitors': [
                    {'name': comps[cid].name if cid in comps else f'ID:{cid}',
                     'stand': assignments.get(str(cid), '?')}
                    for cid in comp_ids
                ],
            })
        no_flight_heats.append({'event': event, 'heats': heat_rows})

    return render_template(
        'scheduling/heat_sheets_print.html',
        tournament=tournament,
        flight_data=flight_data,
        no_flight_heats=no_flight_heats,
    )


# ---------------------------------------------------------------------------
# #15 — College Saturday priority ordering
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/college/saturday-priority', methods=['POST'])
def apply_saturday_priority(tournament_id):
    """Re-number college event heats so COLLEGE_SATURDAY_PRIORITY_DEFAULT events run first."""
    tournament = Tournament.query.get_or_404(tournament_id)

    # Load override file if present, else use default
    import os as _os
    order_path = _os.path.join('instance', f'saturday_priority_{tournament_id}.json')
    if _os.path.exists(order_path):
        try:
            with open(order_path) as f:
                priority_tuples = [tuple(pair) for pair in json.load(f)]
        except Exception:
            priority_tuples = list(config.COLLEGE_SATURDAY_PRIORITY_DEFAULT)
    else:
        priority_tuples = list(config.COLLEGE_SATURDAY_PRIORITY_DEFAULT)

    reordered = 0
    for event_name, gender in priority_tuples:
        matching = tournament.events.filter_by(
            event_type='college',
            name=event_name,
            gender=gender,
        ).all()
        for event in matching:
            # Assign heat_number starting from 1 in existing order
            heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
            for i, heat in enumerate(heats, start=1):
                heat.heat_number = i
            reordered += len(heats)

    db.session.commit()
    log_action('saturday_priority_applied', 'tournament', tournament_id, {
        'priority_count': len(priority_tuples),
    })
    flash(f'Saturday priority applied to {reordered} heats across {len(priority_tuples)} event(s).', 'success')
    return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))


# ---------------------------------------------------------------------------
# #2 — Flight start + SMS notification trigger
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/flights/<int:flight_id>/start', methods=['POST'])
def start_flight(tournament_id, flight_id):
    """Mark a flight as in_progress and send SMS to competitors in upcoming flights."""
    tournament = Tournament.query.get_or_404(tournament_id)
    flight = Flight.query.filter_by(id=flight_id, tournament_id=tournament_id).first_or_404()

    if flight.status == 'in_progress':
        flash(f'Flight {flight.flight_number} is already in progress.', 'warning')
        return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))

    flight.status = 'in_progress'

    # Notify competitors in flights SMS_NOTIFY_FLIGHTS_AHEAD ahead
    _send_upcoming_heat_sms(tournament_id, flight.flight_number)

    log_action('flight_started', 'flight', flight_id, {
        'tournament_id': tournament_id,
        'flight_number': flight.flight_number,
    })
    db.session.commit()
    flash(f'Flight {flight.flight_number} marked as in progress.', 'success')
    return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))


@scheduling_bp.route('/<int:tournament_id>/flights/<int:flight_id>/complete', methods=['POST'])
def complete_flight(tournament_id, flight_id):
    """Mark a flight as completed."""
    flight = Flight.query.filter_by(id=flight_id, tournament_id=tournament_id).first_or_404()
    flight.status = 'completed'
    log_action('flight_completed', 'flight', flight_id, {'tournament_id': tournament_id})
    db.session.commit()
    flash(f'Flight {flight.flight_number} marked as completed.', 'success')
    return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))


def _send_upcoming_heat_sms(tournament_id: int, current_flight_number: int) -> None:
    """Notify opted-in competitors whose flight is SMS_NOTIFY_FLIGHTS_AHEAD ahead."""
    from flask import current_app
    from services.sms_notify import send_sms, is_configured
    from services.background_jobs import submit as submit_job

    if not is_configured():
        return

    notify_ahead = current_app.config.get('SMS_NOTIFY_FLIGHTS_AHEAD', 3)
    target_flight_number = current_flight_number + notify_ahead

    target_flight = Flight.query.filter_by(
        tournament_id=tournament_id,
        flight_number=target_flight_number,
    ).first()
    if not target_flight:
        return

    competitor_ids_in_flight = set()
    competitor_type_map: dict[int, str] = {}
    for heat in target_flight.heats.all():
        event = Event.query.get(heat.event_id)
        if not event:
            continue
        for cid in heat.get_competitors():
            competitor_ids_in_flight.add(int(cid))
            competitor_type_map[int(cid)] = event.event_type

    pro_ids = [cid for cid, t in competitor_type_map.items() if t == 'pro']
    col_ids = [cid for cid, t in competitor_type_map.items() if t == 'college']

    sms_targets: list[tuple[str, str]] = []  # (phone, name)

    if pro_ids:
        pros = ProCompetitor.query.filter(
            ProCompetitor.id.in_(pro_ids),
            ProCompetitor.phone_opted_in == True,  # noqa: E712
        ).all()
        for comp in pros:
            if comp.phone:
                sms_targets.append((comp.phone, comp.name))

    if col_ids:
        colleges = CollegeCompetitor.query.filter(
            CollegeCompetitor.id.in_(col_ids),
            CollegeCompetitor.phone_opted_in == True,  # noqa: E712
        ).all()
        for comp in colleges:
            # CollegeCompetitor has no phone column — skip silently
            pass

    msg = (
        f'Heads up! Flight {target_flight_number} at the Missoula Pro-Am is '
        f'{notify_ahead} flights away. Get ready for your events!'
    )
    for phone, name in sms_targets:
        submit_job(f'sms:{name}', send_sms, phone, msg)
