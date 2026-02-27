"""Minimal REST API endpoints for schedules, standings, and results."""
from flask import Blueprint, jsonify
from models import Event, EventResult, Heat, Tournament

api_bp = Blueprint('api', __name__)


@api_bp.route('/public/tournaments/<int:tournament_id>/standings')
def public_standings(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    payload = {
        'tournament': {'id': tournament.id, 'name': tournament.name, 'year': tournament.year},
        'teams': [
            {'id': team.id, 'team_code': team.team_code, 'points': team.total_points}
            for team in tournament.get_team_standings()
        ],
        'bull': [{'id': c.id, 'name': c.name, 'points': c.individual_points} for c in tournament.get_bull_of_woods(10)],
        'belle': [{'id': c.id, 'name': c.name, 'points': c.individual_points} for c in tournament.get_belle_of_woods(10)],
        'pro_earnings': [
            {'id': c.id, 'name': c.name, 'earnings': c.total_earnings}
            for c in sorted(tournament.pro_competitors.filter_by(status='active').all(), key=lambda x: x.total_earnings, reverse=True)
        ],
    }
    return jsonify(payload)


@api_bp.route('/public/tournaments/<int:tournament_id>/schedule')
def public_schedule(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    events = tournament.events.order_by(Event.event_type, Event.name, Event.gender).all()
    payload = []
    for event in events:
        heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
        payload.append({
            'event_id': event.id,
            'event_name': event.display_name,
            'event_type': event.event_type,
            'status': event.status,
            'heats': [
                {
                    'id': heat.id,
                    'heat_number': heat.heat_number,
                    'run_number': heat.run_number,
                    'status': heat.status,
                    'competitors': heat.get_competitors(),
                    'stand_assignments': heat.get_stand_assignments(),
                    'flight_number': heat.flight.flight_number if heat.flight else None,
                }
                for heat in heats
            ],
        })
    return jsonify({'tournament_id': tournament.id, 'schedule': payload})


@api_bp.route('/public/tournaments/<int:tournament_id>/results')
def public_results(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    completed_events = tournament.events.filter_by(status='completed').order_by(Event.event_type, Event.name, Event.gender).all()
    payload = []
    for event in completed_events:
        rows = event.results.filter_by(status='completed').order_by(EventResult.final_position).all()
        payload.append({
            'event_id': event.id,
            'event_name': event.display_name,
            'event_type': event.event_type,
            'results': [
                {
                    'competitor_id': row.competitor_id,
                    'competitor_name': row.competitor_name,
                    'status': row.status,
                    'result_value': row.result_value,
                    'best_run': row.best_run,
                    'position': row.final_position,
                    'points_awarded': row.points_awarded,
                    'payout_amount': row.payout_amount,
                }
                for row in rows
            ],
        })
    return jsonify({'tournament_id': tournament.id, 'results': payload})

