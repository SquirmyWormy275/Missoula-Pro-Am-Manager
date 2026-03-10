"""Minimal REST API endpoints for schedules, standings, and results."""
import json
import threading
import time
from collections import defaultdict
from datetime import datetime
from flask import Blueprint, Response, current_app, jsonify, stream_with_context
from models import Event, EventResult, Heat, Team, Tournament
from models.competitor import ProCompetitor
from services.report_cache import get as cache_get, set as cache_set
from services.handicap_export import build_chopping_rows

# ---------------------------------------------------------------------------
# SSE connection counter — caps concurrent long-lived streams at SSE_MAX_CONNECTIONS
# to prevent thread exhaustion on gunicorn/gevent workers.
# ---------------------------------------------------------------------------
_sse_connection_count = 0
_sse_lock = threading.Lock()
SSE_MAX_CONNECTIONS = 150

api_bp = Blueprint('api', __name__)

# ---------------------------------------------------------------------------
# Write-endpoint rate limiter — attached in create_app() via _init_write_limiter.
# Applies to POST/PUT/DELETE routes on management blueprints.
# Falls back to a no-op if flask-limiter is not installed.
# ---------------------------------------------------------------------------
_write_limiter = None

def _init_write_limiter(app):
    """Optionally create a shared Limiter for management write routes."""
    global _write_limiter
    try:
        from flask_limiter import Limiter  # type: ignore
        from flask_limiter.util import get_remote_address  # type: ignore
        _write_limiter = Limiter(
            key_func=get_remote_address,
            default_limits=[],
            storage_uri='memory://',
        )
        _write_limiter.init_app(app)
    except ImportError:
        _write_limiter = None

def write_limit(rate: str = '30 per minute'):
    """Decorator applying a write-specific rate limit. No-op when limiter unavailable."""
    import functools
    def decorator(f):
        if _write_limiter is not None:
            return _write_limiter.limit(rate)(f)
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ---------------------------------------------------------------------------
# Rate limiting — gracefully no-ops if flask-limiter is not installed.
# Set RATELIMIT_STORAGE_URI in env (e.g. "memory://") or rely on the default.
# Default limits: 60 requests/minute, 600/hour per remote IP.
# ---------------------------------------------------------------------------
try:
    from flask_limiter import Limiter  # type: ignore
    from flask_limiter.util import get_remote_address  # type: ignore

    _limiter = Limiter(
        key_func=get_remote_address,
        default_limits=['600 per hour', '60 per minute'],
        storage_uri='memory://',
    )

    def _init_limiter(app):
        """Attach the limiter to the Flask app — call from create_app()."""
        _limiter.init_app(app)

    def _limit(rate: str):
        """Decorator applying a specific rate limit to a route."""
        return _limiter.limit(rate)

except ImportError:
    import functools

    def _init_limiter(app):  # type: ignore[misc]
        pass

    def _limit(rate: str):  # type: ignore[misc]
        """No-op decorator when flask-limiter is not installed."""
        def decorator(f):
            @functools.wraps(f)
            def wrapper(*args, **kwargs):
                return f(*args, **kwargs)
            return wrapper
        return decorator


@api_bp.route('/public/tournaments/<int:tournament_id>/standings')
@_limit('120 per minute')
def public_standings(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
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
        'bull': [{'id': c.id, 'name': c.name, 'points': c.individual_points} for c in tournament.get_bull_of_woods(10)],
        'belle': [{'id': c.id, 'name': c.name, 'points': c.individual_points} for c in tournament.get_belle_of_woods(10)],
        'pro_earnings': [
            {'id': c.id, 'name': c.name, 'earnings': c.total_earnings}
            for c in pro_earnings_rows
        ],
    }
    return jsonify(payload)


