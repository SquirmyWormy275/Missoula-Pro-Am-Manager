"""Application services for schedule autofix and bulk generation workflows."""
from __future__ import annotations

from database import db
from models import Event, HeatAssignment, Tournament


def run_preflight_autofix(tournament: Tournament, saturday_ids: list[int] | None = None) -> dict:
    """Apply the one-click preflight autofix workflow and return a summary."""
    from services.flight_builder import integrate_college_spillover_into_flights
    from services.gear_sharing import complete_one_sided_pairs, parse_all_gear_details
    from services.partner_matching import auto_assign_pro_partners

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

    gear_parse_result = parse_all_gear_details(tournament)
    pairs_result = complete_one_sided_pairs(tournament)
    partner_summary = auto_assign_pro_partners(tournament)
    integration = integrate_college_spillover_into_flights(tournament, saturday_ids or [])

    return {
        'heats_fixed': heats_fixed,
        'gear_parsed': gear_parse_result,
        'gear_pairs_completed': pairs_result['completed'],
        'partner_summary': partner_summary,
        'spillover': integration,
    }


def generate_tournament_schedule_artifacts(tournament_id: int) -> dict:
    """Generate heats for every event, then build pro flights when possible."""
    from services.flight_builder import build_pro_flights
    from services.heat_generator import generate_event_heats

    tournament = db.session.get(Tournament, tournament_id)
    if not tournament:
        return {'ok': False, 'error': f'Tournament {tournament_id} not found.'}

    generated = 0
    skipped = 0
    errors = []
    for event in tournament.events.order_by(Event.event_type, Event.name, Event.gender).all():
        try:
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
    flights = build_pro_flights(tournament) if pro_heats else None
    return {
        'ok': True,
        'generated': generated,
        'skipped': skipped,
        'errors': errors,
        'flights': flights,
    }
