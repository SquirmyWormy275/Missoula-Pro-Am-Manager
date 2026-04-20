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

from . import scheduling_bp


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
            heat_rows.append({
                'heat': heat,
                'event': event,
                'competitors': [
                    {'name': comps[cid].display_name if cid in comps else f'ID:{cid}',
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
            def _build_flights_async(target_tournament_id: int, requested_num_flights: int | None):
                target = Tournament.query.get(target_tournament_id)
                if not target:
                    raise RuntimeError(f'Tournament {target_tournament_id} not found.')
                return build_pro_flights(target, num_flights=requested_num_flights)

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
# Flight heat reorder — drag-and-drop endpoint
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/flights/<int:flight_id>/reorder', methods=['POST'])
def reorder_flight_heats(tournament_id, flight_id):
    """Reorder heats within a flight. Expects JSON {heat_ids: [int, ...]}."""
    Tournament.query.get_or_404(tournament_id)
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
