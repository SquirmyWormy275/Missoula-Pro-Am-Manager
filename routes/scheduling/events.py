"""
Event configuration routes: event_list, setup_events, day_schedule, apply_saturday_priority,
and all event-setup helper functions.
"""
import json
import os as _os
import re

from flask import flash, redirect, render_template, request, session, url_for

import config
import strings as text
from database import db
from models import Event, Flight, Heat, HeatAssignment, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.audit import log_action

from . import (
    _build_assignment_details,
    _build_pro_flights_if_possible,
    _build_signup_rows,
    _competitor_entered_event,
    _generate_all_heats,
    _is_list_only_event,
    _load_competitor_lookup,
    _normalize_name,
    _resolve_partner_name,
    _signed_up_competitors,
    scheduling_bp,
)


def _snapshot_flights(tournament_id: int) -> dict:
    """Capture per-flight heat counts for the build-diff modal."""
    snapshot = {}
    for fl in Flight.query.filter_by(tournament_id=tournament_id).all():
        snapshot[fl.flight_number] = len(fl.get_heats_ordered())
    return snapshot


def _handle_event_list_post(tournament, saturday_college_event_ids, generate_event_heats, build_pro_flights, integrate_college_spillover_into_flights):
    """Handle POST actions for event_list: generate_all, rebuild_flights, integrate_spillover.

    Wraps each multi-step operation in a try/except so a failure in one step rolls
    back that step without corrupting the whole session (heat generation already
    commits per-event; flight building commits at the end of build_pro_flights).
    """
    action = request.form.get('action', '')
    tournament_id = tournament.id

    if action == 'generate_all':
        try:
            _generate_all_heats(tournament, generate_event_heats)
            pre_snap = _snapshot_flights(tournament_id)
            flights = _build_pro_flights_if_possible(tournament, build_pro_flights)
            if flights is not None:
                flash(f'Built {flights} pro flight(s).', 'success')
                integration = integrate_college_spillover_into_flights(tournament, saturday_college_event_ids)
                if integration['integrated_heats'] > 0:
                    db.session.commit()
                    flash(f"Integrated {integration['integrated_heats']} college spillover heat(s) into Saturday flights.", 'success')
                post_snap = _snapshot_flights(tournament_id)
                session[f'build_diff_{tournament_id}'] = {
                    'before_flight_count': len(pre_snap),
                    'after_flight_count': len(post_snap),
                    'total_heats': sum(post_snap.values()),
                }
                session.modified = True
        except Exception as exc:
            db.session.rollback()
            flash(f'Heat/flight generation failed and was rolled back: {exc}', 'error')

    elif action == 'rebuild_flights':
        try:
            pre_snap = _snapshot_flights(tournament_id)
            flights = _build_pro_flights_if_possible(tournament, build_pro_flights)
            if flights is not None:
                flash(f'Rebuilt {flights} pro flight(s).', 'success')
                integration = integrate_college_spillover_into_flights(tournament, saturday_college_event_ids)
                if integration['integrated_heats'] > 0:
                    db.session.commit()
                    flash(f"Integrated {integration['integrated_heats']} college spillover heat(s) into Saturday flights.", 'success')
                post_snap = _snapshot_flights(tournament_id)
                session[f'build_diff_{tournament_id}'] = {
                    'before_flight_count': len(pre_snap),
                    'after_flight_count': len(post_snap),
                    'total_heats': sum(post_snap.values()),
                }
                session.modified = True
        except Exception as exc:
            db.session.rollback()
            flash(f'Flight rebuild failed and was rolled back: {exc}', 'error')

    elif action == 'integrate_spillover':
        try:
            integration = integrate_college_spillover_into_flights(tournament, saturday_college_event_ids)
            db.session.commit()
            flash(integration['message'], 'info')
            if integration['integrated_heats'] > 0:
                flash(f"Integrated {integration['integrated_heats']} heat(s) into flights.", 'success')
        except Exception as exc:
            db.session.rollback()
            flash(f'Spillover integration failed: {exc}', 'error')


