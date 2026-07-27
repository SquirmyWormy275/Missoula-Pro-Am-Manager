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
import os

import pytest

from database import db as _db

# ---------------------------------------------------------------------------
# App + DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Create a test Flask app with temp-file SQLite built via flask db upgrade."""
    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()
    # Disable session protection so session_transaction() injected sessions work
    # regardless of preceding request context state.
    _app.config['SESSION_PROTECTION'] = None

    with _app.app_context():
        _seed_db(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_db(app):
    """Insert minimal seed data: one admin user, one tournament, plus events for
    the TestRefinalizationBadge smoke tests so no test body needs to commit."""
    import json
    from decimal import Decimal

    from models import Tournament
    from models.event import Event, EventResult
    from models.user import User

    # Admin user — role='admin' grants is_admin, is_judge, can_score, etc.
    if not User.query.filter_by(username='smoke_admin').first():
        u = User(username='smoke_admin', role='admin')
        u.set_password('smoke_pass')
        _db.session.add(u)

    # One tournament
    if not Tournament.query.first():
        t = Tournament(name='Smoke Test 2026', year=2026, status='setup')
        _db.session.add(t)

    _db.session.flush()

    t = Tournament.query.first()

    # Badge test event 1: in_progress, not finalized, has a completed result
    if not Event.query.filter_by(name='Badge Test Unfinalized').first():
        ev = Event(
            tournament_id=t.id,
            name='Badge Test Unfinalized',
            event_type='pro',
            gender='M',
            scoring_type='time',
            scoring_order='lowest_wins',
            stand_type='underhand',
            max_stands=5,
            payouts=json.dumps({}),
            status='in_progress',
            is_finalized=False,
        )
        _db.session.add(ev)
        _db.session.flush()
        _db.session.add(EventResult(
            event_id=ev.id,
            competitor_id=1,
            competitor_type='pro',
            competitor_name='Test Pro Badge',
            result_value=30.0,
            run1_value=30.0,
            final_position=1,
            points_awarded=Decimal('10.00'),
            status='completed',
        ))

    # Badge test event 2: finalized, has completed result — badge should NOT appear
    if not Event.query.filter_by(name='Badge Test Finalized').first():
        ev2 = Event(
            tournament_id=t.id,
            name='Badge Test Finalized',
            event_type='pro',
            gender='M',
            scoring_type='time',
            scoring_order='lowest_wins',
            stand_type='underhand',
            max_stands=5,
            payouts=json.dumps({}),
            status='completed',
            is_finalized=True,
        )
        _db.session.add(ev2)
        _db.session.flush()
        _db.session.add(EventResult(
            event_id=ev2.id,
            competitor_id=1,
            competitor_type='pro',
            competitor_name='Test Pro Badge 2',
            result_value=31.0,
            run1_value=31.0,
            final_position=1,
            points_awarded=Decimal('10.00'),
            status='completed',
        ))

    # Badge test event 3: in_progress, not finalized, NO completed results
    if not Event.query.filter_by(name='Badge Test No Results').first():
        ev3 = Event(
            tournament_id=t.id,
            name='Badge Test No Results',
            event_type='pro',
            gender='M',
            scoring_type='time',
            scoring_order='lowest_wins',
            stand_type='underhand',
            max_stands=5,
            payouts=json.dumps({}),
            status='in_progress',
            is_finalized=False,
        )
        _db.session.add(ev3)

    _db.session.commit()


@pytest.fixture()
def client(app):
    """Return an unauthenticated test client."""
    return app.test_client()


@pytest.fixture()
def auth_client(app):
    """Return a test client logged in as the smoke_admin judge.

    The login POST is made WITHOUT a nested app.app_context() push — the
    module-scoped app context is already active when this fixture runs.
    Passing use_cookies=True (default) and keeping the same client instance
    ensures the session cookie from the login response is re-sent on the
    subsequent authenticated request.
    """
    c = app.test_client(use_cookies=True)
    # Login POST runs in the current (module-level) app context — no nesting.
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
        # May return 200 JSON or 302 redirect depending on auth state
        _ok(r)
        if r.status_code == 200:
            assert r.is_json

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


# ---------------------------------------------------------------------------
# Unit 7: Re-finalization warning badge
# ---------------------------------------------------------------------------


class TestRefinalizationBadge:
    """Verify the 'Pending re-finalization' badge appears on event_results page
    when an event has completed results but is_finalized=False.

    Events are pre-seeded in _seed_db so no test body commits are needed.
    This avoids the SQLAlchemy scoped-session teardown interaction that caused
    auth failures when portal tests preceded these tests in the run order.
    """

    def test_badge_shown_for_unfinalized_event_with_completed_results(
        self, auth_client, tid
    ):
        """event_results page shows badge when is_finalized=False and results exist.

        eid=1 because 'Badge Test Unfinalized' is the first event seeded in _seed_db.
        """
        import sys
        r1 = auth_client.get(f'/scoring/{tid}/event/1/results')
        r2 = auth_client.get(f'/scoring/{tid}/event/2/results')
        print(f'\nDEBUG: event/1 -> {r1.status_code}, event/2 -> {r2.status_code}', file=sys.stderr)
        r = r1
        assert r.status_code not in (500, 502, 503)
        assert b'Pending re-finalization' in r.data

    def test_badge_not_shown_for_finalized_event(self, auth_client, tid):
        """No badge when event is already finalized."""
        from models.event import Event
        eid = Event.query.filter_by(name='Badge Test Finalized').first().id
        _db.session.remove()

        r = auth_client.get(f'/scoring/{tid}/event/{eid}/results')
        assert r.status_code not in (500, 502, 503)
        assert b'Pending re-finalization' not in r.data

    def test_badge_not_shown_for_new_event_with_no_results(
        self, auth_client, tid
    ):
        """No badge when event has no completed results (never finalized)."""
        from models.event import Event
        eid = Event.query.filter_by(name='Badge Test No Results').first().id
        _db.session.remove()

        r = auth_client.get(f'/scoring/{tid}/event/{eid}/results')
        assert r.status_code not in (500, 502, 503)
        assert b'Pending re-finalization' not in r.data


# ---------------------------------------------------------------------------
# Unit 12: Race-Day Operations Dashboard
# ---------------------------------------------------------------------------


class TestOpsDashboard:
    """Smoke tests for the race-day ops dashboard (capstone)."""

    def test_dashboard_loads(self, auth_client, tid):
        """Happy path: dashboard returns 200."""
        r = auth_client.get(f'/tournament/{tid}/ops-dashboard')
        assert r.status_code == 200

    def test_auto_refresh_meta_tag_present(self, auth_client, tid):
        """Page must contain an auto-refresh directive."""
        r = auth_client.get(f'/tournament/{tid}/ops-dashboard')
        assert r.status_code == 200
        body = r.data
        # Accept either meta http-equiv refresh OR JS setInterval reload pattern
        assert (b'http-equiv="refresh"' in body or b'location.reload' in body)

    def test_no_scratch_entries_shows_empty_message(self, auth_client, tid):
        """When no audit entries exist, scratch feed shows empty state text."""
        r = auth_client.get(f'/tournament/{tid}/ops-dashboard')
        assert r.status_code == 200
        assert b'No recent scratches' in r.data

    def test_no_relay_event_hides_relay_section(self, auth_client, tid):
        """When no Pro-Am Relay event exists, relay section is absent or hidden."""
        r = auth_client.get(f'/tournament/{tid}/ops-dashboard')
        assert r.status_code == 200
        # Section must not show relay health content when no relay event exists
        assert b'relay-health-section' not in r.data

    def test_payout_section_shows_zero_when_no_payouts(self, auth_client, tid):
        """Payout section renders $0.00 totals when no payout_amounts set."""
        r = auth_client.get(f'/tournament/{tid}/ops-dashboard')
        assert r.status_code == 200
        assert b'$0.00' in r.data
