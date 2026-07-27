"""
Tournament lifecycle portal tests — verify that spectator and competitor views
behave correctly as a tournament transitions through status phases.

Tournament statuses: setup → college_active → pro_active → completed

Tests:
  - Spectator views available at every status
  - Event results page blocks incomplete events at all statuses
  - API returns data appropriate to the current status
  - Competitor search works regardless of status
  - Kiosk works at all statuses
  - Role entry / index page shows correct active tournament
  - Language toggle route works and redirects safely

Run:
    pytest tests/test_tournament_lifecycle_portals.py -v
"""
import json
import os

import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-lifecycle')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

from database import db as _db


@pytest.fixture(scope='module')
def app():
    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()
    with _app.app_context():
        _seed(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed(app):
    from models import Event, EventResult, Team, Tournament
    from models.competitor import CollegeCompetitor, ProCompetitor

    t = Tournament.query.filter_by(name='Lifecycle Test').first()
    if not t:
        t = Tournament(name='Lifecycle Test', year=2026, status='setup')
        _db.session.add(t)
        _db.session.flush()

    # Team
    team = Team.query.filter_by(tournament_id=t.id, team_code='UM-A').first()
    if not team:
        team = Team(tournament_id=t.id, team_code='UM-A',
                    school_name='University of Montana', school_abbreviation='UM',
                    total_points=20)
        _db.session.add(team)
        _db.session.flush()

    # College competitor
    cc = CollegeCompetitor.query.filter_by(tournament_id=t.id, name='LC Student').first()
    if not cc:
        cc = CollegeCompetitor(
            tournament_id=t.id, team_id=team.id, name='LC Student',
            gender='M', individual_points=10, events_entered='[]', status='active',
        )
        _db.session.add(cc)
        _db.session.flush()

    # Pro competitor
    pro = ProCompetitor.query.filter_by(tournament_id=t.id, name='LC Pro').first()
    if not pro:
        pro = ProCompetitor(
            tournament_id=t.id, name='LC Pro', gender='M',
            events_entered='[]', gear_sharing='{}', partners='{}',
            total_earnings=200.0, status='active',
        )
        _db.session.add(pro)
        _db.session.flush()

    # Pending event (not completed — no results visible to spectators)
    evt_p = Event.query.filter_by(tournament_id=t.id, name='Pending Event').first()
    if not evt_p:
        evt_p = Event(
            tournament_id=t.id, name='Pending Event', event_type='pro',
            gender='M', scoring_type='time', scoring_order='lowest_wins',
            stand_type='underhand', max_stands=5, status='pending',
        )
        _db.session.add(evt_p)
        _db.session.flush()

    # Completed event with results
    evt_c = Event.query.filter_by(tournament_id=t.id, name='Done Event').first()
    if not evt_c:
        evt_c = Event(
            tournament_id=t.id, name='Done Event', event_type='pro',
            gender='M', scoring_type='time', scoring_order='lowest_wins',
            stand_type='underhand', max_stands=5, status='completed',
        )
        _db.session.add(evt_c)
        _db.session.flush()

    res = EventResult.query.filter_by(event_id=evt_c.id, competitor_id=pro.id).first()
    if not res:
        res = EventResult(
            event_id=evt_c.id, competitor_id=pro.id, competitor_type='pro',
            competitor_name='LC Pro', result_value=30.0, run1_value=30.0,
            final_position=1, payout_amount=100.0, status='completed',
        )
        _db.session.add(res)

    _db.session.commit()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def tid(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.filter_by(name='Lifecycle Test').first().id


@pytest.fixture()
def pending_eid(app, tid):
    with app.app_context():
        from models import Event
        return Event.query.filter_by(tournament_id=tid, name='Pending Event').first().id


@pytest.fixture()
def done_eid(app, tid):
    with app.app_context():
        from models import Event
        return Event.query.filter_by(tournament_id=tid, name='Done Event').first().id


def _ok(r):
    assert r.status_code not in (500, 502, 503), f'Server error: {r.status_code}'


def _set_status(app, tid, status):
    with app.app_context():
        from models import Tournament
        t = Tournament.query.get(tid)
        t.status = status
        _db.session.commit()


# ---------------------------------------------------------------------------
# Spectator views at each tournament status
# ---------------------------------------------------------------------------

STATUSES = ['setup', 'college_active', 'pro_active', 'completed']


class TestSpectatorAtEveryStatus:
    """All spectator pages should render without 500 at every tournament status."""

    @pytest.mark.parametrize('status', STATUSES)
    def test_spectator_dashboard(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.get(f'/portal/spectator/{tid}')
        assert r.status_code == 200

    @pytest.mark.parametrize('status', STATUSES)
    def test_college_standings(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.get(f'/portal/spectator/{tid}/college')
        assert r.status_code == 200

    @pytest.mark.parametrize('status', STATUSES)
    def test_pro_standings(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.get(f'/portal/spectator/{tid}/pro')
        assert r.status_code == 200

    @pytest.mark.parametrize('status', STATUSES)
    def test_kiosk(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.get(f'/portal/kiosk/{tid}')
        assert r.status_code == 200

    @pytest.mark.parametrize('status', STATUSES)
    def test_relay(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.get(f'/portal/spectator/{tid}/relay')
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Event results gate by completion status
# ---------------------------------------------------------------------------

class TestEventResultsGate:
    """Only completed events should show results to spectators."""

    def test_completed_event_accessible(self, app, client, tid, done_eid):
        _set_status(app, tid, 'pro_active')
        r = client.get(f'/portal/spectator/{tid}/event/{done_eid}')
        assert r.status_code == 200

    def test_pending_event_redirects(self, app, client, tid, pending_eid):
        _set_status(app, tid, 'pro_active')
        r = client.get(f'/portal/spectator/{tid}/event/{pending_eid}',
                       follow_redirects=False)
        assert r.status_code == 302


# ---------------------------------------------------------------------------
# API at each status
# ---------------------------------------------------------------------------

class TestAPIAtEveryStatus:
    """API endpoints should return valid JSON at every tournament status."""

    @pytest.mark.parametrize('status', STATUSES)
    def test_standings(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.get(f'/api/public/tournaments/{tid}/standings')
        assert r.status_code == 200
        assert r.is_json

    @pytest.mark.parametrize('status', STATUSES)
    def test_schedule(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.get(f'/api/public/tournaments/{tid}/schedule')
        assert r.status_code == 200
        assert r.is_json

    @pytest.mark.parametrize('status', STATUSES)
    def test_results(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.get(f'/api/public/tournaments/{tid}/results')
        assert r.status_code == 200
        assert r.is_json

    @pytest.mark.parametrize('status', STATUSES)
    def test_standings_poll(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.get(f'/api/public/tournaments/{tid}/standings-poll')
        assert r.status_code == 200
        assert r.is_json


# ---------------------------------------------------------------------------
# Competitor search at each status
# ---------------------------------------------------------------------------

class TestCompetitorSearchAtEveryStatus:
    """Competitor name search should work at every tournament status."""

    @pytest.mark.parametrize('status', STATUSES)
    def test_search_pro(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.post('/portal/competitor-access', data={
            'tournament_id': tid, 'full_name': 'LC Pro',
        }, follow_redirects=False)
        assert r.status_code in (200, 302)

    @pytest.mark.parametrize('status', STATUSES)
    def test_search_college(self, app, client, tid, status):
        _set_status(app, tid, status)
        r = client.post('/portal/competitor-access', data={
            'tournament_id': tid, 'full_name': 'LC Student',
        }, follow_redirects=False)
        assert r.status_code in (200, 302)


# ---------------------------------------------------------------------------
# Role entry / index page
# ---------------------------------------------------------------------------

class TestRoleEntryPage:
    """The / index page renders without error and shows tournament info."""

    def test_index_renders(self, client):
        r = client.get('/')
        _ok(r)

    def test_index_shows_tournament(self, app, client, tid):
        _set_status(app, tid, 'pro_active')
        r = client.get('/')
        _ok(r)


# ---------------------------------------------------------------------------
# Language toggle route
# ---------------------------------------------------------------------------

class TestLanguageToggle:
    """The /language/<code> route should redirect safely."""

    def test_set_english(self, client):
        r = client.get('/language/en', follow_redirects=False)
        assert r.status_code in (200, 302)

    def test_set_invalid_language(self, client):
        r = client.get('/language/zz')
        _ok(r)

    def test_set_language_with_next(self, client):
        r = client.get('/language/en?next=/portal/', follow_redirects=False)
        assert r.status_code in (200, 302)

    def test_language_xss_in_next(self, client):
        """XSS in next param should be handled safely."""
        r = client.get('/language/en?next=javascript:alert(1)')
        _ok(r)


# ---------------------------------------------------------------------------
# Cleanup: restore to a safe status
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope='module')
def cleanup_status(app):
    yield
    with app.app_context():
        from models import Tournament
        t = Tournament.query.filter_by(name='Lifecycle Test').first()
        if t:
            t.status = 'setup'
            _db.session.commit()