@scheduling_bp.route('/<int:tournament_id>/events', methods=['GET', 'POST'])
def event_list(tournament_id):
    """Unified Events & Schedule page — heat status, schedule options, generation actions."""
    from services.flight_builder import build_pro_flights, integrate_college_spillover_into_flights
    from services.heat_generator import generate_event_heats

    tournament = Tournament.query.get_or_404(tournament_id)
    session_key = f'schedule_options_{tournament_id}'

    all_pro = tournament.events.filter_by(event_type='pro').order_by(Event.name, Event.gender).all()
    all_college = tournament.events.filter_by(event_type='college').all()
    # Load schedule config: prefer DB (persists across sessions), fall back to session
    db_config = tournament.get_schedule_config()
    saved = db_config if db_config else session.get(session_key, {})

    # ── POST: dispatch to action handler ─────────────────────────────────
    if request.method == 'POST':
        saturday_college_event_ids = [int(i) for i in saved.get('saturday_college_event_ids', [])]
        _handle_event_list_post(
            tournament, saturday_college_event_ids,
            generate_event_heats, build_pro_flights,
            integrate_college_spillover_into_flights,
        )
        return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))

    # ── Normalise saved options ───────────────────────────────────────────
    saved = {
        'friday_pro_event_ids': [int(i) for i in saved.get('friday_pro_event_ids', [])],
        'saturday_college_event_ids': [int(i) for i in saved.get('saturday_college_event_ids', [])],
    }

    # ── Event data ────────────────────────────────────────────────────────
    college_events = all_college
    pro_events = all_pro
    all_events = college_events + pro_events
    entrant_counts = {e.id: len(_signed_up_competitors(e)) for e in all_events}
    event_progress = {e.id: _build_event_progress(e, entrant_counts[e.id]) for e in all_events}

    college_closed = [e for e in college_events if not _is_list_only_event(e)]
    college_with_heats = sum(1 for e in college_closed if event_progress[e.id]['heat_count'] > 0)
    college_heats_total = len(college_closed)

    flights_built = Flight.query.join(Heat).join(Event).filter(
        Event.tournament_id == tournament_id,
        Event.event_type == 'pro'
    ).count() > 0
    pro_heats_exist = any(event_progress[e.id]['heat_count'] > 0 for e in pro_events)

    # ── Saturday spillover config summary ────────────────────────────────
    sat_spillover_count = len(saved['saturday_college_event_ids'])
    fnf_count = len(saved['friday_pro_event_ids'])

    build_diff = session.pop(f'build_diff_{tournament_id}', None)
    if build_diff:
        session.modified = True

    return render_template('scheduling/events.html',
                           tournament=tournament,
                           college_events=college_events,
                           pro_events=pro_events,
                           entrant_counts=entrant_counts,
                           event_progress=event_progress,
                           college_with_heats=college_with_heats,
                           college_heats_total=college_heats_total,
                           flights_built=flights_built,
                           pro_heats_exist=pro_heats_exist,
                           sat_spillover_count=sat_spillover_count,
                           fnf_count=fnf_count,
                           build_diff=build_diff)


@scheduling_bp.route('/<int:tournament_id>/events/setup', methods=['GET', 'POST'])
def setup_events(tournament_id):
    """Configure events for the tournament."""
    tournament = Tournament.query.get_or_404(tournament_id)
    college_open_events = [_with_field_key(e) for e in config.COLLEGE_OPEN_EVENTS]
    college_closed_events = [_with_field_key(e) for e in config.COLLEGE_CLOSED_EVENTS]
    pro_events = [_with_field_key(e) for e in config.PRO_EVENTS]

    if request.method == 'POST':
        action_scope = request.form.get('action_scope', 'both')  # 'college', 'pro', or 'both'

        if action_scope in {'college', 'both'}:
            skipped_college = _create_college_events(tournament, request.form, college_open_events, college_closed_events)
            if skipped_college:
                flash(
                    f'Skipped removing {skipped_college} college event(s) because heats/results already exist.',
                    'warning'
                )
        if action_scope in {'pro', 'both'}:
            skipped_pro = _create_pro_events(tournament, request.form, pro_events)
            if skipped_pro:
                flash(
                    f'Skipped removing {skipped_pro} pro event(s) because heats/results already exist.',
                    'warning'
                )

        db.session.commit()
        if action_scope == 'college':
            flash('College event configuration saved.', 'success')
        elif action_scope == 'pro':
            flash('Pro event configuration saved.', 'success')
        else:
            flash('College and pro event configurations saved.', 'success')
        if request.form.get('return_to') == 'setup':
            return redirect(url_for('main.tournament_setup', tournament_id=tournament_id, tab='events'))
        return redirect(url_for('scheduling.setup_events', tournament_id=tournament_id))

    existing_config = _get_existing_event_config(tournament)

    return render_template('scheduling/setup_events.html',
                           tournament=tournament,
                           college_open_events=college_open_events,
                           college_closed_events=college_closed_events,
                           pro_events=pro_events,
                           existing_config=existing_config,
                           stand_configs=config.STAND_CONFIGS)


