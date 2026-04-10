"""
Heat sheet and day schedule print routes, plus schedule hydration helpers.
"""
from flask import redirect, render_template, session, url_for

from database import db
from models import Event, EventResult, Flight, Heat, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor

from . import _load_competitor_lookup, scheduling_bp


def _hydrate_schedule_for_display(tournament: Tournament, schedule: dict) -> dict:
    """Attach heat + stand assignment details to schedule entries for display/print."""
    return {
        'friday_day': _hydrate_schedule_entries(tournament, schedule.get('friday_day', [])),
        'friday_feature': _hydrate_schedule_entries(tournament, schedule.get('friday_feature', [])),
        'saturday_show': _hydrate_schedule_entries(tournament, schedule.get('saturday_show', [])),
    }


def _hydrate_schedule_entries(tournament: Tournament, entries: list) -> list:
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
            'name': comp.display_name if comp else f'Unknown ({comp_id})',
            'stand': assignments.get(str(comp_id)),
        })
    return {
        'heat_id': heat.id,
        'heat_number': heat.heat_number,
        'run_number': heat.run_number,
        'competitors': competitors,
    }


# ---------------------------------------------------------------------------
# #7 — Heat sheet print page
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/heat-sheets')
def heat_sheets(tournament_id):
    """Print-ready heat sheets for all flights and events."""
    from datetime import datetime

    from services.flight_builder import _STAND_CONFLICT_GAP

    tournament = Tournament.query.get_or_404(tournament_id)

    # Build {(event_id, competitor_id): status} for SCR/DNF indicators on heat sheets
    result_status = {
        (r.event_id, r.competitor_id): r.status
        for r in EventResult.query.join(Event).filter(Event.tournament_id == tournament_id).all()
    }

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
                     'stand': assignments.get(str(cid), '?'),
                     'status': result_status.get((event.id, cid), 'pending')}
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
    birling_brackets = []
    for event in tournament.events.order_by(Event.event_type, Event.name).all():
        # Birling bracket events get special treatment — show bracket, not heat cards.
        if event.scoring_type == 'bracket':
            from services.birling_bracket import BirlingBracket
            bb = BirlingBracket(event)
            bdata = bb.bracket_data
            has_bracket = bool(bdata.get('bracket', {}).get('winners'))
            if has_bracket:
                comp_lookup = {str(c['id']): c['name'] for c in bdata.get('competitors', [])}
                birling_brackets.append({
                    'event': event,
                    'bracket': bdata.get('bracket', {}),
                    'comp_lookup': comp_lookup,
                    'placements': bdata.get('placements', {}),
                    'current_matches': bb.get_current_matches(),
                })
            continue

        event_heats = event.heats.filter_by(flight_id=None).order_by(
            Heat.heat_number, Heat.run_number).all()
        if not event_heats:
            continue
        heat_rows = []
        for heat in event_heats:
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
                     'stand': assignments.get(str(cid), '?'),
                     'status': result_status.get((event.id, cid), 'pending')}
                    for cid in comp_ids
                ],
            })
        no_flight_heats.append({'event': event, 'heats': heat_rows})

    return render_template(
        'scheduling/heat_sheets_print.html',
        tournament=tournament,
        flight_data=flight_data,
        no_flight_heats=no_flight_heats,
        birling_brackets=birling_brackets,
        now=datetime.utcnow(),
        stand_conflict_gap=_STAND_CONFLICT_GAP,
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
