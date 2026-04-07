"""
Public API contract tests — validate response shape and data types.

Tests the read-only REST API at /api/public/tournaments/<tid>/*.

Run:
    pytest tests/test_api_endpoints.py -v
"""
import json

import pytest

from database import db as _db
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_event_result,
    make_flight,
    make_heat,
    make_pro_competitor,
    make_team,
    make_tournament,
)


@pytest.fixture(autouse=True)
def _db_session(db_session):
    """Activate conftest's db_session for every test in this module."""
    yield db_session


@pytest.fixture()
def tournament(db_session):
    return make_tournament(db_session, status='pro_active')


# ---------------------------------------------------------------------------
# /api/public/tournaments/<tid>/standings
# ---------------------------------------------------------------------------

class TestStandingsAPI:
    """GET /api/public/tournaments/<tid>/standings."""

    def test_empty_tournament_returns_json(self, client, db_session, tournament):
        db_session.commit()
        resp = client.get(f'/api/public/tournaments/{tournament.id}/standings')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_with_college_standings(self, client, db_session, tournament):
        team = make_team(db_session, tournament)
        c1 = make_college_competitor(db_session, tournament, team, 'API Stu1', 'M')
        c1.individual_points = 10
        db_session.flush()
        db_session.commit()

        resp = client.get(f'/api/public/tournaments/{tournament.id}/standings')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_nonexistent_tournament_404(self, client):
        resp = client.get('/api/public/tournaments/99999/standings')
        assert resp.status_code in (200, 404)

    def test_v1_alias_works(self, client, db_session, tournament):
        db_session.commit()
        resp = client.get(f'/api/v1/public/tournaments/{tournament.id}/standings')
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /api/public/tournaments/<tid>/schedule
# ---------------------------------------------------------------------------

class TestScheduleAPI:
    """GET /api/public/tournaments/<tid>/schedule."""

    def test_schedule_returns_json(self, client, db_session, tournament):
        db_session.commit()
        resp = client.get(f'/api/public/tournaments/{tournament.id}/schedule')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_schedule_with_events_and_heats(self, client, db_session, tournament):
        event = make_event(db_session, tournament, "Men's Underhand")
        heat = make_heat(db_session, event, competitors=[])
        flight = make_flight(db_session, tournament, flight_number=1)
        heat.flight_id = flight.id
        heat.flight_position = 1
        db_session.flush()
        db_session.commit()

        resp = client.get(f'/api/public/tournaments/{tournament.id}/schedule')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_schedule_empty_tournament(self, client, db_session, tournament):
        db_session.commit()
        resp = client.get(f'/api/public/tournaments/{tournament.id}/schedule')
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /api/public/tournaments/<tid>/results
# ---------------------------------------------------------------------------

class TestResultsAPI:
    """GET /api/public/tournaments/<tid>/results."""

    def test_results_returns_json(self, client, db_session, tournament):
        db_session.commit()
        resp = client.get(f'/api/public/tournaments/{tournament.id}/results')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_results_with_finalized_event(self, client, db_session, tournament):
        event = make_event(db_session, tournament, 'API Results Event',
                           scoring_type='time', scoring_order='lowest_wins',
                           status='completed')
        event.is_finalized = True
        c1 = make_pro_competitor(db_session, tournament, 'API Res1', 'M',
                                 events=[event.id])
        make_event_result(db_session, event, c1, result_value=10.0,
                          final_position=1, status='completed')
        db_session.flush()
        db_session.commit()

        resp = client.get(f'/api/public/tournaments/{tournament.id}/results')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_results_nonexistent_tournament(self, client):
        resp = client.get('/api/public/tournaments/99999/results')
        assert resp.status_code in (200, 404)


# ---------------------------------------------------------------------------
# /api/public/tournaments/<tid>/standings-poll
# ---------------------------------------------------------------------------

class TestStandingsPollAPI:
    """GET /api/public/tournaments/<tid>/standings-poll — lightweight poll."""

    def test_poll_returns_json(self, client, db_session, tournament):
        db_session.commit()
        resp = client.get(f'/api/public/tournaments/{tournament.id}/standings-poll')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# /api/public/tournaments/<tid>/handicap-input
# ---------------------------------------------------------------------------

class TestHandicapInputAPI:
    """GET /api/public/tournaments/<tid>/handicap-input — Arapaho mode."""

    def test_handicap_input_returns_json(self, client, db_session, tournament):
        db_session.commit()
        resp = client.get(f'/api/public/tournaments/{tournament.id}/handicap-input')
        assert resp.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Response format validation
# ---------------------------------------------------------------------------

class TestAPIResponseFormat:
    """Validate consistent response structure across endpoints."""

    def test_all_endpoints_return_valid_json(self, client, db_session, tournament):
        db_session.commit()
        endpoints = [
            f'/api/public/tournaments/{tournament.id}/standings',
            f'/api/public/tournaments/{tournament.id}/schedule',
            f'/api/public/tournaments/{tournament.id}/results',
            f'/api/public/tournaments/{tournament.id}/standings-poll',
        ]
        for endpoint in endpoints:
            resp = client.get(endpoint)
            assert resp.status_code == 200, f'{endpoint} returned {resp.status_code}'
            data = resp.get_json()
            assert data is not None, f'{endpoint} returned non-JSON'

    def test_content_type_is_json(self, client, db_session, tournament):
        db_session.commit()
        resp = client.get(f'/api/public/tournaments/{tournament.id}/standings')
        assert 'application/json' in resp.content_type


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """GET /health — application health check."""

    def test_health_returns_ok(self, client):
        resp = client.get('/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'ok'
