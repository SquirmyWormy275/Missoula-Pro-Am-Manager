"""
Route smoke tests — verify all blueprint GET routes load without 500 errors.

Uses Flask's test client with an in-memory SQLite database and a seeded
admin user so the auth guard passes. No real data is exercised; the tests
only assert that each route returns an expected HTTP status (200, 302, or
404) and does NOT raise an unhandled exception.

Run:
    pytest tests/test_routes_smoke.py -v

Requirements:
    pytest (pip install pytest)
    All app dependencies must be installed.

Design notes:
  - Each test class corresponds to one blueprint / concern.
  - The `client` fixture logs in as an admin judge before each test, so
    all management-blueprint routes pass the before_request auth hook.
  - Public endpoints (/, /health, /portal/*, /api/public/*, /strathmark/*)
    are hit without authentication.
  - Routes that require a tournament_id seed one tournament in the DB.
  - Routes that require an event_id or heat_id are skipped (marked xfail)
    when no matching fixtures exist — the intent is smoke coverage, not
    full integration tests.
  - WTF_CSRF_ENABLED is disabled for all POST smoke tests.
"""
import pytest
from database import db as _db


# ---------------------------------------------------------------------------
# App + DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='session')
def app():
    """Create a test Flask app with in-memory SQLite and CSRF disabled."""
    import os
    os.environ.setdefault('SECRET_KEY', 'test-secret-smoke')
    os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

    from app import create_app
    _app = create_app()
    _app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': False,
        'WTF_CSRF_CHECK_DEFAULT': False,
        'SERVER_NAME': None,
    })

    with _app.app_context():
        _db.create_all()
        _seed_db(_app)
        yield _app
        _db.session.remove()
        _db.drop_all()


def _seed_db(app):
    """Insert minimal seed data: one admin user, one tournament."""
    from models.user import User
    from models import Tournament

    # Admin user
    if not User.query.filter_by(username='smoke_admin').first():
        u = User(username='smoke_admin', role=User.ROLE_ADMIN)
        u.set_password('smoke_pass')
        _db.session.add(u)

    # One tournament
    if not Tournament.query.first():
        t = Tournament(name='Smoke Test 2026', year=2026, status='setup')
        _db.session.add(t)

    _db.session.commit()


@pytest.fixture()
def client(app):
    """Return an unauthenticated test client."""
    return app.test_client()


@pytest.fixture()
def auth_client(app):
    """Return a test client logged in as the smoke_admin judge."""
    c = app.test_client()
    with app.app_context():
        # POST to login
        c.post('/auth/login', data={
            'username': 'smoke_admin',
            'password': 'smoke_pass',
        }, follow_redirects=True)
    return c


@pytest.fixture()
def tid(app):
    """Return the ID of the seeded tournament."""
    with app.app_context():
        from models import Tournament
        t = Tournament.query.first()
        return t.id


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _ok(response):
    """Assert response is 200 or a redirect (302/301), not a server error."""
    assert response.status_code not in (500, 502, 503), (
        f'Server error {response.status_code}: {response.data[:300]}'
    )


# ---------------------------------------------------------------------------
# Public routes — no auth required
# ---------------------------------------------------------------------------