def _parse_stand_overrides(form_data):
    """Extract stands_{stand_type} overrides from form data. Returns dict of stand_type -> int."""
    overrides = {}
    for stand_type in config.STAND_CONFIGS:
        raw = form_data.get(f'stands_{stand_type}')
        if raw:
            try:
                val = int(raw)
                if val >= 1:
                    overrides[stand_type] = val
            except (TypeError, ValueError):
                pass
    return overrides


def _create_college_events(tournament, form_data, college_open_events, college_closed_events):
    """Create/update college events based on form configuration and remove deselected events."""
    selected_signatures = set()
    stand_overrides = _parse_stand_overrides(form_data)

    # Process OPEN events (check if each should be treated as CLOSED)
    for event_config in college_open_events:
        # Check if this event should be treated as CLOSED
        is_open = form_data.get(f"open_{event_config['field_key']}", 'open') == 'open'
        max_stands_override = stand_overrides.get(event_config.get('stand_type'))

        # Create gendered versions if applicable
        if event_config.get('is_partnered') and event_config.get('partner_gender') == 'mixed':
            # Mixed gender events are not gendered
            event = _upsert_event(tournament, event_config, 'college', None, is_open, max_stands_override)
            selected_signatures.add(_event_signature(event.name, event.event_type, event.gender))
        else:
            # Create men's and women's versions
            event_m = _upsert_event(tournament, event_config, 'college', 'M', is_open, max_stands_override)
            event_f = _upsert_event(tournament, event_config, 'college', 'F', is_open, max_stands_override)
            selected_signatures.add(_event_signature(event_m.name, event_m.event_type, event_m.gender))
            selected_signatures.add(_event_signature(event_f.name, event_f.event_type, event_f.gender))

    # Process CLOSED events
    for event_config in college_closed_events:
        if form_data.get(f"enable_{event_config['field_key']}") != 'on':
            continue

        max_stands_override = stand_overrides.get(event_config.get('stand_type'))
        is_handicap = (
            form_data.get(f"handicap_format_{event_config['field_key']}", 'championship') == 'handicap'
            if event_config.get('stand_type') in config.HANDICAP_ELIGIBLE_STAND_TYPES
            and event_config.get('scoring_type') != 'hits'
            else False
        )
        if event_config.get('is_gendered', True):
            # Create men's and women's versions
            event_m = _upsert_event(tournament, event_config, 'college', 'M', False, max_stands_override, is_handicap)
            event_f = _upsert_event(tournament, event_config, 'college', 'F', False, max_stands_override, is_handicap)
            selected_signatures.add(_event_signature(event_m.name, event_m.event_type, event_m.gender))
            selected_signatures.add(_event_signature(event_f.name, event_f.event_type, event_f.gender))
        else:
            event = _upsert_event(tournament, event_config, 'college', None, False, max_stands_override, is_handicap)
            selected_signatures.add(_event_signature(event.name, event.event_type, event.gender))

    return _remove_deselected_events(tournament, 'college', selected_signatures)


def _create_pro_events(tournament, form_data, pro_events):
    """Create/update pro events based on form configuration and remove deselected events."""
    selected_signatures = set()
    stand_overrides = _parse_stand_overrides(form_data)

    for event_config in pro_events:
        # Check if this event is enabled
        if form_data.get(f"enable_{event_config['field_key']}") != 'on':
            continue

        max_stands_override = stand_overrides.get(event_config.get('stand_type'))
        is_handicap = (
            form_data.get(f"handicap_format_{event_config['field_key']}", 'championship') == 'handicap'
            if event_config.get('stand_type') in config.HANDICAP_ELIGIBLE_STAND_TYPES
            and event_config.get('scoring_type') != 'hits'
            else False
        )
        if event_config.get('is_gendered', False):
            # Check which genders are enabled
            if form_data.get(f"enable_{event_config['field_key']}_M") == 'on':
                event_m = _upsert_event(tournament, event_config, 'pro', 'M', False, max_stands_override, is_handicap)
                selected_signatures.add(_event_signature(event_m.name, event_m.event_type, event_m.gender))
            if form_data.get(f"enable_{event_config['field_key']}_F") == 'on':
                event_f = _upsert_event(tournament, event_config, 'pro', 'F', False, max_stands_override, is_handicap)
                selected_signatures.add(_event_signature(event_f.name, event_f.event_type, event_f.gender))
        else:
            event = _upsert_event(tournament, event_config, 'pro', None, False, max_stands_override, is_handicap)
            selected_signatures.add(_event_signature(event.name, event.event_type, event.gender))

    return _remove_deselected_events(tournament, 'pro', selected_signatures)


