"""Minimal REST API endpoints for schedules, standings, and results."""
import json
import threading
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, Response, current_app, jsonify, stream_with_context

from database import db
from services.time_utils import utc_now_naive


def _json_default(obj):
    """JSON encoder fallback for types the stdlib encoder doesn't handle.

    Decimal appears in payload dicts because EventResult.points_awarded and
    related columns are Numeric (Phase 1B migration) — json.dumps raises
    TypeError on Decimal without this. Flask's own jsonify handles Decimal
    via DefaultJSONProvider, but the SSE stream path uses raw json.dumps.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f'Object of type {obj.__class__.__name__} is not JSON serializable')

from flask import request

# One shared extension exists before route decoration; missing dependencies fail closed.
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import current_user

from models import Event, EventResult, Heat, Team, Tournament
from models.competitor import ProCompetitor
from services.handicap_export import build_chopping_rows
from services.report_cache import get as cache_get
from services.report_cache import get_or_compute as cache_get_or_compute

api_bp = Blueprint('api', __name__)


def _actor_limit_key():
    if current_user.is_authenticated:
        return f'user:{current_user.id}'
    return f'ip:{get_remote_address()}'


_limiter = Limiter(key_func=get_remote_address, default_limits=[])
_write_limiter = _limiter
_limiter.limit('60 per minute; 600 per hour')(api_bp)


@_limiter.request_filter
def _rate_limits_disabled():
    return not current_app.config.get('RATELIMIT_ENABLED', True)


def _init_limiter(app):
    # The supported production process is one worker (Procfile/background_jobs).
    # A shared store can be supplied when moving to a multi-instance deployment.
    import os
    app.config.setdefault('RATELIMIT_STORAGE_URI', os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'))
    _limiter.init_app(app)


def _init_write_limiter(app):
    # Kept for existing factory callers; both kinds share the same extension.
    pass


def write_limit(rate: str = '30 per minute'):
    return _limiter.limit(rate, key_func=_actor_limit_key)


def credential_limit(*fields):
    """Bound attempts per account/resource, including attempts from changing IPs."""
    import hashlib
    def key():
        values = []
        for field in fields:
            source = request.form if field == 'username' else request.values
            if field in {'tournament_id', 'competitor_id'}:
                value = source.get(field, type=int)
            else:
                value = str(source.get(field, '')).strip().casefold()
            values.append(value)
        return hashlib.sha256(repr(values).encode()).hexdigest()
    return _limiter.limit('5 per minute; 30 per hour', key_func=key, methods=['POST'])


def _limit(rate: str):
    return _limiter.limit(rate)


@api_bp.route('/public/tournaments/<int:tournament_id>/standings')
@_limit('120 per minute')
def public_standings(tournament_id):
    tournament = db.get_or_404(Tournament, tournament_id)
    pro_earnings_rows = (
        ProCompetitor.query
        .filter_by(tournament_id=tournament.id, status='active')
        .order_by(ProCompetitor.total_earnings.desc(), ProCompetitor.name)
        .all()
    )
    payload = {
        'tournament': {'id': tournament.id, 'name': tournament.name, 'year': tournament.year},
        'teams': [
            {'id': team.id, 'team_code': team.team_code, 'points': team.total_points}
            for team in tournament.get_team_standings()
        ],
        'bull': [{'id': c.id, 'name': c.display_name, 'points': c.individual_points} for c in tournament.get_bull_of_woods(10)],
        'belle': [{'id': c.id, 'name': c.display_name, 'points': c.individual_points} for c in tournament.get_belle_of_woods(10)],
        'pro_earnings': [
            {'id': c.id, 'name': c.name, 'earnings': c.total_earnings}
            for c in pro_earnings_rows
        ],
    }
    return jsonify(payload)


@api_bp.route('/public/tournaments/<int:tournament_id>/schedule')
def public_schedule(tournament_id):
    tournament = db.get_or_404(Tournament, tournament_id)
    events = tournament.events.order_by(Event.event_type, Event.name, Event.gender).all()
    event_ids = [e.id for e in events]

    # Batch-load all heats for all events in one query — avoids N+1
    all_heats = (
        Heat.query
        .filter(Heat.event_id.in_(event_ids))
        .order_by(Heat.event_id, Heat.heat_number, Heat.run_number)
        .all()
    ) if event_ids else []
    heats_by_event = defaultdict(list)
    for heat in all_heats:
        heats_by_event[heat.event_id].append(heat)

    payload = []
    for event in events:
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
                for heat in heats_by_event[event.id]
            ],
        })
    return jsonify({'tournament_id': tournament.id, 'schedule': payload})


@api_bp.route('/public/tournaments/<int:tournament_id>/results')
def public_results(tournament_id):
    tournament = db.get_or_404(Tournament, tournament_id)
    completed_events = tournament.events.filter_by(status='completed').order_by(Event.event_type, Event.name, Event.gender).all()
    event_ids = [e.id for e in completed_events]

    # Batch-load all completed results in one query — avoids N+1
    all_results = (
        EventResult.query
        .filter(
            EventResult.event_id.in_(event_ids),
            EventResult.status == 'completed',
        )
        .order_by(EventResult.event_id, EventResult.final_position)
        .all()
    ) if event_ids else []
    results_by_event = defaultdict(list)
    for row in all_results:
        results_by_event[row.event_id].append(row)

    payload = []
    for event in completed_events:
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
                for row in results_by_event[event.id]
            ],
        })
    return jsonify({'tournament_id': tournament.id, 'results': payload})


# ---------------------------------------------------------------------------
# #11 — Live leaderboard polling endpoint
# ---------------------------------------------------------------------------

@api_bp.route('/public/tournaments/<int:tournament_id>/standings-poll')
@_limit('120 per minute')
def standings_poll(tournament_id):
    """Lightweight polling endpoint for live leaderboard auto-refresh."""
    tournament = db.get_or_404(Tournament, tournament_id)
    cache_key = f'api:standings-poll:{tournament_id}'
    ttl_seconds = max(1, int(current_app.config.get('PUBLIC_CACHE_TTL_SECONDS', 5)))

    def build_payload():
        teams = [
            {
                'id': team.id,
                'team_code': team.team_code,
                'school_name': team.school_name,
                'points': team.total_points,
            }
            for team in (
                Team.query
                .filter_by(tournament_id=tournament.id, status='active')
                .order_by(Team.total_points.desc(), Team.team_code)
                .limit(15)
                .all()
            )
        ]
        bull = [
            {'id': c.id, 'name': c.display_name, 'points': c.individual_points}
            for c in tournament.get_bull_of_woods(10)
        ]
        belle = [
            {'id': c.id, 'name': c.display_name, 'points': c.individual_points}
            for c in tournament.get_belle_of_woods(10)
        ]
        pro_data = [
            {'id': c.id, 'name': c.name, 'earnings': c.total_earnings or 0}
            for c in (
                ProCompetitor.query
                .filter_by(tournament_id=tournament.id, status='active')
                .order_by(ProCompetitor.total_earnings.desc(), ProCompetitor.name)
                .limit(15)
                .all()
            )
        ]
        return {
            'tournament_id': tournament_id,
            'last_updated': utc_now_naive().isoformat() + 'Z',
            'college_teams': teams,
            'bull': bull,
            'belle': belle,
            'pro': pro_data,
        }

    payload = cache_get_or_compute(cache_key, ttl_seconds, build_payload)
    return jsonify(payload)


# ---------------------------------------------------------------------------
# #13 — Server-Sent Events: live standings push
# ---------------------------------------------------------------------------

@api_bp.route('/public/tournaments/<int:tournament_id>/standings-stream')
def standings_stream(tournament_id):
    """Retire long-lived WSGI streams; the spectator UI uses standings-poll."""
    # The four-thread WSGI process must never park threads for public streams.
    # Spectators already use standings-poll; a retired stream fails immediately.
    db.get_or_404(Tournament, tournament_id)
    return jsonify({
        'error': 'Live streams are unavailable. Use standings-poll.',
        'poll_url': f'/api/public/tournaments/{tournament_id}/standings-poll',
    }), 410


@api_bp.route('/public/tournaments/<int:tournament_id>/handicap-input')
def handicap_input(tournament_id):
    """
    Public payload intended for handicap portal integrations.
    Includes chopping-only historical rows for model ingestion.
    """
    tournament = db.get_or_404(Tournament, tournament_id)
    return jsonify({
        'tournament': {
            'id': tournament.id,
            'name': tournament.name,
            'year': tournament.year,
        },
        'chopping_results': build_chopping_rows(tournament),
    })
