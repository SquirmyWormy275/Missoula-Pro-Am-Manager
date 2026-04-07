"""
Empty state portal tests — verify spectator, competitor, and API views handle
tournaments with no data gracefully (no 500 errors, appropriate messages).

Tests:
  - Spectator pages with no teams, no competitors, no events, no results
  - API endpoints with empty tournament
  - Kiosk with no data
  - Competitor search with no competitors registered
  - School search with no teams
  - Portal landing with no active tournament
  - College/pro standings with zero points

Run:
    pytest tests/test_empty_state_portals.py -v
"""
import os

import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-empty')
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
    from models import Tournament

    # Empty tournament — no teams, no competitors, no events
    t = Tournament.query.filter_by(name='Empty Tournament').first()
    if not t:
        t = Tournament(name='Empty Tournament', year=2026, status='setup')
        _db.session.add(t)

    # Active but empty tournament
    t2 = Tournament.query.filter_by(name='Active Empty').first()
    if not t2:
        t2 = Tournament(name='Active Empty', year=2026, status='pro_active')
        _db.session.add(t2)

    _db.session.commit()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def empty_tid(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.filter_by(name='Empty Tournament').first().id


@pytest.fixture()
def active_empty_tid(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.filter_by(name='Active Empty').first().id


def _ok(r):
    assert r.status_code not in (500, 502, 503), f'Server error: {r.status_code}'


# ---------------------------------------------------------------------------
# Spectator views with empty tournament
# ---------------------------------------------------------------------------

class TestEmptySpectatorDashboard:
    """Spectator pages should render cleanly with no data."""

    def test_spectator_dashboard_empty(self, client, empty_tid):
        r = client.get(f'/portal/spectator/{empty_tid}')
        assert r.status_code == 200

    def test_college_standings_empty(self, client, empty_tid):
        r = client.get(f'/portal/spectator/{empty_tid}/college')
        assert r.status_code == 200

    def test_pro_standings_empty(self, client, empty_tid):
        r = client.get(f'/portal/spectator/{empty_tid}/pro')
        assert r.status_code == 200

    def test_relay_empty(self, client, empty_tid):
        r = client.get(f'/portal/spectator/{empty_tid}/relay')
        assert r.status_code == 200

    def test_kiosk_empty(self, client, empty_tid):
        r = client.get(f'/portal/kiosk/{empty_tid}')
        assert r.status_code == 200

    def test_active_empty_spectator(self, client, active_empty_tid):
        r = client.get(f'/portal/spectator/{active_empty_tid}')
        assert r.status_code == 200

    def test_active_empty_college(self, client, active_empty_tid):
        r = client.get(f'/portal/spectator/{active_empty_tid}/college')
        assert r.status_code == 200

    def test_active_empty_pro(self, client, active_empty_tid):
        r = client.get(f'/portal/spectator/{active_empty_tid}/pro')
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# API with empty tournament
# ---------------------------------------------------------------------------

class TestEmptyAPI:
    """API endpoints should return valid JSON with empty arrays."""

    def test_standings_empty(self, client, empty_tid):
        r = client.get(f'/api/public/tournaments/{empty_tid}/standings')
        assert r.status_code == 200
        data = r.get_json()
        assert data['teams'] == []
        assert data['bull'] == []
        assert data['belle'] == []
        assert data['pro_earnings'] == []

    def test_schedule_empty(self, client, empty_tid):
        r = client.get(f'/api/public/tournaments/{empty_tid}/schedule')
        assert r.status_code == 200
        data = r.get_json()
        assert data['schedule'] == []

    def test_results_empty(self, client, empty_tid):
        r = client.get(f'/api/public/tournaments/{empty_tid}/results')
        assert r.status_code == 200
        data = r.get_json()
        assert data['results'] == []

    def test_standings_poll_empty(self, client, empty_tid):
        r = client.get(f'/api/public/tournaments/{empty_tid}/standings-poll')
        assert r.status_code == 200
        data = r.get_json()
        assert data['college_teams'] == []
        assert data['bull'] == []
        assert data['belle'] == []
        assert data['pro'] == []

    def test_handicap_input_empty(self, client, empty_tid):
        r = client.get(f'/api/public/tournaments/{empty_tid}/handicap-input')
        assert r.status_code == 200
        data = r.get_json()
        assert 'tournament' in data


# ---------------------------------------------------------------------------
# Competitor search with no competitors
# ---------------------------------------------------------------------------

class TestEmptyCompetitorSearch:
    """Competitor search on an empty tournament should return no matches."""

    def test_search_returns_no_match(self, client, empty_tid):
        r = client.post('/portal/competitor-access', data={
            'tournament_id': empty_tid,
            'full_name': 'Anyone',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'No competitor found' in r.data

    def test_search_empty_name(self, client, empty_tid):
        r = client.post('/portal/competitor-access', data={
            'tournament_id': empty_tid,
            'full_name': '',
        }, follow_redirects=True)
        _ok(r)


# ---------------------------------------------------------------------------
# School search with no teams
# ---------------------------------------------------------------------------

class TestEmptySchoolSearch:
    """School search on an empty tournament should return no matches."""

    def test_school_search_no_teams(self, client, empty_tid):
        r = client.post('/portal/school-access', data={
            'tournament_id': empty_tid,
            'school_name': 'Any School',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'No school found' in r.data


# ---------------------------------------------------------------------------
# Portal landing with no active tournament
# ---------------------------------------------------------------------------

class TestPortalLandingNoActive:
    """When no tournament is active, portal landing should show list."""

    def test_landing_with_setup_only(self, app):
        """If no tournament is in an active status, show the landing page."""
        from models import Tournament
        c = app.test_client()
        with app.app_context():
            # Temporarily set all to setup
            active = Tournament.query.filter(
                Tournament.status != 'setup'
            ).all()
            original_statuses = {t.id: t.status for t in active}
            for t in active:
                t.status = 'setup'
            _db.session.commit()

        r = c.get('/portal/', follow_redirects=False)
        # Should either show landing page (200) or redirect to first tournament
        _ok(r)

        # Restore
        with app.app_context():
            for tid, status in original_statuses.items():
                t = Tournament.query.get(tid)
                if t:
                    t.status = status
            _db.session.commit()