def _upsert_event(tournament, event_config, event_type, gender, is_open, max_stands_override=None, is_handicap=False):
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
    event.scoring_order = 'highest_wins' if event_config['scoring_type'] in ['score', 'distance', 'hits'] else 'lowest_wins'
    event.is_open = is_open
    event.is_partnered = event_config.get('is_partnered', False)
    event.partner_gender_requirement = event_config.get('partner_gender')
    event.requires_dual_runs = event_config.get('requires_dual_runs', False)
    event.stand_type = event_config.get('stand_type')
    event.max_stands = max_stands_override if max_stands_override is not None else stand_config.get('total')
    event.has_prelims = event_config.get('has_prelims', False)
    event.is_handicap = is_handicap

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


def _normalize_person_name_local(value: str) -> str:
    return str(value or '').strip().lower()


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

    # Handicap vs. Championship state for eligible college CLOSED events
    college_handicap = {}
    for cfg in config.COLLEGE_CLOSED_EVENTS:
        if cfg.get('stand_type') not in config.HANDICAP_ELIGIBLE_STAND_TYPES:
            continue
        if cfg.get('scoring_type') == 'hits':
            continue
        key = cfg['name']
        matching = [e for e in events if e.event_type == 'college' and e.name == key]
        college_handicap[key] = matching[0].is_handicap if matching else False

    # Handicap vs. Championship state for eligible pro events
    pro_handicap = {}
    for cfg in config.PRO_EVENTS:
        if cfg.get('stand_type') not in config.HANDICAP_ELIGIBLE_STAND_TYPES:
            continue
        if cfg.get('scoring_type') == 'hits':
            continue
        key = cfg['name']
        matching = [e for e in events if e.event_type == 'pro' and e.name == key]
        pro_handicap[key] = matching[0].is_handicap if matching else False

    # Per-stand-type count overrides stored on existing events
    stand_counts = {}
    for event in events:
        if event.stand_type and event.max_stands is not None:
            stand_counts[event.stand_type] = event.max_stands

    return {
        'college_open_state': open_state,
        'college_closed_enabled': closed_enabled,
        'pro_enabled': pro_enabled,
        'pro_gender': pro_gender,
        'college_handicap': college_handicap,
        'pro_handicap': pro_handicap,
        'stand_counts': stand_counts,
    }


@scheduling_bp.route('/<int:tournament_id>/day-schedule', methods=['GET', 'POST'])
def day_schedule(tournament_id):
    """Redirects to the unified Events & Schedule page."""
    return redirect(url_for('scheduling.event_list', tournament_id=tournament_id), 301)


# ---------------------------------------------------------------------------
# Manual event ordering — drag-and-drop endpoints
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/events/reorder-friday', methods=['POST'])
def reorder_friday_events(tournament_id):
    """Save custom Friday event display order. Expects JSON {event_ids: [int, ...]}."""
    from flask import jsonify
    tournament = Tournament.query.get_or_404(tournament_id)
    try:
        data = request.get_json(force=True)
        event_ids = [int(eid) for eid in data.get('event_ids', [])]
    except (TypeError, ValueError, AttributeError):
        return jsonify({'ok': False, 'error': 'Invalid event_ids'}), 400

    cfg = tournament.get_schedule_config()
    cfg['friday_event_order'] = event_ids
    tournament.set_schedule_config(cfg)
    db.session.commit()
    log_action('friday_event_order_set', 'tournament', tournament_id, {'order': event_ids})
    return jsonify({'ok': True})


@scheduling_bp.route('/<int:tournament_id>/events/reorder-saturday', methods=['POST'])
def reorder_saturday_events(tournament_id):
    """Save custom Saturday event display order (fallback mode). Expects JSON {event_ids: [int, ...]}."""
    from flask import jsonify
    tournament = Tournament.query.get_or_404(tournament_id)
    try:
        data = request.get_json(force=True)
        event_ids = [int(eid) for eid in data.get('event_ids', [])]
    except (TypeError, ValueError, AttributeError):
        return jsonify({'ok': False, 'error': 'Invalid event_ids'}), 400

    cfg = tournament.get_schedule_config()
    cfg['saturday_event_order'] = event_ids
    tournament.set_schedule_config(cfg)
    db.session.commit()
    log_action('saturday_event_order_set', 'tournament', tournament_id, {'order': event_ids})
    return jsonify({'ok': True})


@scheduling_bp.route('/<int:tournament_id>/events/reset-order', methods=['POST'])
def reset_event_order(tournament_id):
    """Remove custom event ordering, reverting to config defaults."""
    from flask import jsonify
    tournament = Tournament.query.get_or_404(tournament_id)
    cfg = tournament.get_schedule_config()
    cfg.pop('friday_event_order', None)
    cfg.pop('saturday_event_order', None)
    tournament.set_schedule_config(cfg)
    db.session.commit()
    log_action('event_order_reset', 'tournament', tournament_id, {})
    return jsonify({'ok': True})


