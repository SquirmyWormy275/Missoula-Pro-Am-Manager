"""
Flight management routes: flight_list, build_flights, start_flight, complete_flight,
reorder_flight_heats, and the SMS notification helper.
"""
from flask import flash, jsonify, redirect, render_template, request, url_for

import strings as text
from database import db
from models import Event, Flight, Heat, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.audit import log_action
from services.background_jobs import submit as submit_job

from . import _build_pro_flights_if_possible, _generate_all_heats, scheduling_bp


@scheduling_bp.route('/<int:tournament_id>/flights/one-click-generate', methods=['POST'])
def one_click_generate(tournament_id):
    """Generate heats for every event AND build pro flights AND integrate college
    spillover — in a single user action. Redirects back to the Flights page.

    This is the button users reach for when they have competitors registered and
    Friday/Saturday configuration done and just want the show schedule built.
    """
    from services.flight_builder import (
        build_pro_flights,
        integrate_college_spillover_into_flights,
        integrate_proam_relay_into_final_flight,
    )
    from services.heat_generator import generate_event_heats
    from services.saw_block_assignment import trigger_saw_block_recompute

    tournament = Tournament.query.get_or_404(tournament_id)

    db_config = tournament.get_schedule_config() or {}
    saturday_college_event_ids = [int(i) for i in db_config.get('saturday_college_event_ids', [])]

    try:
        _generate_all_heats(tournament, generate_event_heats)
        flights_built = _build_pro_flights_if_possible(tournament, build_pro_flights)
        if flights_built is not None:
            flash(f'Built {flights_built} pro flight(s).', 'success')
            # Phase 4: Relay BEFORE spillover so Chokerman Run 2 still closes.
            relay_result = integrate_proam_relay_into_final_flight(tournament)
            if relay_result.get('placed'):
                flash('Pro-Am Relay placed in the final flight.', 'success')
            integration = integrate_college_spillover_into_flights(
                tournament, saturday_college_event_ids,
            )
            if integration['integrated_heats'] > 0:
                db.session.commit()
                flash(
                    f"Integrated {integration['integrated_heats']} college spillover heat(s) "
                    'into Saturday flights.',
                    'success',
                )
        trigger_saw_block_recompute(tournament)
        log_action('one_click_generate', 'tournament', tournament_id, {
            'flights_built': flights_built,
        })
    except Exception as exc:
        db.session.rollback()
        flash(f'One-click generate failed and was rolled back: {exc}', 'error')

    return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))


@scheduling_bp.route('/<int:tournament_id>/flights')
def flight_list(tournament_id):
    """View and manage flights for pro competition."""
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

            # For partnered events, resolve each competitor's partner_id
            # (the other ProCompetitor.id bound to this event in their
            # partners JSON) so the UI can drag the pair as one unit.
            partner_id_by_comp: dict[int, int | None] = {}
            if event.is_partnered and event.event_type == 'pro':
                for cid, comp in comps.items():
                    partner_id_by_comp[cid] = None
                    partners = comp.get_partners() if hasattr(comp, 'get_partners') else {}
                    partner_name = partners.get(str(event.id)) or partners.get(event.id)
                    if partner_name:
                        for other_cid, other in comps.items():
                            if other_cid != cid and other.name.strip().lower() == str(partner_name).strip().lower():
                                partner_id_by_comp[cid] = other_cid
                                break

            heat_rows.append({
                'heat': heat,
                'event': event,
                'competitors': [
                    {
                        'id': cid,
                        'name': comps[cid].display_name if cid in comps else f'ID:{cid}',
                        'stand': assignments.get(str(cid), '?'),
                        'partner_id': partner_id_by_comp.get(cid),
                    }
                    for cid in comp_ids
                ],
            })
        flight_data.append({'flight': flight, 'heats': heat_rows})

    return render_template('pro/flights.html',
                           tournament=tournament,
                           flights=flights,
                           flight_data=flight_data)