@api_bp.route('/public/tournaments/<int:tournament_id>/schedule')
def public_schedule(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
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
    tournament = Tournament.query.get_or_404(tournament_id)
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
    tournament = Tournament.query.get_or_404(tournament_id)
    cache_key = f'api:standings-poll:{tournament_id}'
    ttl_seconds = max(1, int(current_app.config.get('PUBLIC_CACHE_TTL_SECONDS', 5)))
    cached = cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    # College team standings
    teams = []
    top_teams = (
        Team.query
        .filter_by(tournament_id=tournament.id, status='active')
        .order_by(Team.total_points.desc(), Team.team_code)
        .limit(15)
        .all()
    )
    for team in top_teams:
        teams.append({
            'id': team.id,
            'team_code': team.team_code,
            'school_name': team.school_name,
            'points': team.total_points,
        })

    # Bull/Belle of the Woods top 10
    bull = [
        {'id': c.id, 'name': c.name, 'points': c.individual_points}
        for c in tournament.get_bull_of_woods(10)
    ]
    belle = [
        {'id': c.id, 'name': c.name, 'points': c.individual_points}
        for c in tournament.get_belle_of_woods(10)
    ]

    # Pro top earners
    pro = (
        ProCompetitor.query
        .filter_by(tournament_id=tournament.id, status='active')
        .order_by(ProCompetitor.total_earnings.desc(), ProCompetitor.name)
        .limit(15)
        .all()
    )
    pro_data = [
        {'id': c.id, 'name': c.name, 'earnings': c.total_earnings or 0}
        for c in pro
    ]

    payload = {
        'tournament_id': tournament_id,
        'last_updated': datetime.utcnow().isoformat() + 'Z',
        'college_teams': teams,
        'bull': bull,
        'belle': belle,
        'pro': pro_data,
    }
    cache_set(cache_key, payload, ttl_seconds)
    return jsonify(payload)


# ---------------------------------------------------------------------------
# #13 — Server-Sent Events: live standings push
# ---------------------------------------------------------------------------

@api_bp.route('/public/tournaments/<int:tournament_id>/standings-stream')
def standings_stream(tournament_id):
    """
    SSE endpoint for real-time leaderboard updates.

    The browser connects once and receives push events as scores are entered.
    Replaces the 30-second polling loop in spectator_college.html when JS
    EventSource is supported.

    Events:
      data: <JSON payload>  — same shape as standings-poll
      : keep-alive comment  — sent every 15s when no data changes

    Clients should reconnect automatically (EventSource does this by default).
    Max stream duration: 5 minutes to avoid long-lived connections on Railway.

    Connection cap: returns HTTP 503 with Retry-After: 10 when SSE_MAX_CONNECTIONS
    is reached, preventing thread exhaustion under heavy spectator load.
    """
    global _sse_connection_count

    # Enforce connection cap before opening the stream.
    with _sse_lock:
        if _sse_connection_count >= SSE_MAX_CONNECTIONS:
            return jsonify({
                'error': 'Too many live connections. Please retry in a few seconds.',
                'retry_after': 10,
            }), 503, {'Retry-After': '10'}
        _sse_connection_count += 1

    app = current_app._get_current_object()
    ttl_seconds = max(1, int(app.config.get('PUBLIC_CACHE_TTL_SECONDS', 5)))
    max_duration = 300  # 5 minutes
    poll_interval = 5   # seconds between DB checks
    cache_key = f'api:standings-poll:{tournament_id}'

    @stream_with_context
    def _generate():
        global _sse_connection_count
        try:
            start = time.monotonic()
            last_payload_json = None
            while time.monotonic() - start < max_duration:
                # Read from the shared standings-poll cache populated by standings_poll().
                # Falls back to None when cache is cold (first poll hasn't fired yet).
                payload = cache_get(cache_key)
                if payload is None:
                    # Cache miss — yield keep-alive and let standings_poll warm the cache.
                    yield ': keep-alive\n\n'
                    time.sleep(poll_interval)
                    continue
                payload_json = json.dumps(payload)
                if payload_json != last_payload_json:
                    last_payload_json = payload_json
                    yield f'data: {payload_json}\n\n'
                else:
                    # Keep-alive comment so the connection doesn't time out.
                    yield ': keep-alive\n\n'
                time.sleep(poll_interval)
        finally:
            # Decrement connection count when the stream ends (client disconnect or timeout).
            with _sse_lock:
                _sse_connection_count = max(0, _sse_connection_count - 1)

    return Response(_generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',  # Disable Nginx buffering for SSE
    })


@api_bp.route('/public/tournaments/<int:tournament_id>/handicap-input')
def handicap_input(tournament_id):
    """
    Public payload intended for handicap portal integrations.
    Includes chopping-only historical rows for model ingestion.
    """
    tournament = Tournament.query.get_or_404(tournament_id)
    return jsonify({
        'tournament': {
            'id': tournament.id,
            'name': tournament.name,
            'year': tournament.year,
        },
        'chopping_results': build_chopping_rows(tournament),
    })