def _day_schedule_legacy(tournament_id):
    """Legacy day-schedule logic — kept for reference, no longer routed."""
    from services.flight_builder import build_pro_flights, integrate_college_spillover_into_flights
    from services.heat_generator import generate_event_heats
    from services.schedule_builder import COLLEGE_SATURDAY_PRIORITY, build_day_schedule

    tournament = Tournament.query.get_or_404(tournament_id)
    friday_feature_names = set(config.FRIDAY_NIGHT_EVENTS)
    pro_events = [
        event for event in tournament.events.filter_by(event_type='pro').order_by(Event.name, Event.gender).all()
        if event.name in friday_feature_names
    ]

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
        # Persist to DB so config survives session expiry
        tournament.set_schedule_config(saved)
        db.session.commit()
        if action == 'generate_schedule':
            integration = integrate_college_spillover_into_flights(tournament, saved['saturday_college_event_ids'])
            if integration['integrated_heats'] > 0:
                db.session.commit()
                flash(
                    f"Integrated {integration['integrated_heats']} college spillover heat(s) into Saturday flights.",
                    'success'
                )
        elif action == 'generate_all':
            _generate_all_heats(tournament, generate_event_heats)
            flights = _build_pro_flights_if_possible(tournament, build_pro_flights)
            if flights is not None:
                flash(f'Built {flights} pro flight(s).', 'success')
                integration = integrate_college_spillover_into_flights(tournament, saved['saturday_college_event_ids'])
                if integration['integrated_heats'] > 0:
                    db.session.commit()
                    flash(
                        f"Integrated {integration['integrated_heats']} college spillover heat(s) into Saturday flights.",
                        'success'
                    )
        elif action == 'rebuild_flights':
            flights = _build_pro_flights_if_possible(tournament, build_pro_flights)
            if flights is not None:
                flash(f'Rebuilt {flights} pro flight(s).', 'success')
                integration = integrate_college_spillover_into_flights(tournament, saved['saturday_college_event_ids'])
                if integration['integrated_heats'] > 0:
                    db.session.commit()
                    flash(
                        f"Integrated {integration['integrated_heats']} college spillover heat(s) into Saturday flights.",
                        'success'
                    )
        elif action == 'integrate_spillover':
            integration = integrate_college_spillover_into_flights(tournament, saved['saturday_college_event_ids'])
            db.session.commit()
            flash(integration['message'], 'info')
            if integration['integrated_heats'] > 0:
                flash(f"Integrated {integration['integrated_heats']} heat(s) into flights.", 'success')
    else:
        saved = {
            'friday_pro_event_ids': [int(eid) for eid in saved.get('friday_pro_event_ids', [])],
            'saturday_college_event_ids': [int(eid) for eid in saved.get('saturday_college_event_ids', [])],
        }

    from .heat_sheets import _hydrate_schedule_for_display
    schedule = build_day_schedule(
        tournament,
        friday_pro_event_ids=saved['friday_pro_event_ids'],
        saturday_college_event_ids=saved['saturday_college_event_ids']
    )
    has_schedule_overrides = bool(saved['friday_pro_event_ids'] or saved['saturday_college_event_ids'])
    detailed_schedule = _hydrate_schedule_for_display(tournament, schedule)

    return render_template(
        'scheduling/day_schedule.html',
        tournament=tournament,
        pro_events=pro_events,
        college_sat_options=college_sat_options,
        selected_friday_pro_event_ids=saved['friday_pro_event_ids'],
        selected_saturday_college_event_ids=saved['saturday_college_event_ids'],
        has_schedule_overrides=has_schedule_overrides,
        schedule=schedule,
        detailed_schedule=detailed_schedule
    )


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


# ---------------------------------------------------------------------------
# #15 — College Saturday priority ordering
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/college/saturday-priority', methods=['POST'])
def apply_saturday_priority(tournament_id):
    """Re-number college event heats so COLLEGE_SATURDAY_PRIORITY_DEFAULT events run first."""
    import json as _json
    tournament = Tournament.query.get_or_404(tournament_id)

    # Load override file if present, else use default
    order_path = _os.path.join('instance', f'saturday_priority_{tournament_id}.json')
    if _os.path.exists(order_path):
        try:
            with open(order_path) as f:
                priority_tuples = [tuple(pair) for pair in _json.load(f)]
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