# ---------------------------------------------------------------------------
# Flight sizing config (Phase 3) — persisted in Tournament.schedule_config.
# Two modes: 'minutes' (derive num_flights from target duration) or 'count'
# (operator picks num_flights directly). Defaults below are tuned for the
# Missoula Pro Am's 60-min flight cadence and 5.5-min avg heat duration.
# ---------------------------------------------------------------------------

FLIGHT_SIZING_MODE_MINUTES = 'minutes'
FLIGHT_SIZING_MODE_COUNT = 'count'
VALID_FLIGHT_SIZING_MODES = {FLIGHT_SIZING_MODE_MINUTES, FLIGHT_SIZING_MODE_COUNT}

FLIGHT_SIZING_DEFAULTS = {
    'mode': FLIGHT_SIZING_MODE_MINUTES,
    'target_minutes_per_flight': 60,
    'minutes_per_heat': 5.5,
    'num_flights': 4,
}

FLIGHT_COUNT_MIN = 2
FLIGHT_COUNT_MAX = 10
MINUTES_PER_FLIGHT_MIN = 30
MINUTES_PER_FLIGHT_MAX = 180
MINUTES_PER_HEAT_MIN = 1.0
MINUTES_PER_HEAT_MAX = 15.0


def _read_flight_sizing_config(tournament):
    """Return saved flight-sizing config merged over defaults."""
    cfg = tournament.get_schedule_config() or {}
    mode = cfg.get('flight_sizing_mode', FLIGHT_SIZING_DEFAULTS['mode'])
    if mode not in VALID_FLIGHT_SIZING_MODES:
        mode = FLIGHT_SIZING_DEFAULTS['mode']
    try:
        target_minutes = int(cfg.get('target_minutes_per_flight',
                                     FLIGHT_SIZING_DEFAULTS['target_minutes_per_flight']))
    except (TypeError, ValueError):
        target_minutes = FLIGHT_SIZING_DEFAULTS['target_minutes_per_flight']
    try:
        minutes_per_heat = float(cfg.get('minutes_per_heat',
                                         FLIGHT_SIZING_DEFAULTS['minutes_per_heat']))
    except (TypeError, ValueError):
        minutes_per_heat = FLIGHT_SIZING_DEFAULTS['minutes_per_heat']
    try:
        saved_num_flights = int(cfg.get('num_flights', FLIGHT_SIZING_DEFAULTS['num_flights']))
    except (TypeError, ValueError):
        saved_num_flights = FLIGHT_SIZING_DEFAULTS['num_flights']
    return {
        'mode': mode,
        'target_minutes_per_flight': max(
            MINUTES_PER_FLIGHT_MIN, min(MINUTES_PER_FLIGHT_MAX, target_minutes),
        ),
        'minutes_per_heat': max(
            MINUTES_PER_HEAT_MIN, min(MINUTES_PER_HEAT_MAX, minutes_per_heat),
        ),
        'num_flights': max(FLIGHT_COUNT_MIN, min(FLIGHT_COUNT_MAX, saved_num_flights)),
    }


def _persist_flight_sizing_config(tournament, mode, target_minutes, minutes_per_heat, num_flights):
    """Persist operator's flight sizing choices to schedule_config."""
    cfg = tournament.get_schedule_config() or {}
    cfg['flight_sizing_mode'] = mode
    cfg['target_minutes_per_flight'] = int(target_minutes)
    cfg['minutes_per_heat'] = float(minutes_per_heat)
    cfg['num_flights'] = int(num_flights)
    tournament.set_schedule_config(cfg)


