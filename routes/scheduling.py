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


def _event_rank_category(event) -> str | None:
    """Return the ProEventRank category string for an event, or None if unranked."""
    if event is None:
        return None
    st = getattr(event, 'stand_type', None)
    if st == 'springboard':
        return 'springboard'
    if st == 'underhand':
        return 'underhand'
    if st == 'standing_block':
        return 'standing_block'
    if st == 'obstacle_pole':
        return 'obstacle_pole'
    if st == 'saw_hand':
        if not getattr(event, 'is_partnered', False):
            return 'singlebuck'
        pg = getattr(event, 'partner_gender', None)
        if pg == 'mixed':
            return 'jack_jill'
        return 'doublebuck'
    return None


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
    from services.heat_generator import generate_event_heats
    from services.flight_builder import build_pro_flights, integrate_college_spillover_into_flights

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
    aliases = {target_name, target_display_name}

    # Backward-compatible aliases for historic imports and form-label variants.
    if event.event_type == 'pro':
        if target_name == 'springboard':
            aliases.update({'springboardl', 'springboardr'})
        elif target_name in {'pro1board', '1boardspringboard'}:
            aliases.update({'intermediate1boardspringboard', 'pro1board', '1boardspringboard'})
        elif target_name == 'jackjillsawing':
            aliases.update({'jackjill', 'jackandjill'})
        elif target_name in {'poleclimb', 'speedclimb'}:
            aliases.update({'poleclimb', 'speedclimb'})
        elif target_name == 'partneredaxethrow':
            aliases.update({'partneredaxethrow', 'axethrow'})

    for raw in entered:
        value = str(raw).strip()
        if not value:
            continue
        if value == target_id:
            return True
        normalized = _normalize_name(value)
        if normalized in aliases:
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


def _day_schedule_legacy(tournament_id):
    """Legacy day-schedule logic — kept for reference, no longer routed."""
    from services.schedule_builder import build_day_schedule, COLLEGE_SATURDAY_PRIORITY
    from services.heat_generator import generate_event_heats
    from services.flight_builder import build_pro_flights, integrate_college_spillover_into_flights

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


