"""Application services for schedule autofix and bulk generation workflows."""
from __future__ import annotations

from database import db
from models import Event, Tournament


def run_preflight_autofix(tournament: Tournament, saturday_ids: list[int] | None = None) -> dict:
    """Apply the one-click preflight autofix workflow and return a summary."""
    from services.flight_builder import (
        integrate_college_spillover_into_flights,
        integrate_proam_relay_into_final_flight,
    )
    from services.gear_sharing import complete_one_sided_pairs, parse_all_gear_details
    from services.partner_matching import auto_assign_partners

    # D12-C commit F2: the heat-sync sweep is gone, and with it the
    # `heats_fixed` / `heats_checked` counters this function used to return.
    # It walked every heat calling `sync_assignments`, which rebuilt the
    # `heat_assignments` rows from the `heats.competitors` JSON. That was a
    # repair for a divergence that can no longer happen: commit E made the
    # rows the only place a roster is written, so there is nothing to sync
    # them FROM and nothing they can drift AGAINST. Sweeping anyway would
    # rewrite every roster in the tournament from a column no writer touches.

    gear_parse_result = parse_all_gear_details(tournament)
    pairs_result = complete_one_sided_pairs(tournament)
    partner_summary = auto_assign_partners(tournament)
    # Phase 4: relay BEFORE spillover so Chokerman Run 2 still closes the show.
    relay_result = integrate_proam_relay_into_final_flight(tournament)
    integration = integrate_college_spillover_into_flights(tournament, saturday_ids or [])

    return {
        'gear_parsed': gear_parse_result,
        'gear_pairs_completed': pairs_result['completed'],
        'partner_summary': partner_summary,
        'spillover': integration,
        'relay': relay_result,
    }


def generate_tournament_schedule_artifacts(tournament_id: int) -> dict:
    """Generate heats for every event, then build pro flights when possible.

    Matches the synchronous Run Show "Generate All Heats + Build Flights"
    pipeline: generate heats → build flights → place Pro-Am Relay pseudo-heat
    in the final flight → integrate Saturday college spillover (Chokerman Run 2
    etc.). Without the last two steps the async generate path would produce an
    incomplete schedule — relay unassigned, Chokerman Run 2 with flight_id=NULL.

    Flight build + relay + spillover run with commit=False and commit once at
    the end so the chain is atomic. Mirrors ``_build_flights_async`` in
    ``routes/scheduling/flights.py``.
    """
    from services.flight_builder import (
        build_pro_flights,
        integrate_college_spillover_into_flights,
        integrate_proam_relay_into_final_flight,
    )
    from services.heat_generator import generate_event_heats

    tournament = db.session.get(Tournament, tournament_id)
    if not tournament:
        return {'ok': False, 'error': f'Tournament {tournament_id} not found.'}

    generated = 0
    skipped = 0
    errors = []
    for event in tournament.events.order_by(Event.event_type, Event.name, Event.gender).all():
        try:
            with db.session.begin_nested():
                generate_event_heats(event)
            generated += 1
        except Exception as exc:
            if 'No competitors entered' in str(exc):
                skipped += 1
            else:
                errors.append(str(exc))

    db.session.commit()

    pro_heats = sum(
        1
        for event in tournament.events.filter_by(event_type='pro').all()
        for heat in event.heats.all()
        if heat.run_number == 1
    )
    flights = None
    relay_placed = False
    spillover_integrated = 0
    if pro_heats:
        from routes.scheduling.flights import _resolve_num_flights_from_persisted_config
        num_flights = _resolve_num_flights_from_persisted_config(tournament)
        saturday_college_event_ids = [
            int(i) for i in
            (tournament.get_schedule_config() or {}).get('saturday_college_event_ids', [])
        ]
        try:
            flights = build_pro_flights(
                tournament, num_flights=num_flights, commit=False,
            )
            # Relay BEFORE spillover so Chokerman Run 2 lands AFTER the relay
            # (FlightLogic.md §4.1 show-climax rule).
            relay_result = integrate_proam_relay_into_final_flight(
                tournament, commit=False,
            )
            relay_placed = bool(relay_result.get('placed'))
            integration = integrate_college_spillover_into_flights(
                tournament,
                college_event_ids=saturday_college_event_ids,
                commit=False,
            )
            spillover_integrated = integration.get('integrated_heats', 0)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            errors.append(f'flight build chain failed: {exc}')
            flights = None

    return {
        'ok': True,
        'generated': generated,
        'skipped': skipped,
        'errors': errors,
        'flights': flights,
        'relay_placed': relay_placed,
        'spillover_integrated': spillover_integrated,
    }
