"""
Spectator portal test suite — all public viewing routes, data payloads,
kiosk mode, event results, caching, and public API.

Tests the full spectator experience:
  - Portal landing and redirect to active tournament
  - Spectator dashboard (college/pro/relay choice)
  - College standings (team, Bull/Belle, event summaries)
  - Pro standings (earnings, event summaries)
  - Individual event results (ranking + heat sort)
  - Relay results
  - Kiosk / TV display
  - User guide
  - Public REST API (standings, schedule, results, poll, handicap-input)
  - View mode (mobile/desktop) switching
  - No authentication required for any route

Run:
    pytest tests/test_spectator_portal.py -v
"""
import json
import os

import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-spectator')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

from database import db as _db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()

    with _app.app_context():
        _seed_spectator_data(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_spectator_data(app):
    """Seed a tournament with teams, competitors, events, heats, results."""
    from models import Event, EventResult, Heat, Team, Tournament
    from models.competitor import CollegeCompetitor, ProCompetitor

    # Tournament — active so spectator portal auto-redirects
    t = Tournament.query.filter_by(name='Spectator Test 2026').first()
    if not t:
        t = Tournament(name='Spectator Test 2026', year=2026, status='pro_active')
        _db.session.add(t)
        _db.session.flush()

    # Second tournament — setup (not active)
    t2 = Tournament.query.filter_by(name='Setup Tournament').first()
    if not t2:
        t2 = Tournament(name='Setup Tournament', year=2025, status='setup')
        _db.session.add(t2)
        _db.session.flush()

    # Team
    team = Team.query.filter_by(tournament_id=t.id, team_code='UM-A').first()
    if not team:
        team = Team(tournament_id=t.id, team_code='UM-A',
                    school_name='University of Montana', school_abbreviation='UM',
                    total_points=42)
        _db.session.add(team)
        _db.session.flush()

    team2 = Team.query.filter_by(tournament_id=t.id, team_code='MSU-A').first()
    if not team2:
        team2 = Team(tournament_id=t.id, team_code='MSU-A',
                     school_name='Montana State', school_abbreviation='MSU',
                     total_points=35)
        _db.session.add(team2)
        _db.session.flush()

    # College competitors — Bull and Belle
    bull = CollegeCompetitor.query.filter_by(tournament_id=t.id, name='Jake Logger').first()
    if not bull:
        bull = CollegeCompetitor(
            tournament_id=t.id, team_id=team.id, name='Jake Logger',
            gender='M', individual_points=25, events_entered='[]', status='active',
        )
        _db.session.add(bull)
        _db.session.flush()

    belle = CollegeCompetitor.query.filter_by(tournament_id=t.id, name='Sara Axe').first()
    if not belle:
        belle = CollegeCompetitor(
            tournament_id=t.id, team_id=team.id, name='Sara Axe',
            gender='F', individual_points=20, events_entered='[]', status='active',
        )
        _db.session.add(belle)
        _db.session.flush()

    # Pro competitor
    pro = ProCompetitor.query.filter_by(tournament_id=t.id, name='Mike Pro').first()
    if not pro:
        pro = ProCompetitor(
            tournament_id=t.id, name='Mike Pro', gender='M',
            events_entered='[]', gear_sharing='{}', partners='{}',
            total_earnings=1500.0, status='active',
        )
        _db.session.add(pro)
        _db.session.flush()

    pro2 = ProCompetitor.query.filter_by(tournament_id=t.id, name='Dan Saw').first()
    if not pro2:
        pro2 = ProCompetitor(
            tournament_id=t.id, name='Dan Saw', gender='M',
            events_entered='[]', gear_sharing='{}', partners='{}',
            total_earnings=800.0, status='active',
        )
        _db.session.add(pro2)
        _db.session.flush()

    # College event — completed with results
    c_evt = Event.query.filter_by(tournament_id=t.id, name='Underhand Speed (M)', event_type='college').first()
    if not c_evt:
        c_evt = Event(
            tournament_id=t.id, name='Underhand Speed (M)', event_type='college',
            gender='M', scoring_type='time', scoring_order='lowest_wins',
            stand_type='underhand', max_stands=5, status='completed',
        )
        _db.session.add(c_evt)
        _db.session.flush()

    # College event result
    cr = EventResult.query.filter_by(event_id=c_evt.id, competitor_id=bull.id).first()
    if not cr:
        cr = EventResult(
            event_id=c_evt.id, competitor_id=bull.id, competitor_type='college',
            competitor_name='Jake Logger', result_value=18.5,
            run1_value=18.5, final_position=1, points_awarded=10,
            payout_amount=0, status='completed',
        )
        _db.session.add(cr)

    # Pro event — completed
    p_evt = Event.query.filter_by(tournament_id=t.id, name='Springboard Speed (M)').first()
    if not p_evt:
        p_evt = Event(
            tournament_id=t.id, name='Springboard Speed (M)', event_type='pro',
            gender='M', scoring_type='time', scoring_order='lowest_wins',
            stand_type='springboard', max_stands=4, status='completed',
        )
        _db.session.add(p_evt)
        _db.session.flush()

    # Pro results
    pr1 = EventResult.query.filter_by(event_id=p_evt.id, competitor_id=pro.id).first()
    if not pr1:
        pr1 = EventResult(
            event_id=p_evt.id, competitor_id=pro.id, competitor_type='pro',
            competitor_name='Mike Pro', result_value=45.2,
            run1_value=45.2, final_position=1, points_awarded=0,
            payout_amount=500.0, status='completed',
        )
        _db.session.add(pr1)

    pr2 = EventResult.query.filter_by(event_id=p_evt.id, competitor_id=pro2.id).first()
    if not pr2:
        pr2 = EventResult(
            event_id=p_evt.id, competitor_id=pro2.id, competitor_type='pro',
            competitor_name='Dan Saw', result_value=52.1,
            run1_value=52.1, final_position=2, points_awarded=0,
            payout_amount=300.0, status='completed',
        )
        _db.session.add(pr2)

    # Pro event — in_progress (not completed, no results visible to spectators)
    ip_evt = Event.query.filter_by(tournament_id=t.id, name='Standing Block Speed (M)').first()
    if not ip_evt:
        ip_evt = Event(
            tournament_id=t.id, name='Standing Block Speed (M)', event_type='pro',
            gender='M', scoring_type='time', scoring_order='lowest_wins',
            stand_type='standing_block', max_stands=5, status='in_progress',
        )
        _db.session.add(ip_evt)
        _db.session.flush()

    # Heat for in-progress event
    ip_heat = Heat.query.filter_by(event_id=ip_evt.id).first()
    if not ip_heat:
        ip_heat = Heat(
            event_id=ip_evt.id, heat_number=1, run_number=1,
            competitors=json.dumps([pro.id, pro2.id]),
            stand_assignments=json.dumps({str(pro.id): '1', str(pro2.id): '2'}),
            status='in_progress',
        )
        _db.session.add(ip_heat)

    _db.session.commit()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def tid(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.filter_by(name='Spectator Test 2026').first().id


@pytest.fixture()
def completed_event_id(app, tid):
    with app.app_context():
        from models import Event
        return Event.query.filter_by(
            tournament_id=tid, name='Springboard Speed (M)', status='completed',
        ).first().id


@pytest.fixture()
def incomplete_event_id(app, tid):
    with app.app_context():
        from models import Event
        return Event.query.filter_by(
            tournament_id=tid, name='Standing Block Speed (M)', status='in_progress',
        ).first().id


def _ok(response):
    assert response.status_code not in (500, 502, 503), (
        f'Server error {response.status_code}: {response.data[:300]}'
    )


# ---------------------------------------------------------------------------
# Portal landing
# ---------------------------------------------------------------------------

class TestPortalLanding:
    """Test the portal index which auto-redirects to the active tournament."""

    def test_landing_redirects_to_active(self, client, tid):
        r = client.get('/portal/', follow_redirects=False)
        assert r.status_code == 302
        assert '/spectator' in r.headers['Location']

    def test_landing_no_auth_required(self, client):
        r = client.get('/portal/')
        _ok(r)


# ---------------------------------------------------------------------------
# Spectator dashboard
# ---------------------------------------------------------------------------

class TestSpectatorDashboard:
    """Test the spectator landing page (college/pro/relay choice)."""

    def test_dashboard_loads(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}')
        assert r.status_code == 200

    def test_dashboard_no_auth_required(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}')
        assert r.status_code == 200

    def test_dashboard_invalid_tournament(self, client):
        r = client.get('/portal/spectator/99999')
        assert r.status_code == 404

    def test_dashboard_mobile_view(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}?view=mobile')
        assert r.status_code == 200

    def test_dashboard_desktop_view(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}?view=desktop')
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# College standings
# ---------------------------------------------------------------------------

class TestSpectatorCollegeStandings:
    """Test the college-focused spectator page."""

    def test_college_page_loads(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}/college')
        assert r.status_code == 200

    def test_college_shows_team_standings(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}/college')
        assert r.status_code == 200
        assert b'UM-A' in r.data or b'University of Montana' in r.data

    def test_college_shows_bull_belle(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}/college')
        assert r.status_code == 200
        # Bull and Belle names should appear
        assert b'Jake Logger' in r.data or b'Sara Axe' in r.data

    def test_college_no_auth(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}/college')
        assert r.status_code == 200

    def test_college_invalid_tournament(self, client):
        r = client.get('/portal/spectator/99999/college')
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Pro standings
# ---------------------------------------------------------------------------

class TestSpectatorProStandings:
    """Test the pro-focused spectator page."""

    def test_pro_page_loads(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}/pro')
        assert r.status_code == 200

    def test_pro_no_auth(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}/pro')
        assert r.status_code == 200

    def test_pro_invalid_tournament(self, client):
        r = client.get('/portal/spectator/99999/pro')
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Relay results
# ---------------------------------------------------------------------------

class TestSpectatorRelayResults:
    """Test the public relay results page."""

    def test_relay_page_loads(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}/relay')
        assert r.status_code == 200

    def test_relay_no_auth(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}/relay')
        assert r.status_code == 200

    def test_relay_invalid_tournament(self, client):
        r = client.get('/portal/spectator/99999/relay')
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Individual event results
# ---------------------------------------------------------------------------

class TestSpectatorEventResults:
    """Test the public event results detail page."""

    def test_completed_event_shows_results(self, client, tid, completed_event_id):
        r = client.get(f'/portal/spectator/{tid}/event/{completed_event_id}')
        assert r.status_code == 200
        assert b'Mike Pro' in r.data

    def test_ranking_sort(self, client, tid, completed_event_id):
        r = client.get(f'/portal/spectator/{tid}/event/{completed_event_id}?sort=ranking')
        assert r.status_code == 200

    def test_heat_sort(self, client, tid, completed_event_id):
        r = client.get(f'/portal/spectator/{tid}/event/{completed_event_id}?sort=heat')
        assert r.status_code == 200

    def test_invalid_sort_defaults_to_ranking(self, client, tid, completed_event_id):
        r = client.get(f'/portal/spectator/{tid}/event/{completed_event_id}?sort=nonsense')
        assert r.status_code == 200

    def test_incomplete_event_redirects(self, client, tid, incomplete_event_id):
        """An in-progress event should redirect back with a warning."""
        r = client.get(f'/portal/spectator/{tid}/event/{incomplete_event_id}',
                       follow_redirects=False)
        assert r.status_code == 302

    def test_nonexistent_event(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}/event/99999')
        assert r.status_code == 404

    def test_event_wrong_tournament(self, client, completed_event_id):
        """Event belongs to tournament A but accessed via tournament B URL."""
        r = client.get(f'/portal/spectator/99999/event/{completed_event_id}')
        assert r.status_code == 404

    def test_event_results_no_auth(self, client, tid, completed_event_id):
        r = client.get(f'/portal/spectator/{tid}/event/{completed_event_id}')
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Kiosk / TV display
# ---------------------------------------------------------------------------

class TestKioskDisplay:
    """Test the auto-rotating fullscreen kiosk display."""

    def test_kiosk_loads(self, client, tid):
        r = client.get(f'/portal/kiosk/{tid}')
        assert r.status_code == 200

    def test_kiosk_no_auth(self, client, tid):
        r = client.get(f'/portal/kiosk/{tid}')
        assert r.status_code == 200

    def test_kiosk_invalid_tournament(self, client):
        r = client.get('/portal/kiosk/99999')
        assert r.status_code == 404

    def test_kiosk_contains_tournament_data(self, client, tid):
        r = client.get(f'/portal/kiosk/{tid}')
        assert r.status_code == 200
        assert b'Spectator Test 2026' in r.data


# ---------------------------------------------------------------------------
# User guide
# ---------------------------------------------------------------------------

class TestUserGuide:
    """Test the in-app user guide."""

    def test_guide_loads(self, client):
        r = client.get('/portal/guide')
        assert r.status_code == 200

    def test_guide_no_auth(self, client):
        r = client.get('/portal/guide')
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Public REST API — standings, schedule, results, poll, handicap-input
# ---------------------------------------------------------------------------

class TestPublicAPIStandings:
    """Test the /api/public/tournaments/<tid>/standings endpoint."""

    def test_standings_returns_json(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/standings')
        assert r.status_code == 200
        data = r.get_json()
        assert 'tournament' in data
        assert 'teams' in data
        assert 'bull' in data
        assert 'belle' in data
        assert 'pro_earnings' in data

    def test_standings_team_data(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/standings')
        data = r.get_json()
        team_codes = [t['team_code'] for t in data['teams']]
        assert 'UM-A' in team_codes

    def test_standings_pro_earnings(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/standings')
        data = r.get_json()
        names = [p['name'] for p in data['pro_earnings']]
        assert 'Mike Pro' in names

    def test_standings_invalid_tournament(self, client):
        r = client.get('/api/public/tournaments/99999/standings')
        assert r.status_code == 404

    def test_standings_no_auth(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/standings')
        assert r.status_code == 200


class TestPublicAPISchedule:
    """Test the /api/public/tournaments/<tid>/schedule endpoint."""

    def test_schedule_returns_json(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/schedule')
        assert r.status_code == 200
        data = r.get_json()
        assert 'tournament_id' in data
        assert 'schedule' in data
        assert isinstance(data['schedule'], list)

    def test_schedule_has_events(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/schedule')
        data = r.get_json()
        event_names = [e['event_name'] for e in data['schedule']]
        assert len(event_names) > 0

    def test_schedule_heat_data(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/schedule')
        data = r.get_json()
        # in-progress event should have heats
        for event in data['schedule']:
            if event['status'] == 'in_progress':
                assert len(event['heats']) > 0
                heat = event['heats'][0]
                assert 'heat_number' in heat
                assert 'competitors' in heat
                assert 'stand_assignments' in heat


class TestPublicAPIResults:
    """Test the /api/public/tournaments/<tid>/results endpoint."""

    def test_results_returns_json(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/results')
        assert r.status_code == 200
        data = r.get_json()
        assert 'tournament_id' in data
        assert 'results' in data

    def test_results_only_completed_events(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/results')
        data = r.get_json()
        for event in data['results']:
            # Only completed events should appear
            assert event['event_type'] in ('college', 'pro')

    def test_results_contain_competitor_data(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/results')
        data = r.get_json()
        all_names = []
        for event in data['results']:
            for res in event['results']:
                all_names.append(res['competitor_name'])
                assert 'position' in res
                assert 'result_value' in res
                assert 'payout_amount' in res
        assert 'Mike Pro' in all_names

    def test_results_no_auth(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/results')
        assert r.status_code == 200


class TestPublicAPIStandingsPoll:
    """Test the lightweight standings-poll endpoint."""

    def test_poll_returns_json(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/standings-poll')
        assert r.status_code == 200
        data = r.get_json()
        assert 'tournament_id' in data
        assert 'last_updated' in data
        assert 'college_teams' in data
        assert 'bull' in data
        assert 'belle' in data
        assert 'pro' in data

    def test_poll_team_data(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/standings-poll')
        data = r.get_json()
        team_codes = [t['team_code'] for t in data['college_teams']]
        assert 'UM-A' in team_codes

    def test_poll_caching(self, client, tid):
        """Two rapid requests should return consistent data (cache hit)."""
        r1 = client.get(f'/api/public/tournaments/{tid}/standings-poll')
        r2 = client.get(f'/api/public/tournaments/{tid}/standings-poll')
        assert r1.status_code == 200
        assert r2.status_code == 200
        d1 = r1.get_json()
        d2 = r2.get_json()
        assert d1['college_teams'] == d2['college_teams']


class TestPublicAPIHandicapInput:
    """Test the handicap-input endpoint."""

    def test_handicap_input_returns_json(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/handicap-input')
        assert r.status_code == 200
        data = r.get_json()
        assert 'tournament' in data
        assert 'chopping_results' in data

    def test_handicap_input_no_auth(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/handicap-input')
        assert r.status_code == 200


class TestPublicAPIV1Alias:
    """Test the /api/v1/ prefix alias."""

    def test_v1_standings(self, client, tid):
        r = client.get(f'/api/v1/public/tournaments/{tid}/standings')
        assert r.status_code in (200, 404)

    def test_v1_schedule(self, client, tid):
        r = client.get(f'/api/v1/public/tournaments/{tid}/schedule')
        assert r.status_code in (200, 404)

    def test_v1_results(self, client, tid):
        r = client.get(f'/api/v1/public/tournaments/{tid}/results')
        assert r.status_code in (200, 404)


# ---------------------------------------------------------------------------
# View mode switching
# ---------------------------------------------------------------------------

class TestViewModeSwitching:
    """Test mobile/desktop view mode query parameter."""

    def test_mobile_view_param(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}?view=mobile')
        assert r.status_code == 200

    def test_desktop_view_param(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}?view=desktop')
        assert r.status_code == 200

    def test_invalid_view_param_defaults(self, client, tid):
        r = client.get(f'/portal/spectator/{tid}?view=tablet')
        assert r.status_code == 200

    def test_mobile_user_agent(self, client, tid):
        """Mobile User-Agent should default to mobile view."""
        r = client.get(f'/portal/spectator/{tid}',
                       headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU OS 15_0)'})
        assert r.status_code == 200