@scheduling_bp.route('/<int:tournament_id>/preflight', methods=['GET', 'POST'])
def preflight_check(tournament_id):
    """Run preflight checks and offer one-click auto-fix actions."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.preflight import build_preflight_report
    from services.partner_matching import auto_assign_pro_partners
    from services.flight_builder import integrate_college_spillover_into_flights

    session_key = f'schedule_options_{tournament_id}'
    saved = session.get(session_key, {})
    saturday_ids = [int(eid) for eid in saved.get('saturday_college_event_ids', [])]

    if request.method == 'POST':
        action = request.form.get('action', 'autofix')
        if action == 'autofix':
            from services.gear_sharing import complete_one_sided_pairs, parse_all_gear_details

            # 1) Heat assignment sync for all events
            heats_fixed = 0
            for event in tournament.events.all():
                for heat in event.heats.all():
                    json_ids = heat.get_competitors()
                    HeatAssignment.query.filter_by(heat_id=heat.id).delete()
                    assignments = heat.get_stand_assignments()
                    for comp_id in json_ids:
                        db.session.add(HeatAssignment(
                            heat_id=heat.id,
                            competitor_id=comp_id,
                            competitor_type=event.event_type,
                            stand_number=assignments.get(str(comp_id)),
                        ))
                    heats_fixed += 1

            # 2) Parse unstructured gear-sharing details into structured maps
            gear_parse_result = parse_all_gear_details(tournament)

            # 3) Write reciprocals for all one-sided gear pairs
            pairs_result = complete_one_sided_pairs(tournament)

            # 4) Auto-partner assignments
            partner_summary = auto_assign_pro_partners(tournament)

            # 5) Saturday spillover integration
            integration = integrate_college_spillover_into_flights(tournament, saturday_ids)

            db.session.commit()
            log_action('preflight_autofix_applied', 'tournament', tournament_id, {
                'heats_fixed': heats_fixed,
                'gear_parsed': gear_parse_result,
                'gear_pairs_completed': pairs_result['completed'],
                'partner_summary': partner_summary,
                'spillover': integration,
            })
            gear_msg = (
                f" parsed {gear_parse_result['parsed']} gear detail(s),"
                if gear_parse_result['parsed'] else ''
            )
            pairs_msg = (
                f" completed {pairs_result['completed']} one-sided gear pair(s),"
                if pairs_result['completed'] else ''
            )
            flash(
                f"Auto-fix complete: synced {heats_fixed} heats,{gear_msg}{pairs_msg} "
                f"assigned {partner_summary['assigned_pairs']} pairs, "
                f"integrated {integration['integrated_heats']} spillover heats.",
                'success'
            )
            return redirect(url_for('scheduling.preflight_check', tournament_id=tournament_id))

    report = build_preflight_report(tournament, saturday_ids)
    return render_template(
        'scheduling/preflight.html',
        tournament=tournament,
        report=report,
        saturday_college_event_ids=saturday_ids,
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

    # Build competitor spacing heatmap data (run-1 heats only)
    spacing_data = {}
    if not signup_list_mode and heats:
        run1_heats = [h for h in heats if h.run_number == 1] or heats
        comp_appearances: dict[int, list[int]] = {}
        for h in run1_heats:
            for cid in h.get_competitors():
                comp_appearances.setdefault(int(cid), []).append(h.heat_number)
        all_cids = list(comp_appearances.keys())
        if all_cids:
            if event.event_type == 'college':
                from models.competitor import CollegeCompetitor
                name_map = {c.id: c.name for c in CollegeCompetitor.query.filter(
                    CollegeCompetitor.id.in_(all_cids)).all()}
            else:
                from models.competitor import ProCompetitor
                name_map = {c.id: c.name for c in ProCompetitor.query.filter(
                    ProCompetitor.id.in_(all_cids)).all()}
            spacing_data = {
                'total_heats': len(run1_heats),
                'competitors': sorted(
                    [{'name': name_map.get(cid, f'ID:{cid}'), 'appearances': sorted(app)}
                     for cid, app in comp_appearances.items()],
                    key=lambda x: x['name'].lower(),
                ),
            }

    return render_template('scheduling/heats.html',
                           tournament=tournament,
                           event=event,
                           heats=heats,
                           signup_rows=signup_rows,
                           signup_list_mode=signup_list_mode,
                           spacing_data=spacing_data)


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/generate-heats', methods=['POST'])
def generate_heats(tournament_id, event_id):
    """Generate heats for an event using snake draft distribution."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)

    # Gear-sharing integrity gate for pro events: block generation when any enrolled
    # competitor has unstructured gear details but no structured gear_sharing map.
    # This prevents silently building heats with unresolved gear conflicts.
    if event.event_type == 'pro':
        from models import EventResult
        from models.competitor import ProCompetitor
        enrolled_ids = {
            r.competitor_id
            for r in EventResult.query.filter_by(event_id=event.id, competitor_type='pro').all()
        }
        if enrolled_ids:
            unresolved_gear = [
                c for c in ProCompetitor.query.filter(
                    ProCompetitor.id.in_(enrolled_ids),
                    ProCompetitor.tournament_id == tournament_id,
                    ProCompetitor.status == 'active',
                ).all()
                if str(getattr(c, 'gear_sharing_details', '') or '').strip()
                and not c.get_gear_sharing()
            ]
            if unresolved_gear:
                names = ', '.join(c.name for c in unresolved_gear[:5])
                extra = f' (+{len(unresolved_gear) - 5} more)' if len(unresolved_gear) > 5 else ''
                flash(
                    f'Heat generation blocked: {len(unresolved_gear)} competitor(s) in '
                    f'{event.display_name} have unstructured gear-sharing notes — '
                    f'{names}{extra}. '
                    'Parse gear details in the Gear Sharing Manager first, or run Preflight Auto-Fix.',
                    'error'
                )
                return redirect(url_for('scheduling.event_heats',
                                        tournament_id=tournament_id,
                                        event_id=event_id))

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


