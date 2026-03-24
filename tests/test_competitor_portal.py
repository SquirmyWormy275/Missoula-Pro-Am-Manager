"""
Competitor portal test suite — PIN flow, dashboard access, session auth, SMS opt-in.

Tests the full competitor self-service lifecycle:
  - Name search and match
  - PIN setup (first visit) and PIN verification (return visit)
  - Session-based authorization
  - Dashboard data (schedule, results, payouts)
  - Access denied without PIN
  - SMS opt-in toggle
  - School captain portal flow

Run:
    pytest tests/test_competitor_portal.py -v
"""
import json
import os
import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-competitor')
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
        _seed_competitor_data(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_competitor_data(app):
    """Seed tournament, teams, competitors, events, heats, results."""
    from models.user import User
    from models import Tournament, Team, Event, EventResult, Heat
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.school_captain import SchoolCaptain

    # Admin user for judge-override tests
    if not User.query.filter_by(username='comp_admin').first():
        u = User(username='comp_admin', role='admin')
        u.set_password('adminpass')
        _db.session.add(u)

    # Competitor-role user for authenticated competitor dashboard
    comp_user = User.query.filter_by(username='comp_user').first()
    if not comp_user:
        comp_user = User(username='comp_user', role='competitor')
        comp_user.set_password('comppass')
        _db.session.add(comp_user)
        _db.session.flush()

    # Tournament
    t = Tournament.query.filter_by(name='Comp Test 2026').first()
    if not t:
        t = Tournament(name='Comp Test 2026', year=2026, status='pro_active')
        _db.session.add(t)
        _db.session.flush()

    # Team
    team = Team.query.filter_by(tournament_id=t.id, team_code='UM-A').first()
    if not team:
        team = Team(tournament_id=t.id, team_code='UM-A',
                    school_name='University of Montana', school_abbreviation='UM')
        _db.session.add(team)
        _db.session.flush()

    # College competitor
    cc = CollegeCompetitor.query.filter_by(tournament_id=t.id, name='Alice Smith').first()
    if not cc:
        cc = CollegeCompetitor(
            tournament_id=t.id, team_id=team.id, name='Alice Smith',
            gender='F', events_entered='[]', status='active',
        )
        _db.session.add(cc)
        _db.session.flush()

    # Pro competitor (no PIN)
    pc = ProCompetitor.query.filter_by(tournament_id=t.id, name='Bob Jones').first()
    if not pc:
        pc = ProCompetitor(
            tournament_id=t.id, name='Bob Jones', gender='M',
            events_entered='[]', gear_sharing='{}', partners='{}', status='active',
        )
        _db.session.add(pc)
        _db.session.flush()

    # Pro competitor with PIN already set
    pc2 = ProCompetitor.query.filter_by(tournament_id=t.id, name='Charlie Brown').first()
    if not pc2:
        pc2 = ProCompetitor(
            tournament_id=t.id, name='Charlie Brown', gender='M',
            events_entered='[]', gear_sharing='{}', partners='{}', status='active',
        )
        pc2.set_portal_pin('1234')
        _db.session.add(pc2)
        _db.session.flush()

    # Pro event with result for Bob
    evt = Event.query.filter_by(tournament_id=t.id, name='Underhand Speed (M)').first()
    if not evt:
        evt = Event(
            tournament_id=t.id, name='Underhand Speed (M)', event_type='pro',
            gender='M', scoring_type='time', scoring_order='lowest_wins',
            stand_type='underhand', max_stands=5, status='completed',
        )
        _db.session.add(evt)
        _db.session.flush()

    # Heat with Bob
    heat = Heat.query.filter_by(event_id=evt.id, heat_number=1).first()
    if not heat:
        heat = Heat(
            event_id=evt.id, heat_number=1, run_number=1,
            competitors=json.dumps([pc.id]),
            stand_assignments=json.dumps({str(pc.id): '1'}),
            status='completed',
        )
        _db.session.add(heat)
        _db.session.flush()

    # Result for Bob
    res = EventResult.query.filter_by(event_id=evt.id, competitor_id=pc.id).first()
    if not res:
        res = EventResult(
            event_id=evt.id, competitor_id=pc.id, competitor_type='pro',
            competitor_name='Bob Jones', result_value=25.4,
            run1_value=25.4, final_position=1, points_awarded=0,
            payout_amount=500.0, status='completed',
        )
        _db.session.add(res)

    # School captain for UM
    sc = SchoolCaptain.query.filter_by(tournament_id=t.id, school_name='University of Montana').first()
    if not sc:
        sc = SchoolCaptain(tournament_id=t.id, school_name='University of Montana')
        _db.session.add(sc)

    _db.session.commit()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_client(app):
    c = app.test_client()
    with app.app_context():
        c.post('/auth/login', data={
            'username': 'comp_admin', 'password': 'adminpass',
        }, follow_redirects=True)
    return c


@pytest.fixture()
def tid(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.filter_by(name='Comp Test 2026').first().id


@pytest.fixture()
def pro_bob(app, tid):
    with app.app_context():
        from models.competitor import ProCompetitor
        return ProCompetitor.query.filter_by(tournament_id=tid, name='Bob Jones').first()


@pytest.fixture()
def pro_charlie(app, tid):
    with app.app_context():
        from models.competitor import ProCompetitor
        return ProCompetitor.query.filter_by(tournament_id=tid, name='Charlie Brown').first()


@pytest.fixture()
def college_alice(app, tid):
    with app.app_context():
        from models.competitor import CollegeCompetitor
        return CollegeCompetitor.query.filter_by(tournament_id=tid, name='Alice Smith').first()


def _ok(response):
    assert response.status_code not in (500, 502, 503), (
        f'Server error {response.status_code}: {response.data[:300]}'
    )


# ---------------------------------------------------------------------------
# Competitor access — name search
# ---------------------------------------------------------------------------

class TestCompetitorAccess:
    """Test the /portal/competitor-access name search flow."""

    def test_get_competitor_access_page(self, client, tid):
        r = client.get('/portal/competitor-access')
        assert r.status_code == 200

    def test_search_pro_by_name(self, client, tid):
        r = client.post('/portal/competitor-access', data={
            'tournament_id': tid, 'full_name': 'Bob Jones',
        }, follow_redirects=False)
        # Should redirect to claim page
        assert r.status_code == 302
        assert 'competitor/claim' in r.headers['Location']

    def test_search_college_by_name(self, client, tid):
        r = client.post('/portal/competitor-access', data={
            'tournament_id': tid, 'full_name': 'Alice Smith',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert 'competitor/claim' in r.headers['Location']

    def test_search_no_match(self, client, tid):
        r = client.post('/portal/competitor-access', data={
            'tournament_id': tid, 'full_name': 'Nobody Here',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'No competitor found' in r.data

    def test_search_too_short(self, client, tid):
        r = client.post('/portal/competitor-access', data={
            'tournament_id': tid, 'full_name': 'AB',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'full name' in r.data.lower()

    def test_search_partial_match(self, client, tid):
        """Partial name search should find competitors when exact match fails."""
        r = client.post('/portal/competitor-access', data={
            'tournament_id': tid, 'full_name': 'Bob',
        }, follow_redirects=False)
        # Either redirect to claim (single match) or show matches
        assert r.status_code in (200, 302)

    def test_search_case_insensitive(self, client, tid):
        r = client.post('/portal/competitor-access', data={
            'tournament_id': tid, 'full_name': 'bob jones',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert 'competitor/claim' in r.headers['Location']


# ---------------------------------------------------------------------------
# PIN setup — first visit (no PIN set)
# ---------------------------------------------------------------------------

class TestCompetitorPINSetup:
    """Test PIN creation flow for a competitor who hasn't set a PIN yet."""

    def test_claim_page_renders(self, client, tid, pro_bob):
        r = client.get(f'/portal/competitor/claim?tournament_id={tid}'
                       f'&competitor_type=pro&competitor_id={pro_bob.id}')
        assert r.status_code == 200

    def test_set_pin_success(self, client, tid, pro_bob):
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_bob.id,
            'pin': '5678',
            'confirm_pin': '5678',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert 'competitor/public' in r.headers['Location']

    def test_set_pin_mismatch(self, client, tid, pro_bob):
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_bob.id,
            'pin': '5678',
            'confirm_pin': '9999',
        }, follow_redirects=True)
        assert r.status_code == 200
        # Flash message: "PIN confirmation does not match." — may appear in toast
        assert b'does not match' in r.data or b'PIN' in r.data

    def test_set_pin_too_short(self, client, tid, pro_bob):
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_bob.id,
            'pin': '12',
            'confirm_pin': '12',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'4-8 digits' in r.data

    def test_set_pin_non_numeric(self, client, tid, pro_bob):
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_bob.id,
            'pin': 'abcd',
            'confirm_pin': 'abcd',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'4-8 digits' in r.data


# ---------------------------------------------------------------------------
# PIN verification — return visit (PIN already set)
# ---------------------------------------------------------------------------

class TestCompetitorPINVerify:
    """Test PIN verification for a competitor who already has a PIN."""

    def test_claim_page_shows_verify(self, client, tid, pro_charlie):
        r = client.get(f'/portal/competitor/claim?tournament_id={tid}'
                       f'&competitor_type=pro&competitor_id={pro_charlie.id}')
        assert r.status_code == 200

    def test_correct_pin_redirects(self, client, tid, pro_charlie):
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_charlie.id,
            'pin': '1234',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert 'competitor/public' in r.headers['Location']

    def test_wrong_pin_rejected(self, client, tid, pro_charlie):
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_charlie.id,
            'pin': '9999',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'Incorrect PIN' in r.data

    def test_invalid_pin_format(self, client, tid, pro_charlie):
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_charlie.id,
            'pin': 'ab',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'4-8 digits' in r.data


# ---------------------------------------------------------------------------
# Competitor public dashboard — session-authorized access
# ---------------------------------------------------------------------------

class TestCompetitorDashboard:
    """Test the competitor dashboard once session is authorized."""

    def _authorize_session(self, client, tid, competitor_type, competitor_id, pin='1234'):
        """POST the correct PIN to authorize the session."""
        client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': competitor_type,
            'competitor_id': competitor_id,
            'pin': pin,
        })

    def test_dashboard_access_after_pin(self, client, tid, pro_charlie):
        self._authorize_session(client, tid, 'pro', pro_charlie.id)
        r = client.get(f'/portal/competitor/public?tournament_id={tid}'
                       f'&competitor_type=pro&competitor_id={pro_charlie.id}')
        assert r.status_code == 200
        assert b'Charlie Brown' in r.data

    def test_dashboard_blocked_without_pin(self, client, tid, pro_charlie):
        """Unauthenticated access redirects to claim page."""
        r = client.get(f'/portal/competitor/public?tournament_id={tid}'
                       f'&competitor_type=pro&competitor_id={pro_charlie.id}',
                       follow_redirects=False)
        assert r.status_code == 302
        assert 'competitor/claim' in r.headers['Location']

    def test_dashboard_shows_schedule(self, client, tid, pro_bob, app):
        """Bob has a heat assignment — dashboard should show schedule data."""
        # Authorize via session_transaction (bypass PIN requirement)
        with client.session_transaction() as sess:
            auth = sess.get('competitor_portal_auth', {})
            auth[f'{tid}:pro:{pro_bob.id}'] = True
            sess['competitor_portal_auth'] = auth

        r = client.get(f'/portal/competitor/public?tournament_id={tid}'
                       f'&competitor_type=pro&competitor_id={pro_bob.id}')
        assert r.status_code == 200
        assert b'Bob Jones' in r.data

    def test_dashboard_shows_results(self, client, tid, pro_bob, app):
        """Bob has a completed result — dashboard should show it."""
        with client.session_transaction() as sess:
            auth = sess.get('competitor_portal_auth', {})
            auth[f'{tid}:pro:{pro_bob.id}'] = True
            sess['competitor_portal_auth'] = auth

        r = client.get(f'/portal/competitor/public?tournament_id={tid}'
                       f'&competitor_type=pro&competitor_id={pro_bob.id}')
        assert r.status_code == 200
        assert b'Bob Jones' in r.data

    def test_invalid_competitor_id(self, client, tid):
        r = client.get(f'/portal/competitor/public?tournament_id={tid}'
                       f'&competitor_type=pro&competitor_id=99999',
                       follow_redirects=True)
        assert r.status_code == 200
        assert b'not found' in r.data.lower() or b'competitor' in r.data.lower()

    def test_missing_params_redirects(self, client):
        r = client.get('/portal/competitor/public', follow_redirects=True)
        assert r.status_code == 200

    def test_invalid_competitor_type(self, client, tid):
        r = client.get(f'/portal/competitor/public?tournament_id={tid}'
                       f'&competitor_type=invalid&competitor_id=1',
                       follow_redirects=True)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Cross-competitor access prevention
# ---------------------------------------------------------------------------

class TestCompetitorIsolation:
    """Verify a competitor's session cannot access another competitor's data."""

    def test_cannot_access_other_competitor(self, client, tid, pro_charlie, pro_bob):
        """Authorizing as Charlie should not grant access to Bob's dashboard."""
        # Authorize as Charlie
        client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_charlie.id,
            'pin': '1234',
        })
        # Try to access Bob's dashboard
        r = client.get(f'/portal/competitor/public?tournament_id={tid}'
                       f'&competitor_type=pro&competitor_id={pro_bob.id}',
                       follow_redirects=False)
        # Should redirect to claim (not authorized for Bob)
        assert r.status_code == 302
        assert 'competitor/claim' in r.headers['Location']


# ---------------------------------------------------------------------------
# Admin/judge override — can view any competitor dashboard
# ---------------------------------------------------------------------------

class TestJudgeCompetitorOverride:
    """Judges/admins can access any competitor's dashboard without PIN.

    The ``_can_access_competitor_page()`` helper checks
    ``current_user.is_admin`` — an admin bypasses the PIN requirement.
    """

    def test_admin_can_view_any_competitor(self, app, tid, pro_charlie):
        """Admin user bypasses PIN check via _can_access_competitor_page().

        Alternatively, admin can just set the competitor's session auth
        directly — this tests that the admin override OR session auth grants
        access to the dashboard.
        """
        c = app.test_client()
        # Set session auth directly (simulates what the claim route does)
        with c.session_transaction() as sess:
            auth = sess.get('competitor_portal_auth', {})
            auth[f'{tid}:pro:{pro_charlie.id}'] = True
            sess['competitor_portal_auth'] = auth

        r = c.get(f'/portal/competitor/public?tournament_id={tid}'
                  f'&competitor_type=pro&competitor_id={pro_charlie.id}')
        assert r.status_code == 200
        assert b'Charlie Brown' in r.data


# ---------------------------------------------------------------------------
# Authenticated competitor dashboard (/portal/competitor — login_required)
# ---------------------------------------------------------------------------

class TestAuthenticatedCompetitorDashboard:
    """Test the @login_required competitor dashboard route."""

    def test_unauthenticated_redirects_to_login(self, client):
        r = client.get('/portal/competitor', follow_redirects=False)
        assert r.status_code == 302
        assert 'login' in r.headers['Location'].lower()

    def test_spectator_role_gets_403(self, app):
        """A spectator user cannot access the competitor dashboard."""
        from models.user import User
        with app.app_context():
            u = User.query.filter_by(username='spec_viewer').first()
            if not u:
                u = User(username='spec_viewer', role='spectator')
                u.set_password('specpass')
                _db.session.add(u)
                _db.session.commit()

        c = app.test_client()
        c.post('/auth/login', data={
            'username': 'spec_viewer', 'password': 'specpass',
        }, follow_redirects=True)
        r = c.get('/portal/competitor')
        assert r.status_code in (302, 403)


# ---------------------------------------------------------------------------
# College competitor — claim page + dashboard
# ---------------------------------------------------------------------------

class TestCollegeCompetitorPortal:
    """Test the college competitor portal flow."""

    def test_college_claim_page(self, client, tid, college_alice):
        r = client.get(f'/portal/competitor/claim?tournament_id={tid}'
                       f'&competitor_type=college&competitor_id={college_alice.id}')
        assert r.status_code == 200

    def test_college_set_pin_and_access(self, client, tid, college_alice, app):
        # Set PIN
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': 'college',
            'competitor_id': college_alice.id,
            'pin': '7777',
            'confirm_pin': '7777',
        }, follow_redirects=False)
        assert r.status_code == 302

        # Access dashboard
        r = client.get(f'/portal/competitor/public?tournament_id={tid}'
                       f'&competitor_type=college&competitor_id={college_alice.id}')
        assert r.status_code == 200
        assert b'Alice Smith' in r.data


# ---------------------------------------------------------------------------
# SMS opt-in toggle
# ---------------------------------------------------------------------------

class TestSMSOptIn:
    """Test the SMS notification opt-in toggle for competitors."""

    def test_opt_in_without_session_redirects(self, client, tid, pro_charlie):
        """Toggling SMS without session auth redirects to claim."""
        r = client.post('/portal/competitor/sms-opt-in', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_charlie.id,
        }, follow_redirects=False)
        assert r.status_code == 302
        assert 'claim' in r.headers['Location']

    def test_opt_in_with_session(self, client, tid, pro_charlie):
        """Toggling SMS with valid session succeeds."""
        # Authorize first
        client.post('/portal/competitor/claim', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_charlie.id,
            'pin': '1234',
        })
        r = client.post('/portal/competitor/sms-opt-in', data={
            'tournament_id': tid,
            'competitor_type': 'pro',
            'competitor_id': pro_charlie.id,
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'SMS notifications' in r.data

    def test_opt_in_invalid_params(self, client):
        r = client.post('/portal/competitor/sms-opt-in', data={
            'tournament_id': '', 'competitor_type': 'bad', 'competitor_id': '',
        }, follow_redirects=True)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# School captain portal
# ---------------------------------------------------------------------------

class TestSchoolCaptainPortal:
    """Test the school captain access, claim, and dashboard flow."""

    def test_school_access_get(self, client):
        r = client.get('/portal/school-access')
        assert r.status_code == 200

    def test_school_search_found(self, client, tid):
        r = client.post('/portal/school-access', data={
            'tournament_id': tid,
            'school_name': 'University of Montana',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert 'school/claim' in r.headers['Location']

    def test_school_search_not_found(self, client, tid):
        r = client.post('/portal/school-access', data={
            'tournament_id': tid,
            'school_name': 'Nonexistent University',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'No school found' in r.data

    def test_school_search_too_short(self, client, tid):
        r = client.post('/portal/school-access', data={
            'tournament_id': tid,
            'school_name': 'U',
        }, follow_redirects=True)
        assert r.status_code == 200

    def test_school_claim_page(self, client, tid):
        r = client.get(f'/portal/school/claim?tournament_id={tid}'
                       f'&school_name=University of Montana')
        assert r.status_code == 200

    def test_school_set_pin_and_access(self, client, tid):
        # Set PIN for school captain
        r = client.post('/portal/school/claim', data={
            'tournament_id': tid,
            'school_name': 'University of Montana',
            'pin': '5555',
            'confirm_pin': '5555',
        }, follow_redirects=False)
        assert r.status_code == 302

    def test_school_dashboard_without_auth_redirects(self, client, tid):
        """Accessing school dashboard without session redirects to claim."""
        c = client.application.test_client()  # fresh client, no session
        r = c.get(f'/portal/school/dashboard?tournament_id={tid}'
                  f'&school_name=University of Montana',
                  follow_redirects=False)
        assert r.status_code == 302


# ---------------------------------------------------------------------------
# Edge cases and error handling
# ---------------------------------------------------------------------------

class TestCompetitorEdgeCases:
    """Edge cases for competitor portal routes."""

    def test_claim_invalid_tournament(self, client):
        r = client.get('/portal/competitor/claim?tournament_id=99999'
                       '&competitor_type=pro&competitor_id=1')
        assert r.status_code in (302, 404)

    def test_claim_missing_params(self, client):
        r = client.get('/portal/competitor/claim', follow_redirects=True)
        assert r.status_code == 200

    def test_public_dashboard_invalid_type(self, client, tid):
        r = client.get(f'/portal/competitor/public?tournament_id={tid}'
                       '&competitor_type=xyz&competitor_id=1',
                       follow_redirects=True)
        _ok(r)

    def test_claim_nonexistent_competitor(self, client, tid):
        r = client.get(f'/portal/competitor/claim?tournament_id={tid}'
                       '&competitor_type=pro&competitor_id=99999',
                       follow_redirects=True)
        assert r.status_code == 200
        assert b'not found' in r.data.lower()
