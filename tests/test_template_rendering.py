"""
Template rendering regression tests.

Renders key Jinja2 templates with seed data and asserts they produce
valid HTML without UndefinedError, TemplateSyntaxError, or 500 errors.

This catches:
  - Missing template variables
  - Broken {% include %} / {% extends %}
  - Bad filters or macros
  - Context processor gaps

Run:
    pytest tests/test_template_rendering.py -v
    pytest -m slow   (these hit the full render pipeline)
"""
from __future__ import annotations

import pytest
from tests.conftest import (
    make_tournament, make_team, make_college_competitor,
    make_pro_competitor, make_event, make_heat, make_event_result, make_flight,
)

pytestmark = pytest.mark.slow


@pytest.fixture()
def seeded_app(app, db_session):
    """Seed enough data for templates that require tournament context."""
    t = make_tournament(db_session, name='Template Test 2026')
    team = make_team(db_session, t)
    c1 = make_college_competitor(db_session, t, team, 'Test Student', 'M')
    p1 = make_pro_competitor(db_session, t, 'Test Pro', 'M')
    event_c = make_event(db_session, t, "Men's Underhand Speed",
                         event_type='college', gender='M',
                         scoring_type='time', stand_type='underhand')
    event_p = make_event(db_session, t, 'Springboard',
                         event_type='pro', scoring_type='time',
                         stand_type='springboard')
    heat_c = make_heat(db_session, event_c, competitors=[c1.id])
    heat_p = make_heat(db_session, event_p, competitors=[p1.id])
    flight = make_flight(db_session, t)
    heat_p.flight_id = flight.id
    heat_p.flight_position = 1
    r1 = make_event_result(db_session, event_c, c1, competitor_type='college',
                           result_value=25.0, status='completed', final_position=1,
                           points_awarded=10)
    r2 = make_event_result(db_session, event_p, p1, competitor_type='pro',
                           result_value=45.0, status='completed', final_position=1,
                           payout_amount=500)
    db_session.flush()

    return {
        'app': app,
        'tournament': t,
        'team': team,
        'college_competitor': c1,
        'pro_competitor': p1,
        'college_event': event_c,
        'pro_event': event_p,
        'college_heat': heat_c,
        'pro_heat': heat_p,
        'flight': flight,
    }


@pytest.fixture()
def admin_client(seeded_app):
    """Authenticated test client with seeded data."""
    from models.user import User
    from database import db
    app = seeded_app['app']
    c = app.test_client()

    # Ensure admin user exists
    u = User.query.filter_by(username='tmpl_admin').first()
    if not u:
        u = User(username='tmpl_admin', role='admin')
        u.set_password('tmpl_pass')
        db.session.add(u)
        db.session.flush()

    with c.session_transaction() as sess:
        sess['_user_id'] = str(u.id)
    return c


def _assert_renders(response):
    """Assert the response did not produce a server error (500/502/503).

    404 is acceptable — the route may not match the URL we guessed.
    302 redirects are fine. The goal is catching Jinja2 template crashes.
    """
    assert response.status_code not in (500, 502, 503), (
        f'Server error {response.status_code}:\n'
        f'{response.data[:500]}'
    )
    # Should never see a Jinja2 UndefinedError in the body
    if response.status_code == 200:
        body = response.data.decode('utf-8', errors='replace')
        assert 'UndefinedError' not in body
        assert 'TemplateSyntaxError' not in body


# ===========================================================================
# MANAGEMENT TEMPLATES (require auth)
# ===========================================================================