def _compute_num_flights_from_duration(total_heats, minutes_per_heat, target_minutes_per_flight):
    """Derive num_flights from target flight duration.

    Returns a clamped value in [FLIGHT_COUNT_MIN, FLIGHT_COUNT_MAX]. A
    ``clamped`` flag indicates the ideal calculation exceeded the range so
    the caller can surface a warning.
    """
    import math as _math
    if total_heats <= 0 or minutes_per_heat <= 0 or target_minutes_per_flight <= 0:
        return FLIGHT_COUNT_MIN, False
    ideal = _math.ceil((total_heats * minutes_per_heat) / target_minutes_per_flight)
    clamped_value = max(FLIGHT_COUNT_MIN, min(FLIGHT_COUNT_MAX, ideal))
    return clamped_value, clamped_value != ideal


@scheduling_bp.route('/<int:tournament_id>/flights/build', methods=['GET', 'POST'])
def build_flights(tournament_id):
    """Build flights for pro competition."""
    tournament = Tournament.query.get_or_404(tournament_id)

    if request.method == 'POST':
        from services.flight_builder import build_pro_flights

        # Phase 3: accept either 'minutes' (duration-driven) or 'count'
        # (operator-specified). Persist choices to schedule_config so the
        # form pre-fills on the next visit.
        sizing_mode_raw = (request.form.get('flight_sizing_mode') or '').strip().lower()
        if sizing_mode_raw not in VALID_FLIGHT_SIZING_MODES:
            sizing_mode_raw = FLIGHT_SIZING_DEFAULTS['mode']

        try:
            form_target_minutes = int(request.form.get('target_minutes_per_flight',
                                                       FLIGHT_SIZING_DEFAULTS['target_minutes_per_flight']))
        except (TypeError, ValueError):
            form_target_minutes = FLIGHT_SIZING_DEFAULTS['target_minutes_per_flight']
        form_target_minutes = max(
            MINUTES_PER_FLIGHT_MIN, min(MINUTES_PER_FLIGHT_MAX, form_target_minutes),
        )

        try:
            form_minutes_per_heat = float(request.form.get('minutes_per_heat',
                                                           FLIGHT_SIZING_DEFAULTS['minutes_per_heat']))
        except (TypeError, ValueError):
            form_minutes_per_heat = FLIGHT_SIZING_DEFAULTS['minutes_per_heat']
        form_minutes_per_heat = max(
            MINUTES_PER_HEAT_MIN, min(MINUTES_PER_HEAT_MAX, form_minutes_per_heat),
        )

        try:
            form_num_flights = int(request.form.get('num_flights', 0))
        except (TypeError, ValueError):
            form_num_flights = 0

        # Total heats available for the duration calc (pro run_number=1 excluding axe).
        pro_heats_for_calc = Heat.query.join(Event).filter(
            Event.tournament_id == tournament_id,
            Event.event_type == 'pro',
            Event.name != 'Partnered Axe Throw',
            Heat.run_number == 1,
        ).count()

        if sizing_mode_raw == FLIGHT_SIZING_MODE_MINUTES:
            computed_num_flights, was_clamped = _compute_num_flights_from_duration(
                pro_heats_for_calc, form_minutes_per_heat, form_target_minutes,
            )
            if was_clamped and pro_heats_for_calc > 0:
                actual_flight_minutes = int(
                    (pro_heats_for_calc * form_minutes_per_heat) / computed_num_flights,
                )
                flash(
                    f'Target {form_target_minutes} min/flight would need more than '
                    f'{FLIGHT_COUNT_MAX} or fewer than {FLIGHT_COUNT_MIN} flights. '
                    f'Capped at {computed_num_flights} flights — actual duration '
                    f'~{actual_flight_minutes} min/flight.',
                    'info',
                )
            num_flights = computed_num_flights if computed_num_flights >= 1 else None
        else:  # 'count' mode
            num_flights = form_num_flights if form_num_flights >= 1 else None

        # Persist the operator's sizing choices so the form pre-fills next visit.
        try:
            _persist_flight_sizing_config(
                tournament, sizing_mode_raw,
                form_target_minutes, form_minutes_per_heat,
                form_num_flights if form_num_flights >= FLIGHT_COUNT_MIN else FLIGHT_SIZING_DEFAULTS['num_flights'],
            )
            db.session.commit()
        except Exception:
            db.session.rollback()

        # Guard: abort if no pro heats have been generated yet
        pro_heat_count = Heat.query.join(Event).filter(
            Event.tournament_id == tournament_id,
            Event.event_type == 'pro',
            Heat.run_number == 1
        ).count()
        if pro_heat_count == 0:
            flash('No pro heats found. Generate heats for pro events first, then build flights.', 'warning')
            return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))

        # Flights exist to interleave heats from DIFFERENT events for crowd variety.
        # Warn when that premise is broken, but still run the builder so the user can
        # clear stale flight data and see the best grouping for the current heat state.
        pro_events_with_heats = db.session.query(Event.id).join(Heat).filter(
            Event.tournament_id == tournament_id,
            Event.event_type == 'pro',
            Event.name != 'Partnered Axe Throw',
            Heat.run_number == 1,
        ).distinct().count()
        if pro_events_with_heats <= 1:
            flash(
                f'Only {pro_events_with_heats} pro event has heats generated. Flights group '
                'heats from multiple events for crowd variety — all heats will land in a '
                'single flight until more events have heats generated.',
                'warning',
            )
            num_flights = None  # let the builder collapse to one flight of up to 8

        # Clamp num_flights so each flight gets at least 2 heats; a "flight" with 1 heat is
        # just a heat. Mirrors MIN_HEATS_PER_FLIGHT in flight_builder.
        elif num_flights and num_flights > 0 and pro_heat_count >= 2:
            effective_heats_per_flight = pro_heat_count // num_flights
            if effective_heats_per_flight < 2:
                import math as _math
                clamped = _math.ceil(pro_heat_count / 2)
                if clamped != num_flights:
                    flash(
                        f'Requested {num_flights} flights for {pro_heat_count} heats would '
                        f'give less than 2 heats per flight. Building {clamped} flights instead.',
                        'info',
                    )
                num_flights = clamped

        if request.form.get('run_async') == '1':
            def _build_flights_async(target_tournament_id: int, requested_num_flights: int | None):
                """Build pro flights + relay + spillover atomically.

                All three operations run with commit=False; a single db.session.commit()
                at the end makes the chain atomic. If any step raises, the entire
                build rolls back — no orphaned relay heat, no orphaned Chokerman
                Run 2, no orphaned saturday_college_event_ids heats.

                Order: build → relay → spillover. Relay lands first at the end of
                the last flight; Chokerman Run 2 then appends AFTER the relay so
                the show closes with Chokerman (FlightLogic.md §4.1).
                """
                from services.flight_builder import (
                    integrate_college_spillover_into_flights,
                    integrate_proam_relay_into_final_flight,
                )
                target = Tournament.query.get(target_tournament_id)
                if not target:
                    raise RuntimeError(f'Tournament {target_tournament_id} not found.')
                try:
                    flights_built = build_pro_flights(
                        target,
                        num_flights=requested_num_flights,
                        commit=False,
                    )
                    relay_result = integrate_proam_relay_into_final_flight(
                        target, commit=False,
                    )
                    saturday_college_event_ids = [
                        int(i) for i in
                        (target.get_schedule_config() or {}).get('saturday_college_event_ids', [])
                    ]
                    integration = integrate_college_spillover_into_flights(
                        target,
                        college_event_ids=saturday_college_event_ids,
                        commit=False,
                    )
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    raise
                return {
                    'flights_built': flights_built,
                    'relay': {
                        'placed': relay_result.get('placed', False),
                        'reason': relay_result.get('reason'),
                        'team_count': relay_result.get('team_count', 0),
                    },
                    'spillover': {
                        'integrated_heats': integration.get('integrated_heats', 0),
                        'events': integration.get('events', 0),
                        'message': integration.get('message', ''),
                    },
                }

            job_id = submit_job(
                'build_pro_flights',
                _build_flights_async,
                tournament_id,
                num_flights,
                metadata={'tournament_id': tournament_id, 'kind': 'build_pro_flights'},
            )
            log_action('flight_build_job_started', 'tournament', tournament_id, {'job_id': job_id})
            db.session.commit()
            flash('Flight build started in the background.', 'success')
            return redirect(url_for('reporting.export_results_job_status', tournament_id=tournament_id, job_id=job_id))

        try:
            built = build_pro_flights(tournament, num_flights=num_flights)
            log_action('flights_built', 'tournament', tournament_id, {'count': built})
            db.session.commit()
            flash(text.FLASH['flights_built'].format(num_flights=built), 'success')

            # Phase 5: surface LH springboard dummy contention warnings.
            from services.flight_builder import get_last_lh_flight_warnings
            for w in get_last_lh_flight_warnings(tournament_id):
                flash(
                    f"LH SPRINGBOARD CONTENTION: Flight {w['flight_number']} "
                    f"contains {w['lh_count']} left-handed cutters. "
                    'Consider increasing flight count — LH dummy setup '
                    'cannot be shared within one flight block.',
                    'warning',
                )

            # build_pro_flights wipes every Heat.flight_id (including college
            # spillover that was previously integrated). Chain the relay + spillover
            # so "Rebuild Flights Only" doesn't silently orphan them.
            # Order: relay BEFORE spillover so Chokerman Run 2 lands last.
            from services.flight_builder import (
                integrate_college_spillover_into_flights,
                integrate_proam_relay_into_final_flight,
            )
            relay_result = integrate_proam_relay_into_final_flight(tournament)
            if relay_result.get('placed'):
                flash('Pro-Am Relay placed in the final flight.', 'success')

            db_config = tournament.get_schedule_config() or {}
            saturday_college_event_ids = [
                int(i) for i in db_config.get('saturday_college_event_ids', [])
            ]
            integration = integrate_college_spillover_into_flights(
                tournament, saturday_college_event_ids,
            )
            if integration.get('integrated_heats'):
                db.session.commit()
                flash(
                    f"Integrated {integration['integrated_heats']} college spillover "
                    f"heat(s) into Saturday flights.",
                    'success',
                )

            from services.saw_block_assignment import trigger_saw_block_recompute
            trigger_saw_block_recompute(tournament)
        except Exception as e:
            db.session.rollback()
            from flask import current_app
            current_app.logger.exception(
                'Flight build failed for tournament %s', tournament_id,
            )
            # Generic message — full traceback is in app logs. Operator gets
            # the action plus a hint to escalate; raw exception text never
            # surfaces in the UI per CLAUDE.md §6 safe-error-handling rule.
            flash(text.FLASH['flights_error'].format(
                error='see application logs (admin only)'
            ), 'error')

        return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))

    # Get available heats
    pro_events = tournament.events.filter_by(event_type='pro').all()
    total_heats = sum(
        e.heats.filter_by(run_number=1).count()
        for e in pro_events
        if e.name != 'Partnered Axe Throw'
    )

    sizing = _read_flight_sizing_config(tournament)

    return render_template('pro/build_flights.html',
                           tournament=tournament,
                           events=pro_events,
                           total_heats=total_heats,
                           flight_sizing=sizing,
                           flight_count_min=FLIGHT_COUNT_MIN,
                           flight_count_max=FLIGHT_COUNT_MAX,
                           minutes_per_flight_min=MINUTES_PER_FLIGHT_MIN,
                           minutes_per_flight_max=MINUTES_PER_FLIGHT_MAX,
                           minutes_per_heat_min=MINUTES_PER_HEAT_MIN,
                           minutes_per_heat_max=MINUTES_PER_HEAT_MAX)


