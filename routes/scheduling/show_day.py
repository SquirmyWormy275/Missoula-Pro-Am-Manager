"""
Show Day live operations dashboard route.
"""
from flask import render_template

from models import Event, Flight, Heat, Tournament

from . import scheduling_bp

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