@scheduling_bp.route('/<int:tournament_id>/generate-college-heats', methods=['POST'])
def generate_college_heats(tournament_id):
    """Bulk-generate heats for all closed college events in one click."""
    from services.heat_generator import generate_event_heats

    tournament = Tournament.query.get_or_404(tournament_id)
    events = tournament.events.filter_by(event_type='college').order_by(Event.name, Event.gender).all()

    generated = 0
    skipped_open = 0
    skipped_completed = 0
    errors = 0

    for event in events:
        if _is_list_only_event(event):
            skipped_open += 1
            continue
        if event.status == 'completed':
            skipped_completed += 1
            continue
        try:
            generate_event_heats(event)
            generated += 1
        except Exception as exc:
            if 'No competitors entered' in str(exc):
                skipped_open += 1
            else:
                errors += 1
                flash(f'Error generating heats for {event.display_name}: {exc}', 'error')

    parts = []
    if generated:
        parts.append(f'Heats generated for {generated} event(s)')
    if skipped_open:
        parts.append(f'{skipped_open} signup-list event(s) skipped')
    if skipped_completed:
        parts.append(f'{skipped_completed} completed event(s) unchanged')
    if parts:
        flash('. '.join(parts) + '.', 'success')

    log_action('generate_college_heats', 'tournament', tournament_id,
               {'generated': generated, 'skipped_open': skipped_open, 'errors': errors})
    return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))


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

    comp_type = event.event_type  # 'pro' or 'college'
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

        source.sync_assignments(comp_type)
        target.sync_assignments(comp_type)

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
    total = event.max_stands if event.max_stands is not None else stand_config.get('total', max(len(target_ids), 1))
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
    from models.competitor import ProCompetitor, CollegeCompetitor

    tournament = Tournament.query.get_or_404(tournament_id)
    flights = Flight.query.filter_by(tournament_id=tournament_id).order_by(Flight.flight_number).all()

    # Pre-fetch competitor names + stand assignments for display.
    # Preserve flight sequence order so the displayed opener matches the actual show order.
    flight_data = []
    for flight in flights:
        heat_rows = []
        for heat in flight.get_heats_ordered():
            event = Event.query.get(heat.event_id)
            if not event:
                continue
            comp_ids = heat.get_competitors()
            assignments = heat.get_stand_assignments()
            if event.event_type == 'college':
                comps = {c.id: c for c in CollegeCompetitor.query.filter(
                    CollegeCompetitor.id.in_(comp_ids)).all()} if comp_ids else {}
            else:
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
        flight_data.append({'flight': flight, 'heats': heat_rows})

    return render_template('pro/flights.html',
                           tournament=tournament,
                           flights=flights,
                           flight_data=flight_data)