# ---------------------------------------------------------------------------
# Flight heat reorder — drag-and-drop endpoint
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/flights/<int:flight_id>/reorder', methods=['POST'])
def reorder_flight_heats(tournament_id, flight_id):
    """Reorder heats within a flight. Expects JSON {heat_ids: [int, ...]}."""
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

    from services.saw_block_assignment import trigger_saw_block_recompute
    trigger_saw_block_recompute(tournament)

    return jsonify({'ok': True})


@scheduling_bp.route('/<int:tournament_id>/flights/bulk-reorder', methods=['POST'])
def bulk_reorder_flights(tournament_id):
    """Apply a full DOM snapshot of flight heat order — handles both within-flight
    reordering and cross-flight moves in one atomic update.

    Expects JSON: {flights: [{flight_id: int, heat_ids: [int, ...]}, ...]}.
    The union of all heat_ids across flights MUST equal the set of all heats
    currently assigned to any of those flights — otherwise refuse to prevent
    an incomplete drag from wiping state.
    """
    tournament = Tournament.query.get_or_404(tournament_id)

    try:
        data = request.get_json(force=True)
        entries = data.get('flights', [])
        payload: list[tuple[int, list[int]]] = []
        for entry in entries:
            fid = int(entry['flight_id'])
            hids = [int(h) for h in entry.get('heat_ids', [])]
            payload.append((fid, hids))
    except (TypeError, ValueError, KeyError, AttributeError):
        return jsonify({'ok': False, 'error': 'Invalid payload'}), 400

    if not payload:
        return jsonify({'ok': False, 'error': 'No flights in payload'}), 400

    flight_ids = [fid for fid, _ in payload]
    flights = Flight.query.filter(
        Flight.id.in_(flight_ids),
        Flight.tournament_id == tournament_id,
    ).all()
    if len(flights) != len(flight_ids):
        return jsonify({'ok': False, 'error': 'Unknown flight id'}), 400

    # Heat set check: every heat currently in these flights must still be
    # present in the payload. Prevents a half-loaded DOM from dropping heats.
    existing_heats = Heat.query.filter(Heat.flight_id.in_(flight_ids)).all()
    existing_heat_ids = {h.id for h in existing_heats}
    payload_heat_ids = {hid for _, hids in payload for hid in hids}
    if existing_heat_ids != payload_heat_ids:
        return jsonify({
            'ok': False,
            'error': 'Heat set mismatch — refresh and try again',
        }), 400

    heats_by_id = {h.id: h for h in existing_heats}
    for fid, hids in payload:
        for position, hid in enumerate(hids, start=1):
            heat = heats_by_id[hid]
            heat.flight_id = fid
            heat.flight_position = position
    db.session.commit()

    log_action('flights_bulk_reordered', 'tournament', tournament_id,
               {'flights': [{'flight_id': fid, 'count': len(hids)} for fid, hids in payload]})

    from services.saw_block_assignment import trigger_saw_block_recompute
    trigger_saw_block_recompute(tournament)

    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Competitor move — drag-drop individuals (or partnered pairs) between heats
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/heats/<int:source_heat_id>/drag-move',
                     methods=['POST'])