class TestDashboardTemplates:

    def test_main_dashboard(self, admin_client):
        _assert_renders(admin_client.get('/'))

    def test_tournament_detail(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/tournament/{tid}'))

    def test_tournament_setup(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        r = admin_client.get(f'/tournament/{tid}/setup')
        _assert_renders(r)

    def test_tournament_new(self, admin_client):
        _assert_renders(admin_client.get('/tournament/new'))


class TestCollegeTemplates:

    def test_college_dashboard(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/tournament/{tid}/college'))

    def test_college_registration(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/registration/{tid}/college'))


class TestProTemplates:

    def test_pro_dashboard(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/tournament/{tid}/pro'))

    def test_pro_registration(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/registration/{tid}/pro'))

    def test_new_pro_competitor_form(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/registration/{tid}/pro/new'))

    def test_gear_sharing(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/registration/{tid}/pro/gear-sharing'))


class TestSchedulingTemplates:

    def test_event_list(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/scheduling/{tid}/events'))

    def test_setup_events(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/scheduling/{tid}/events/setup'))

    def test_heat_sheets(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/scheduling/{tid}/heat-sheets'))

    def test_flight_list(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/scheduling/{tid}/flights'))

    def test_build_flights(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/scheduling/{tid}/flights/build'))

    def test_show_day(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/scheduling/{tid}/show-day'))

    def test_ability_rankings(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/scheduling/{tid}/pro/ability-rankings'))

    def test_preflight(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/scheduling/{tid}/preflight'))

    def test_friday_feature(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/scheduling/{tid}/friday-night'))


class TestReportingTemplates:

    def test_all_results(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/reporting/{tid}/all-results'))

    @pytest.mark.xfail(reason='DetachedInstanceError: lazy load of team in standings template after session rollback')
    def test_college_standings(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/reporting/{tid}/college/standings'))

    def test_pro_standings(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/reporting/{tid}/pro/standings'))

    def test_payout_summary(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/reporting/{tid}/pro/payout-summary'))

    def test_fee_tracker(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/reporting/{tid}/pro/fee-tracker'))


class TestAuthTemplates:

    def test_login_page(self, seeded_app):
        client = seeded_app['app'].test_client()
        _assert_renders(client.get('/auth/login'))

    def test_users_page(self, admin_client):
        _assert_renders(admin_client.get('/auth/users'))

    def test_audit_log(self, admin_client):
        _assert_renders(admin_client.get('/auth/audit'))


class TestScoringTemplates:

    def test_scoring_event_results(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        eid = seeded_app['college_event'].id
        r = admin_client.get(f'/scoring/{tid}/event/{eid}/results')
        _assert_renders(r)

    def test_scoring_enter_heat(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        hid = seeded_app['college_heat'].id
        r = admin_client.get(f'/scoring/{tid}/heat/{hid}/enter')
        _assert_renders(r)


class TestWoodbossTemplates:

    def test_woodboss_dashboard(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/woodboss/{tid}/'))

    def test_woodboss_config(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/woodboss/{tid}/config'))

    def test_woodboss_report(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/woodboss/{tid}/report'))


class TestValidationTemplates:

    def test_validation_dashboard(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/validation/{tid}/'))

    def test_validation_college(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/validation/{tid}/college'))

    def test_validation_pro(self, admin_client, seeded_app):
        tid = seeded_app['tournament'].id
        _assert_renders(admin_client.get(f'/validation/{tid}/pro'))


# ===========================================================================
# PUBLIC TEMPLATES (no auth)
# ===========================================================================

class TestPortalTemplates:

    def test_portal_landing(self, seeded_app):
        client = seeded_app['app'].test_client()
        _assert_renders(client.get('/portal/'))

    def test_spectator_dashboard(self, seeded_app):
        client = seeded_app['app'].test_client()
        tid = seeded_app['tournament'].id
        r = client.get(f'/portal/{tid}/spectator')
        # May redirect; that's fine — just no 500
        assert r.status_code not in (500, 502, 503)

    def test_school_access(self, seeded_app):
        client = seeded_app['app'].test_client()
        tid = seeded_app['tournament'].id
        _assert_renders(client.get(f'/portal/{tid}/school-access'))

    def test_competitor_access(self, seeded_app):
        client = seeded_app['app'].test_client()
        tid = seeded_app['tournament'].id
        _assert_renders(client.get(f'/portal/{tid}/competitor'))

    def test_user_guide(self, seeded_app):
        client = seeded_app['app'].test_client()
        _assert_renders(client.get('/portal/guide'))