@scheduling_bp.route('/<int:tournament_id>/flights/build', methods=['GET', 'POST'])
def build_flights(tournament_id):
    """Build flights for pro competition."""
    tournament = Tournament.query.get_or_404(tournament_id)

    if request.method == 'POST':
        from services.flight_builder import build_pro_flights

        try:
            num_flights = int(request.form.get('num_flights', 0))
            if num_flights < 1:
                num_flights = None
        except (TypeError, ValueError):
            num_flights = None

        # Guard: abort if no pro heats have been generated yet
        pro_heat_count = Heat.query.join(Event).filter(
            Event.tournament_id == tournament_id,
            Event.event_type == 'pro',
            Heat.run_number == 1
        ).count()
        if pro_heat_count == 0:
            flash('No pro heats found. Generate heats for pro events first, then build flights.', 'warning')
            return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))

        if request.form.get('run_async') == '1':
            from functools import partial
            fn = partial(build_pro_flights, num_flights=num_flights)
            job_id = submit_job('build_pro_flights', fn, tournament)
            log_action('flight_build_job_started', 'tournament', tournament_id, {'job_id': job_id})
            db.session.commit()
            flash('Flight build started in the background.', 'success')
            return redirect(url_for('reporting.export_results_job_status', tournament_id=tournament_id, job_id=job_id))

        try:
            built = build_pro_flights(tournament, num_flights=num_flights)
            log_action('flights_built', 'tournament', tournament_id, {'count': built})
            db.session.commit()
            flash(text.FLASH['flights_built'].format(num_flights=built), 'success')
        except Exception as e:
            flash(text.FLASH['flights_error'].format(error=str(e)), 'error')

        return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))

    # Get available heats
    pro_events = tournament.events.filter_by(event_type='pro').all()
    total_heats = sum(
        e.heats.filter_by(run_number=1).count()
        for e in pro_events
        if e.name != 'Partnered Axe Throw'
    )

    return render_template('pro/build_flights.html',
                           tournament=tournament,
                           events=pro_events,
                           total_heats=total_heats)


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
    """Configure Friday Night Feature events and Saturday college spillover."""
    from services.schedule_builder import COLLEGE_SATURDAY_PRIORITY

    tournament = Tournament.query.get_or_404(tournament_id)

    # FNF: pro events eligible for Friday Night
    eligible_names = set(config.FRIDAY_NIGHT_EVENTS)
    pro_events = tournament.events.filter_by(event_type='pro').order_by(Event.name, Event.gender).all()
    eligible_events = [e for e in pro_events if e.name in eligible_names]

    # Saturday spillover: college events eligible to run Saturday morning
    priority_index = {p: i for i, p in enumerate(COLLEGE_SATURDAY_PRIORITY)}
    all_college = tournament.events.filter_by(event_type='college').all()
    sat_eligible = sorted(
        [e for e in all_college if (e.name, e.gender) in priority_index],
        key=lambda e: priority_index[(e.name, e.gender)]
    )

    fnf_config = _load_fnf_config(tournament_id)
    session_key = f'schedule_options_{tournament_id}'
    saved_opts = session.get(session_key, {})

    if request.method == 'POST':
        action = request.form.get('action', 'save')
        selected_ids = [int(x) for x in request.form.getlist('event_ids') if x.isdigit()]
        notes = (request.form.get('notes') or '').strip()

        _save_fnf_config(tournament_id, {'event_ids': selected_ids, 'notes': notes})

        # Save Saturday spillover selections into the shared schedule session
        try:
            saturday_college_event_ids = [
                int(eid) for eid in request.form.getlist('saturday_college_event_ids') if str(eid).strip()
            ]
        except (TypeError, ValueError):
            saturday_college_event_ids = []

        saved_opts = dict(saved_opts)
        saved_opts['saturday_college_event_ids'] = saturday_college_event_ids
        session[session_key] = saved_opts
        session.modified = True
        # Also persist to DB
        tournament.set_schedule_config(saved_opts)
        db.session.commit()

        if action == 'generate_heats' and selected_ids:
            # Generate heats for each Friday Night Feature event using the
            # standard heat generator.  Existing heats for these events are
            # cleared first; only events already created in the DB are processed.
            from services.heat_generator import generate_event_heats
            generated = 0
            errors = []
            for event_id in selected_ids:
                event = Event.query.filter_by(id=event_id, tournament_id=tournament_id).first()
                if not event:
                    continue
                try:
                    heat_count = generate_event_heats(event)
                    generated += heat_count
                except Exception as exc:
                    errors.append(f'{event.display_name}: {exc}')
            db.session.commit()
            if errors:
                for err in errors:
                    flash(f'Heat generation error — {err}', 'error')
            if generated > 0:
                flash(f'Generated {generated} Friday Night Feature heat(s).', 'success')
            elif not errors:
                flash('No heats generated (check that competitors are enrolled in the selected events).', 'warning')
            log_action('fnf_heats_generated', 'tournament', tournament_id, {
                'event_ids': selected_ids,
                'heats_generated': generated,
            })
        else:
            log_action('friday_feature_configured', 'tournament', tournament_id, {
                'fnf_event_count': len(selected_ids),
                'sat_spillover_count': len(saturday_college_event_ids),
            })
            db.session.commit()
            flash('Friday Showcase & Saturday spillover saved.', 'success')
        return redirect(url_for('scheduling.friday_feature', tournament_id=tournament_id))

    selected_saturday_ids = set(int(i) for i in saved_opts.get('saturday_college_event_ids', []))

    return render_template(
        'scheduling/friday_feature.html',
        tournament=tournament,
        eligible_events=eligible_events,
        selected_ids=set(fnf_config.get('event_ids', [])),
        notes=fnf_config.get('notes', ''),
        sat_eligible=sat_eligible,
        selected_saturday_ids=selected_saturday_ids,
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
    from datetime import datetime
    from services.flight_builder import _STAND_CONFLICT_GAP

    tournament = Tournament.query.get_or_404(tournament_id)

    # Build ordered heat data: flights first, then ungrouped events
    flights = Flight.query.filter_by(tournament_id=tournament_id).order_by(Flight.flight_number).all()

    flight_data = []
    for flight in flights:
        heats_in_flight = flight.get_heats_ordered()
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
            # Detect Cookie Stack / Standing Block conflicts within this flight
            conflicts = []
            indexed = [(i, row['heat'], row['event'].stand_type) for i, row in enumerate(heat_rows)]
            conflict_pairs = [('cookie_stack', 'standing_block')]
            for i, _h, st_i in indexed:
                if not st_i:
                    continue
                for pair_a, pair_b in conflict_pairs:
                    if st_i not in (pair_a, pair_b):
                        continue
                    conflict_type = pair_b if st_i == pair_a else pair_a
                    for j, _h2, st_j in indexed:
                        if st_j == conflict_type and abs(i - j) < _STAND_CONFLICT_GAP and i != j:
                            conflicts.append({'pos_a': i + 1, 'pos_b': j + 1, 'gap': abs(i - j)})
                            break
            flight_data.append({'flight': flight, 'heats': heat_rows, 'stand_conflicts': conflicts})

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
        now=datetime.utcnow(),
        stand_conflict_gap=_STAND_CONFLICT_GAP,
    )