def drag_move_competitor(tournament_id, source_heat_id):
    """Move a competitor (or a partnered pair) from source_heat to target_heat.

    Both heats must belong to the same event (a competitor can only be rearranged
    into a heat they're signed up for — which means same event). For partnered
    events the caller must send both partner IDs in competitor_ids; the endpoint
    moves them as a unit.

    Body: {
        competitor_ids: [int, ...],  # 1 for solo, 2 for partnered
        target_heat_id: int,
    }

    Returns: {ok: bool, error?: str, source?: {...}, target?: {...}}
    """
    tournament = Tournament.query.get_or_404(tournament_id)
    source = Heat.query.filter_by(id=source_heat_id).first_or_404()

    try:
        data = request.get_json(silent=True) or {}
        competitor_ids = [int(c) for c in data.get('competitor_ids', [])]
        target_heat_id = int(data.get('target_heat_id'))
    except (TypeError, ValueError, KeyError):
        return jsonify({'ok': False, 'error': 'Invalid payload'}), 400

    if not competitor_ids:
        return jsonify({'ok': False, 'error': 'competitor_ids required'}), 400

    target = Heat.query.filter_by(id=target_heat_id).first()
    if target is None:
        return jsonify({'ok': False, 'error': 'Target heat not found'}), 404

    # Same-event constraint: competitors signed up for event E can only be moved
    # among heats of event E.
    if source.event_id != target.event_id:
        return jsonify({
            'ok': False,
            'error': 'Competitor can only be moved into a heat of the same event.',
        }), 400

    event = Event.query.filter_by(id=source.event_id, tournament_id=tournament_id).first()
    if event is None:
        return jsonify({'ok': False, 'error': 'Event not found for tournament'}), 404

    # Every competitor in the payload must currently be in the source heat.
    source_comps = source.get_competitors()
    missing = [c for c in competitor_ids if c not in source_comps]
    if missing:
        return jsonify({
            'ok': False,
            'error': f'Competitor(s) {missing} not in source heat — refresh and try again.',
        }), 400

    # Target heat capacity check.
    max_stands = event.max_stands or 4
    target_comps = target.get_competitors()
    if len(target_comps) + len(competitor_ids) > max_stands:
        return jsonify({
            'ok': False,
            'code': 'target_full',
            'error': (
                f'Target heat {target.heat_number} is full '
                f'({len(target_comps)}/{max_stands}). '
                'Use the holding bin to rearrange, or pick a heat with open stands.'
            ),
        }), 409

    # Perform the move.
    source_assignments = source.get_stand_assignments()
    target_assignments = target.get_stand_assignments()

    used_stands = {int(v) for v in target_assignments.values() if v is not None}
    next_free = 1
    def _next_stand():
        nonlocal next_free
        while next_free in used_stands:
            next_free += 1
        stand = next_free
        used_stands.add(stand)
        next_free += 1
        return stand

    for cid in competitor_ids:
        source.remove_competitor(cid)
        source_assignments.pop(str(cid), None)
        target.add_competitor(cid)
        target.set_stand_assignment(cid, _next_stand())

    source.stand_assignments = (
        __import__('json').dumps(source_assignments) if source_assignments else '{}'
    )
    db.session.flush()

    competitor_type = 'pro' if event.event_type == 'pro' else 'college'
    source.sync_assignments(competitor_type)
    target.sync_assignments(competitor_type)

    db.session.commit()
    log_action('competitor_moved_between_heats', 'heat', target.id, {
        'tournament_id': tournament_id,
        'source_heat_id': source.id,
        'target_heat_id': target.id,
        'competitor_ids': competitor_ids,
        'event_id': event.id,
    })

    from services.saw_block_assignment import trigger_saw_block_recompute
    trigger_saw_block_recompute(tournament)

    return jsonify({
        'ok': True,
        'source': {
            'heat_id': source.id,
            'competitor_ids': source.get_competitors(),
            'stand_assignments': source.get_stand_assignments(),
        },
        'target': {
            'heat_id': target.id,
            'competitor_ids': target.get_competitors(),
            'stand_assignments': target.get_stand_assignments(),
        },
    })


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

    from services.sms_notify import is_configured, send_sms

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
    competitor_type_map: dict = {}
    for heat in target_flight.heats.all():
        event = Event.query.get(heat.event_id)
        if not event:
            continue
        for cid in heat.get_competitors():
            competitor_ids_in_flight.add(int(cid))
            competitor_type_map[int(cid)] = event.event_type

    pro_ids = [cid for cid, t in competitor_type_map.items() if t == 'pro']
    col_ids = [cid for cid, t in competitor_type_map.items() if t == 'college']

    sms_targets: list = []  # (phone, name)

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