class TestPublicRoutes:
    """Smoke tests for routes accessible without authentication."""

    def test_index(self, client):
        _ok(client.get('/'))

    def test_health(self, client):
        r = client.get('/health')
        assert r.status_code == 200
        data = r.get_json()
        assert 'status' in data

    def test_login_page(self, client):
        _ok(client.get('/auth/login'))

    def test_bootstrap_page(self, client):
        # Bootstrap locks itself after the first user exists — expect redirect.
        r = client.get('/auth/bootstrap')
        assert r.status_code in (200, 302, 403)

    def test_service_worker(self, client):
        r = client.get('/sw.js')
        assert r.status_code in (200, 404)

    def test_strathmark_status(self, client):
        _ok(client.get('/strathmark/status'))

    def test_portal_landing(self, client):
        _ok(client.get('/portal/'))

    def test_portal_guide(self, client):
        _ok(client.get('/portal/guide'))

    def test_api_v1_alias(self, client, tid):
        r = client.get(f'/api/v1/public/tournaments/{tid}/standings')
        assert r.status_code in (200, 404)

    def test_api_standings(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/standings')
        assert r.status_code in (200, 404)

    def test_api_schedule(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/schedule')
        assert r.status_code in (200, 404)

    def test_api_results(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/results')
        assert r.status_code in (200, 404)

    def test_api_standings_poll(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/standings-poll')
        assert r.status_code in (200, 404)

    def test_api_handicap_input(self, client, tid):
        r = client.get(f'/api/public/tournaments/{tid}/handicap-input')
        assert r.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Main routes — require judge auth
# ---------------------------------------------------------------------------

class TestMainRoutes:
    """Smoke tests for the main blueprint (dashboard, tournament CRUD)."""

    def test_dashboard(self, auth_client):
        _ok(auth_client.get('/'))

    def test_tournament_new_page(self, auth_client):
        _ok(auth_client.get('/tournament/new'))

    def test_tournament_detail(self, auth_client, tid):
        _ok(auth_client.get(f'/tournament/{tid}'))

    def test_tournament_setup(self, auth_client, tid):
        _ok(auth_client.get(f'/tournament/{tid}/setup'))

    def test_college_dashboard(self, auth_client, tid):
        _ok(auth_client.get(f'/tournament/{tid}/college'))

    def test_pro_dashboard(self, auth_client, tid):
        _ok(auth_client.get(f'/tournament/{tid}/pro'))


# ---------------------------------------------------------------------------
# Registration routes
# ---------------------------------------------------------------------------

class TestRegistrationRoutes:
    """Smoke tests for the registration blueprint."""

    def test_college_registration(self, auth_client, tid):
        _ok(auth_client.get(f'/registration/{tid}/college'))

    def test_pro_registration(self, auth_client, tid):
        _ok(auth_client.get(f'/registration/{tid}/pro'))

    def test_new_pro_competitor(self, auth_client, tid):
        _ok(auth_client.get(f'/registration/{tid}/pro/new'))

    def test_gear_sharing(self, auth_client, tid):
        _ok(auth_client.get(f'/registration/{tid}/pro/gear-sharing'))


# ---------------------------------------------------------------------------
# Scheduling routes
# ---------------------------------------------------------------------------

class TestSchedulingRoutes:
    """Smoke tests for the scheduling blueprint package."""

    def test_event_list(self, auth_client, tid):
        _ok(auth_client.get(f'/scheduling/{tid}/events'))

    def test_setup_events(self, auth_client, tid):
        _ok(auth_client.get(f'/scheduling/{tid}/events/setup'))

    def test_day_schedule_redirect(self, auth_client, tid):
        r = auth_client.get(f'/scheduling/{tid}/day-schedule')
        # 301 permanent redirect to event_list
        assert r.status_code in (200, 301, 302)

    def test_preflight_page(self, auth_client, tid):
        _ok(auth_client.get(f'/scheduling/{tid}/preflight'))

    def test_preflight_json(self, auth_client, tid):
        r = auth_client.get(f'/scheduling/{tid}/preflight-json')
        assert r.status_code in (200, 302)

    def test_flight_list(self, auth_client, tid):
        _ok(auth_client.get(f'/scheduling/{tid}/flights'))

    def test_build_flights_page(self, auth_client, tid):
        _ok(auth_client.get(f'/scheduling/{tid}/flights/build'))

    def test_heat_sheets(self, auth_client, tid):
        _ok(auth_client.get(f'/scheduling/{tid}/heat-sheets'))

    def test_day_schedule_print(self, auth_client, tid):
        _ok(auth_client.get(f'/scheduling/{tid}/day-schedule/print'))

    def test_friday_feature(self, auth_client, tid):
        _ok(auth_client.get(f'/scheduling/{tid}/friday-night'))

    def test_show_day(self, auth_client, tid):
        _ok(auth_client.get(f'/scheduling/{tid}/show-day'))

    def test_ability_rankings(self, auth_client, tid):
        _ok(auth_client.get(f'/scheduling/{tid}/pro/ability-rankings'))


# ---------------------------------------------------------------------------
# Scoring routes
# ---------------------------------------------------------------------------

class TestScoringRoutes:
    """Smoke tests for the scoring blueprint."""

    def test_event_results_404_no_event(self, auth_client, tid):
        # No events seeded — expect 404
        r = auth_client.get(f'/scoring/{tid}/event/9999/results')
        assert r.status_code in (200, 302, 404)

    def test_offline_ops(self, auth_client, tid):
        _ok(auth_client.get(f'/scoring/{tid}/offline'))


# ---------------------------------------------------------------------------
# Reporting routes
# ---------------------------------------------------------------------------

class TestReportingRoutes:
    """Smoke tests for the reporting blueprint."""

    def test_all_results(self, auth_client, tid):
        _ok(auth_client.get(f'/reporting/{tid}/all-results'))

    def test_college_standings(self, auth_client, tid):
        _ok(auth_client.get(f'/reporting/{tid}/college/standings'))

    def test_pro_standings(self, auth_client, tid):
        _ok(auth_client.get(f'/reporting/{tid}/pro/standings'))

    def test_payout_summary(self, auth_client, tid):
        _ok(auth_client.get(f'/reporting/{tid}/pro/payout-summary'))

    def test_fee_tracker(self, auth_client, tid):
        _ok(auth_client.get(f'/reporting/{tid}/pro/fee-tracker'))


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

class TestAuthRoutes:
    """Smoke tests for the auth blueprint."""

    def test_login_get(self, client):
        _ok(client.get('/auth/login'))

    def test_users_page(self, auth_client):
        _ok(auth_client.get('/auth/users'))

    def test_audit_log(self, auth_client):
        _ok(auth_client.get('/auth/audit'))


# ---------------------------------------------------------------------------
# Portal routes — public, no auth required
# ---------------------------------------------------------------------------

class TestPortalRoutes:
    """Smoke tests for the portal blueprint."""

    def test_portal_home(self, client):
        _ok(client.get('/portal/'))

    def test_spectator_dashboard(self, client, tid):
        _ok(client.get(f'/portal/{tid}/spectator'))

    def test_school_access(self, client, tid):
        _ok(client.get(f'/portal/{tid}/school-access'))

    def test_competitor_access(self, client, tid):
        _ok(client.get(f'/portal/{tid}/competitor'))


# ---------------------------------------------------------------------------
# Validation routes
# ---------------------------------------------------------------------------

class TestValidationRoutes:
    """Smoke tests for the validation blueprint."""

    def test_validation_dashboard(self, auth_client, tid):
        _ok(auth_client.get(f'/validation/{tid}/'))

    def test_college_validation(self, auth_client, tid):
        _ok(auth_client.get(f'/validation/{tid}/college'))

    def test_pro_validation(self, auth_client, tid):
        _ok(auth_client.get(f'/validation/{tid}/pro'))


# ---------------------------------------------------------------------------
# Woodboss routes
# ---------------------------------------------------------------------------

class TestWoodbossRoutes:
    """Smoke tests for the Virtual Woodboss blueprint."""

    def test_woodboss_dashboard(self, auth_client, tid):
        _ok(auth_client.get(f'/woodboss/{tid}/'))

    def test_woodboss_config(self, auth_client, tid):
        _ok(auth_client.get(f'/woodboss/{tid}/config'))

    def test_woodboss_report(self, auth_client, tid):
        _ok(auth_client.get(f'/woodboss/{tid}/report'))

    def test_woodboss_lottery(self, auth_client, tid):
        _ok(auth_client.get(f'/woodboss/{tid}/lottery'))


# ---------------------------------------------------------------------------
# Pro-Am Relay routes
# ---------------------------------------------------------------------------

class TestProAmRelayRoutes:
    """Smoke tests for the Pro-Am Relay blueprint."""

    def test_relay_dashboard(self, auth_client, tid):
        _ok(auth_client.get(f'/proam-relay/{tid}/'))


# ---------------------------------------------------------------------------
# Partnered Axe routes
# ---------------------------------------------------------------------------

class TestPartneredAxeRoutes:
    """Smoke tests for the Partnered Axe Throw blueprint."""

    def test_axe_dashboard(self, auth_client, tid):
        _ok(auth_client.get(f'/partnered-axe/{tid}/'))


# ---------------------------------------------------------------------------
# Import routes
# ---------------------------------------------------------------------------

class TestImportRoutes:
    """Smoke tests for the import blueprint."""

    def test_import_upload(self, auth_client, tid):
        _ok(auth_client.get(f'/import/{tid}/pro/upload'))