# ---------------------------------------------------------------------------
# Preflight JSON — inline checklist for events page
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/preflight-json')
def preflight_json(tournament_id):
    """JSON endpoint: inline preflight status for the events page."""
    from services.preflight import build_preflight_report
    from flask import jsonify
    tournament = Tournament.query.get_or_404(tournament_id)
    session_key = f'schedule_options_{tournament_id}'
    saved = tournament.get_schedule_config() or session.get(session_key, {})
    saturday_ids = [int(eid) for eid in saved.get('saturday_college_event_ids', [])]
    report = build_preflight_report(tournament, saturday_ids)
    return jsonify({
        'issue_count': report['issue_count'],
        'severity': report['severity'],
        'issues': [
            {
                'severity': i['severity'],
                'title': i['title'],
                'detail': i.get('detail', ''),
                'autofix': i.get('autofix', False),
            }
            for i in report['issues']
        ],
    })


# ---------------------------------------------------------------------------
# Flight heat reorder — drag-and-drop endpoint
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/flights/<int:flight_id>/reorder', methods=['POST'])
def reorder_flight_heats(tournament_id, flight_id):
    """Reorder heats within a flight. Expects JSON {heat_ids: [int, ...]}."""
    from flask import jsonify
    tournament = Tournament.query.get_or_404(tournament_id)
    flight = Flight.query.filter_by(id=flight_id, tournament_id=tournament_id).first_or_404()
    try:
        data = request.get_json(force=True)
        heat_ids = [int(hid) for hid in data.get('heat_ids', [])]
    except (TypeError, ValueError, AttributeError):
        return jsonify({'ok': False, 'error': 'Invalid heat_ids'}), 400

    existing = {h.id: h for h in flight.get_heats_ordered()}
    if set(heat_ids) != set(existing.keys()):
        return jsonify({'ok': False, 'error': 'Heat set mismatch — refresh and try again'}), 400

    for position, hid in enumerate(heat_ids, start=1):
        existing[hid].flight_position = position
    db.session.commit()
    log_action('flight_heats_reordered', 'flight', flight_id, {'order': heat_ids})
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Show Day — live operations dashboard
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/show-day')
def show_day(tournament_id):
    """Live operations dashboard for show day."""
    tournament = Tournament.query.get_or_404(tournament_id)
    flights = Flight.query.filter_by(tournament_id=tournament_id).order_by(Flight.flight_number).all()

    flight_data = []
    for flight in flights:
        heats_ordered = flight.get_heats_ordered()
        total = len(heats_ordered)
        completed = sum(1 for h in heats_ordered if h.status == 'completed')
        in_progress = sum(1 for h in heats_ordered if h.status == 'in_progress')

        current_heat = next((h for h in heats_ordered if h.status == 'in_progress'), None)
        if current_heat is None:
            current_heat = next((h for h in heats_ordered if h.status not in ('completed',)), None)

        current_event = Event.query.get(current_heat.event_id) if current_heat else None

        upcoming_pairs = []
        for h in heats_ordered:
            if h.status != 'completed' and h is not current_heat:
                ev = Event.query.get(h.event_id)
                if ev:
                    upcoming_pairs.append((h, ev))
                if len(upcoming_pairs) >= 2:
                    break

        pct = int(completed / total * 100) if total else 0
        if completed == total and total > 0:
            status = 'completed'
        elif in_progress > 0:
            status = 'in_progress'
        elif completed == 0:
            status = 'pending'
        else:
            status = 'partial'

        flight_data.append({
            'flight': flight,
            'total': total,
            'completed': completed,
            'in_progress': in_progress,
            'pct': pct,
            'status': status,
            'current_heat': current_heat,
            'current_event': current_event,
            'upcoming': upcoming_pairs,
        })

    college_events_data = []
    for event in tournament.events.filter_by(event_type='college').order_by(Event.name, Event.gender).all():
        heats = event.heats.order_by(Heat.heat_number).all()
        total = len(heats)
        completed = sum(1 for h in heats if h.status == 'completed')
        college_events_data.append({
            'event': event,
            'total': total,
            'completed': completed,
            'pct': int(completed / total * 100) if total else 0,
        })

    return render_template(
        'scheduling/show_day.html',
        tournament=tournament,
        flight_data=flight_data,
        college_events_data=college_events_data,
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


# ---------------------------------------------------------------------------
# Ability Rankings — per-event judge-assigned ranks for heat snake-draft sort
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/pro/ability-rankings', methods=['GET', 'POST'])
def ability_rankings(tournament_id):
    """View and set per-event ability rankings for pro competitors."""
    from models.pro_event_rank import (
        ProEventRank, RANKED_CATEGORIES, CATEGORY_DISPLAY_NAMES, CATEGORY_DESCRIPTIONS
    )

    tournament = Tournament.query.get_or_404(tournament_id)

    if request.method == 'POST':
        # Parse rank_{category}_{competitor_id} fields and upsert ProEventRank rows.
        saved_count = 0
        deleted_count = 0
        for key, raw_val in request.form.items():
            if not key.startswith('rank_'):
                continue
            parts = key.split('_', 2)
            if len(parts) != 3:
                continue
            _, category, comp_id_str = parts
            if category not in RANKED_CATEGORIES:
                continue
            try:
                competitor_id = int(comp_id_str)
            except (TypeError, ValueError):
                continue

            raw_val = raw_val.strip()
            if not raw_val:
                # Blank field — delete existing rank if present.
                existing = ProEventRank.query.filter_by(
                    tournament_id=tournament_id,
                    competitor_id=competitor_id,
                    event_category=category,
                ).first()
                if existing:
                    db.session.delete(existing)
                    deleted_count += 1
                continue

            try:
                rank = int(raw_val)
                if rank < 1:
                    raise ValueError
            except (TypeError, ValueError):
                flash(f'Invalid rank value "{raw_val}" for category {category} — must be a positive integer.', 'error')
                continue

            existing = ProEventRank.query.filter_by(
                tournament_id=tournament_id,
                competitor_id=competitor_id,
                event_category=category,
            ).first()
            if existing:
                existing.rank = rank
            else:
                db.session.add(ProEventRank(
                    tournament_id=tournament_id,
                    competitor_id=competitor_id,
                    event_category=category,
                    rank=rank,
                ))
            saved_count += 1

        db.session.commit()
        log_action('ability_rankings_saved', 'tournament', tournament_id, {
            'saved': saved_count,
            'cleared': deleted_count,
        })
        flash(f'Ability rankings saved ({saved_count} set, {deleted_count} cleared).', 'success')
        return redirect(url_for('scheduling.ability_rankings', tournament_id=tournament_id))

    # GET — build display data.
    # Find which ranked categories have at least one pro event for this tournament.
    pro_events = tournament.events.filter_by(event_type='pro').all()
    category_event_map: dict[str, list] = {}
    for event in pro_events:
        cat = _event_rank_category(event)
        if cat:
            category_event_map.setdefault(cat, []).append(event)

    # Load existing ranks for this tournament.
    existing_ranks = ProEventRank.query.filter_by(tournament_id=tournament_id).all()
    rank_map: dict[tuple, int] = {
        (r.competitor_id, r.event_category): r.rank for r in existing_ranks
    }

    # Build category_groups: {category: {'M': [...], 'F': [...], 'open': [...]}}
    # Each entry is {competitor, rank (or None)}.
    category_groups: dict[str, dict] = {}
    for category, cat_events in category_event_map.items():
        genders_seen: dict[str, set] = {}  # gender -> set of competitor ids already added
        group: dict[str, list] = {}
        for event in cat_events:
            gender_key = event.gender if event.gender else 'open'
            seen = genders_seen.setdefault(gender_key, set())
            comps = ProCompetitor.query.filter_by(
                tournament_id=tournament_id,
                status='active',
            )
            if event.gender:
                comps = comps.filter_by(gender=event.gender)
            comps = comps.order_by(ProCompetitor.name).all()
            for comp in comps:
                if comp.id in seen:
                    continue
                seen.add(comp.id)
                group.setdefault(gender_key, []).append({
                    'competitor': comp,
                    'rank': rank_map.get((comp.id, category)),
                })
        # Sort each gender group by current rank (ranked first, then unranked alphabetically).
        for gk in group:
            group[gk].sort(key=lambda e: (
                e['rank'] if e['rank'] is not None else float('inf'),
                e['competitor'].name,
            ))
        if group:
            category_groups[category] = group

    return render_template(
        'scheduling/ability_rankings.html',
        tournament=tournament,
        category_groups=category_groups,
        category_display_names=CATEGORY_DISPLAY_NAMES,
        category_descriptions=CATEGORY_DESCRIPTIONS,
    )


# ---------------------------------------------------------------------------
# #4 — Async heat / flight generation  (#4)
# ---------------------------------------------------------------------------

def _async_generate_all(tournament_id: int) -> dict:
    """Background task: generate all heats + pro flights for a tournament.

    Runs inside a new application context so the ThreadPoolExecutor worker
    has access to the DB session and Flask app.
    """
    from flask import current_app
    app = current_app._get_current_object()
    with app.app_context():
        from models import Tournament as _Tournament
        from services.heat_generator import generate_event_heats
        from services.flight_builder import build_pro_flights

        tournament = _Tournament.query.get(tournament_id)
        if not tournament:
            return {'ok': False, 'error': f'Tournament {tournament_id} not found.'}

        generated, skipped, errors = 0, 0, []
        for event in tournament.events.order_by(Event.event_type, Event.name, Event.gender).all():
            try:
                generate_event_heats(event)
                generated += 1
            except Exception as exc:
                if 'No competitors entered' in str(exc):
                    skipped += 1
                else:
                    errors.append(str(exc))

        pro_flights = _build_pro_flights_if_possible(tournament, build_pro_flights)
        return {
            'ok': True,
            'generated': generated,
            'skipped': skipped,
            'errors': errors,
            'flights': pro_flights,
        }


@scheduling_bp.route('/<int:tournament_id>/events/generate-async', methods=['POST'])
def generate_async(tournament_id):
    """Submit heat + flight generation as a background job and return a job_id."""
    Tournament.query.get_or_404(tournament_id)
    from flask import current_app
    app = current_app._get_current_object()

    job_id = submit_job(
        f'generate_all:{tournament_id}',
        _async_generate_all,
        tournament_id,
    )
    return json.dumps({'job_id': job_id}), 202, {'Content-Type': 'application/json'}


@scheduling_bp.route('/<int:tournament_id>/events/job-status/<job_id>')
def generation_job_status(tournament_id, job_id):
    """Poll background job status. Returns JSON with status/result."""
    from services.background_jobs import get as get_job
    job = get_job(job_id)
    if not job:
        return json.dumps({'error': 'Job not found.'}), 404, {'Content-Type': 'application/json'}
    return json.dumps({
        'job_id': job['id'],
        'status': job['status'],
        'result': job['result'],
        'error': job['error'],
    }), 200, {'Content-Type': 'application/json'}
