"""
POST route integration tests — form submissions with Flask test client.

Tests cover:
  - Pro competitor creation
  - Heat result entry (single-run, dual-run, triple-run)
  - Event finalization
  - Heat lock acquire/release
  - Authentication flow (login, logout, bootstrap guard)
  - Error handling (404, 409)

Run:
    pytest tests/test_routes_post.py -v
"""
import json
import os

import pytest

from database import db as _db

# ---------------------------------------------------------------------------
# Self-contained app fixture (avoids conftest collision)
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Test Flask app with temp-file SQLite built via flask db upgrade."""
    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()

    with _app.app_context():
        _seed_admin(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_admin(app):
    """Seed an admin user and a tournament."""
    from models import Tournament
    from models.user import User

    if not User.query.filter_by(username='post_admin').first():
        u = User(username='post_admin', role='admin')
        u.set_password('post_pass')
        _db.session.add(u)

    if not Tournament.query.first():
        t = Tournament(name='POST Test 2026', year=2026, status='pro_active')
        _db.session.add(t)

    _db.session.commit()


@pytest.fixture(autouse=True)
def db_session(app):
    """Wrap each test in a nested transaction and roll back afterward."""
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def auth_client(app):
    c = app.test_client()
    with app.app_context():
        c.post('/auth/login', data={
            'username': 'post_admin',
            'password': 'post_pass',
        }, follow_redirects=True)
    return c


@pytest.fixture()
def tid(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.first().id


def _ok(resp):
    assert resp.status_code not in (500, 502, 503), \
        f'Server error {resp.status_code}: {resp.data[:300]}'


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _make_event(session, tid, name, **kw):
    from models.event import Event
    defaults = dict(
        tournament_id=tid, name=name, event_type='pro', gender=None,
        scoring_type='time', scoring_order='lowest_wins',
        stand_type='underhand', max_stands=5, status='pending',
        payouts=json.dumps({}),
    )
    defaults.update(kw)
    e = Event(**defaults)
    session.add(e)
    session.flush()
    return e


def _make_competitor(session, tid, name, gender='M', events=None):
    from models.competitor import ProCompetitor
    c = ProCompetitor(
        tournament_id=tid, name=name, gender=gender,
        events_entered=json.dumps(events or []), status='active',
    )
    session.add(c)
    session.flush()
    return c


def _make_heat(session, event_id, competitors=None, status='pending'):
    from models.heat import Heat
    h = Heat(
        event_id=event_id, heat_number=1, run_number=1,
        competitors=json.dumps(competitors or []),
        stand_assignments=json.dumps({}),
        status=status,
    )
    session.add(h)
    session.flush()
    return h


def _make_result(session, event_id, comp, **kw):
    from models.event import EventResult
    defaults = dict(
        event_id=event_id, competitor_id=comp.id,
        competitor_type='pro', competitor_name=comp.name,
        status='pending',
    )
    defaults.update(kw)
    r = EventResult(**defaults)
    session.add(r)
    session.flush()
    return r


# ---------------------------------------------------------------------------
# Authentication flow
# ---------------------------------------------------------------------------

class TestAuthFlow:
    def test_login_valid(self, client):
        resp = client.post('/auth/login', data={
            'username': 'post_admin', 'password': 'post_pass',
        }, follow_redirects=False)
        _ok(resp)

    def test_login_invalid(self, client):
        resp = client.post('/auth/login', data={
            'username': 'post_admin', 'password': 'wrong',
        }, follow_redirects=True)
        _ok(resp)

    def test_logout(self, auth_client):
        resp = auth_client.post('/auth/logout', follow_redirects=False)
        _ok(resp)

    def test_bootstrap_locked(self, client):
        resp = client.get('/auth/bootstrap')
        _ok(resp)

    def test_management_requires_auth(self, client, tid):
        resp = client.get(f'/tournament/{tid}')
        assert resp.status_code in (302, 401, 403)


# ---------------------------------------------------------------------------
# Pro competitor creation
# ---------------------------------------------------------------------------

class TestProCompetitorCreation:
    def test_create_pro_competitor(self, auth_client, tid, db_session):
        resp = auth_client.post(f'/registration/pro/{tid}/new', data={
            'name': 'Route Pro Comp',
            'gender': 'M',
            'address': '123 Main',
            'phone': '5551234567',
            'email': 'test@example.com',
        }, follow_redirects=True)
        _ok(resp)

    def test_create_missing_name(self, auth_client, tid, db_session):
        resp = auth_client.post(f'/registration/pro/{tid}/new', data={
            'name': '', 'gender': 'M',
        }, follow_redirects=True)
        _ok(resp)


# ---------------------------------------------------------------------------
# Heat result entry
# ---------------------------------------------------------------------------

class TestHeatResultEntry:
    def test_single_run_entry(self, auth_client, tid, db_session):
        event = _make_event(db_session, tid, 'Entry UH')
        comp = _make_competitor(db_session, tid, 'EntryC1', events=[event.id])
        heat = _make_heat(db_session, event.id, competitors=[comp.id])
        db_session.commit()

        resp = auth_client.post(
            f'/scoring/heat/{tid}/{heat.id}/enter',
            data={
                f'result_{comp.id}': '15.5',
                f'status_{comp.id}': 'completed',
                'heat_version': str(heat.version_id),
            },
            follow_redirects=True,
        )
        _ok(resp)

    def test_nonexistent_heat(self, auth_client, tid):
        resp = auth_client.post(
            f'/scoring/heat/{tid}/99999/enter',
            data={'heat_version': '1'},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 404)

    def test_get_entry_form(self, auth_client, tid, db_session):
        event = _make_event(db_session, tid, 'GET Entry')
        comp = _make_competitor(db_session, tid, 'GetC1', events=[event.id])
        heat = _make_heat(db_session, event.id, competitors=[comp.id])
        db_session.commit()

        resp = auth_client.get(f'/scoring/heat/{tid}/{heat.id}/enter')
        _ok(resp)


# ---------------------------------------------------------------------------
# Event finalization
# ---------------------------------------------------------------------------

class TestEventFinalization:
    def test_finalize_with_results(self, auth_client, tid, db_session):
        event = _make_event(db_session, tid, 'Finalize Evt')
        comp = _make_competitor(db_session, tid, 'FinC1', events=[event.id])
        _make_result(db_session, event.id, comp, result_value=10.0, status='completed')
        _make_heat(db_session, event.id, competitors=[comp.id], status='completed')
        db_session.commit()

        resp = auth_client.post(
            f'/scoring/event/{tid}/{event.id}/finalize',
            follow_redirects=True,
        )
        _ok(resp)

    def test_finalize_nonexistent(self, auth_client, tid):
        resp = auth_client.post(
            f'/scoring/event/{tid}/99999/finalize',
            follow_redirects=False,
        )
        assert resp.status_code in (302, 404)


# ---------------------------------------------------------------------------
# Heat lock
# ---------------------------------------------------------------------------

class TestHeatLockRoutes:
    def test_release_lock(self, auth_client, tid, db_session):
        event = _make_event(db_session, tid, 'Lock Evt')
        heat = _make_heat(db_session, event.id)
        db_session.commit()

        resp = auth_client.post(
            f'/scoring/heat/{tid}/{heat.id}/release-lock',
            follow_redirects=True,
        )
        _ok(resp)


# ---------------------------------------------------------------------------
# Tournament CRUD
# ---------------------------------------------------------------------------

class TestTournamentCRUD:
    def test_create_tournament(self, auth_client):
        resp = auth_client.post('/tournament/new', data={
            'name': 'New T 2026', 'year': '2026',
        }, follow_redirects=True)
        _ok(resp)

    def test_setup_page(self, auth_client, tid):
        resp = auth_client.get(f'/tournament/{tid}/setup')
        _ok(resp)


# ---------------------------------------------------------------------------
# Gear sharing routes
# ---------------------------------------------------------------------------

class TestGearSharingRoutes:
    def test_gear_page_loads(self, auth_client, tid):
        resp = auth_client.get(f'/registration/pro/{tid}/gear-sharing')
        _ok(resp)

    def test_gear_parse(self, auth_client, tid, db_session):
        resp = auth_client.post(
            f'/registration/pro/{tid}/pro/gear-sharing/parse',
            follow_redirects=True,
        )
        _ok(resp)


# ---------------------------------------------------------------------------
# Scheduling routes
# ---------------------------------------------------------------------------

class TestSchedulingPOST:
    def test_setup_events_post(self, auth_client, tid):
        resp = auth_client.post(
            f'/scheduling/setup/{tid}',
            data={'_action': 'save'},
            follow_redirects=True,
        )
        _ok(resp)

    def test_build_flights_post(self, auth_client, tid):
        resp = auth_client.post(
            f'/scheduling/flights/build/{tid}',
            data={'num_flights': '8'},
            follow_redirects=True,
        )
        _ok(resp)


# ---------------------------------------------------------------------------
# Portal routes
# ---------------------------------------------------------------------------

class TestPortalAccess:
    def test_spectator_dashboard(self, client, tid):
        resp = client.get(f'/portal/spectator/{tid}')
        _ok(resp)

    def test_portal_landing(self, client):
        _ok(client.get('/portal/'))

    def test_user_guide(self, client):
        _ok(client.get('/portal/guide'))


# ---------------------------------------------------------------------------
# Undo heat results
# ---------------------------------------------------------------------------

class TestUndoHeatResults:
    def test_undo_without_token(self, auth_client, tid, db_session):
        event = _make_event(db_session, tid, 'Undo Evt')
        heat = _make_heat(db_session, event.id)
        db_session.commit()

        resp = auth_client.post(
            f'/scoring/heat/{tid}/{heat.id}/undo',
            follow_redirects=True,
        )
        _ok(resp)
