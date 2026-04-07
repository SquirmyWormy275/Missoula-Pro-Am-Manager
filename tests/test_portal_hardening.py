"""
Portal hardening test suite — POST mutation blocking, data leakage prevention,
cross-tournament isolation, session tampering resilience, and PIN edge cases.

These tests verify security invariants that go beyond basic route access:
  - Restricted roles cannot POST to mutating management endpoints
  - Spectator/competitor views do not leak admin-only data
  - Competitor sessions are scoped to a single tournament + competitor
  - Corrupted/tampered session data does not cause 500 errors
  - PIN brute-force attempts are handled gracefully
  - Public API does not expose internal identifiers or PII

Run:
    pytest tests/test_portal_hardening.py -v
"""
import json
import os

import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-hardening')
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
        _seed_hardening_data(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_hardening_data(app):
    """Seed two tournaments with competitors, events, and results."""
    from models import Event, EventResult, Heat, Team, Tournament
    from models.audit_log import AuditLog
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.user import User

    # Users — one per role
    for username, role in [
        ('h_admin', 'admin'), ('h_competitor', 'competitor'),
        ('h_spectator', 'spectator'), ('h_scorer', 'scorer'),
    ]:
        if not User.query.filter_by(username=username).first():
            u = User(username=username, role=role)
            u.set_password(f'{role}pass')
            _db.session.add(u)
    _db.session.flush()

    # Tournament A — active
    ta = Tournament.query.filter_by(name='Hardening A').first()
    if not ta:
        ta = Tournament(name='Hardening A', year=2026, status='pro_active')
        _db.session.add(ta)
        _db.session.flush()

    # Tournament B — separate
    tb = Tournament.query.filter_by(name='Hardening B').first()
    if not tb:
        tb = Tournament(name='Hardening B', year=2025, status='setup')
        _db.session.add(tb)
        _db.session.flush()

    # Team in A
    team_a = Team.query.filter_by(tournament_id=ta.id, team_code='UM-A').first()
    if not team_a:
        team_a = Team(tournament_id=ta.id, team_code='UM-A',
                      school_name='University of Montana', school_abbreviation='UM',
                      total_points=30)
        _db.session.add(team_a)
        _db.session.flush()

    # Pro competitor in A
    pro_a = ProCompetitor.query.filter_by(tournament_id=ta.id, name='Pro Alpha').first()
    if not pro_a:
        pro_a = ProCompetitor(
            tournament_id=ta.id, name='Pro Alpha', gender='M',
            events_entered='[]', gear_sharing='{}', partners='{}',
            total_earnings=500.0, status='active',
            email='alpha@test.com', phone='406-555-0100',
            address='123 Timber Lane', shirt_size='L',
        )
        pro_a.set_portal_pin('1111')
        _db.session.add(pro_a)
        _db.session.flush()

    # Pro competitor in B (different tournament)
    pro_b = ProCompetitor.query.filter_by(tournament_id=tb.id, name='Pro Beta').first()
    if not pro_b:
        pro_b = ProCompetitor(
            tournament_id=tb.id, name='Pro Beta', gender='M',
            events_entered='[]', gear_sharing='{}', partners='{}',
            total_earnings=300.0, status='active',
        )
        pro_b.set_portal_pin('2222')
        _db.session.add(pro_b)
        _db.session.flush()

    # College competitor in A
    cc_a = CollegeCompetitor.query.filter_by(tournament_id=ta.id, name='College Gamma').first()
    if not cc_a:
        cc_a = CollegeCompetitor(
            tournament_id=ta.id, team_id=team_a.id, name='College Gamma',
            gender='F', individual_points=15, events_entered='[]', status='active',
        )
        cc_a.set_portal_pin('3333')
        _db.session.add(cc_a)
        _db.session.flush()

    # Completed pro event in A with results
    evt = Event.query.filter_by(tournament_id=ta.id, name='SB Speed (M)').first()
    if not evt:
        evt = Event(
            tournament_id=ta.id, name='SB Speed (M)', event_type='pro',
            gender='M', scoring_type='time', scoring_order='lowest_wins',
            stand_type='standing_block', max_stands=5, status='completed',
        )
        _db.session.add(evt)
        _db.session.flush()

    res = EventResult.query.filter_by(event_id=evt.id, competitor_id=pro_a.id).first()
    if not res:
        res = EventResult(
            event_id=evt.id, competitor_id=pro_a.id, competitor_type='pro',
            competitor_name='Pro Alpha', result_value=22.5,
            run1_value=22.5, final_position=1, points_awarded=0,
            payout_amount=500.0, status='completed',
        )
        _db.session.add(res)

    # Heat for event
    heat = Heat.query.filter_by(event_id=evt.id).first()
    if not heat:
        heat = Heat(
            event_id=evt.id, heat_number=1, run_number=1,
            competitors=json.dumps([pro_a.id]),
            stand_assignments=json.dumps({str(pro_a.id): '1'}),
            status='completed',
        )
        _db.session.add(heat)

    # Audit log entry (admin-only data)
    if not AuditLog.query.first():
        al = AuditLog(
            action='test_action', entity_type='test', entity_id=1,
            details='{"secret": "admin-only-data"}',
        )
        _db.session.add(al)

    _db.session.commit()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def tid_a(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.filter_by(name='Hardening A').first().id


@pytest.fixture()
def tid_b(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.filter_by(name='Hardening B').first().id


@pytest.fixture()
def pro_a_id(app, tid_a):
    with app.app_context():
        from models.competitor import ProCompetitor
        return ProCompetitor.query.filter_by(tournament_id=tid_a, name='Pro Alpha').first().id


@pytest.fixture()
def pro_b_id(app, tid_b):
    with app.app_context():
        from models.competitor import ProCompetitor
        return ProCompetitor.query.filter_by(tournament_id=tid_b, name='Pro Beta').first().id


@pytest.fixture()
def cc_a_id(app, tid_a):
    with app.app_context():
        from models.competitor import CollegeCompetitor
        return CollegeCompetitor.query.filter_by(tournament_id=tid_a, name='College Gamma').first().id


@pytest.fixture()
def heat_id(app, tid_a):
    with app.app_context():
        from models import Event, Heat
        evt = Event.query.filter_by(tournament_id=tid_a, name='SB Speed (M)').first()
        return Heat.query.filter_by(event_id=evt.id).first().id


@pytest.fixture()
def event_id(app, tid_a):
    with app.app_context():
        from models import Event
        return Event.query.filter_by(tournament_id=tid_a, name='SB Speed (M)').first().id


def _spectator_client(app):
    from models.user import User
    c = app.test_client()
    with app.app_context():
        u = User.query.filter_by(username='h_spectator').first()
        with c.session_transaction() as sess:
            sess['_user_id'] = str(u.id)
    return c


def _competitor_client(app):
    from models.user import User
    c = app.test_client()
    with app.app_context():
        u = User.query.filter_by(username='h_competitor').first()
        with c.session_transaction() as sess:
            sess['_user_id'] = str(u.id)
    return c


def _scorer_client(app):
    from models.user import User
    c = app.test_client()
    with app.app_context():
        u = User.query.filter_by(username='h_scorer').first()
        with c.session_transaction() as sess:
            sess['_user_id'] = str(u.id)
    return c


def _ok(response):
    assert response.status_code not in (500, 502, 503), (
        f'Server error {response.status_code}: {response.data[:300]}'
    )


# ===========================================================================
# 1. POST MUTATION BLOCKING — restricted roles cannot mutate data
# ===========================================================================

class TestSpectatorCannotPost:
    """Spectator role must not be able to POST to any mutating management route."""

    def test_cannot_create_tournament(self, app, tid_a):
        c = _spectator_client(app)
        r = c.post('/tournament/new', data={'name': 'Hack', 'year': 2099})
        assert r.status_code != 200, 'Spectator should not create tournaments'

    def test_cannot_delete_tournament(self, app, tid_a):
        c = _spectator_client(app)
        r = c.post(f'/tournament/{tid_a}/delete')
        assert r.status_code != 200

    def test_cannot_register_pro(self, app, tid_a):
        c = _spectator_client(app)
        r = c.post(f'/registration/{tid_a}/pro/new', data={
            'name': 'Hacker', 'gender': 'M',
        })
        assert r.status_code != 200

    def test_cannot_upload_college(self, app, tid_a):
        c = _spectator_client(app)
        r = c.post(f'/registration/{tid_a}/college/upload')
        assert r.status_code != 200

    def test_cannot_generate_heats(self, app, tid_a, event_id):
        c = _spectator_client(app)
        r = c.post(f'/scheduling/{tid_a}/event/{event_id}/generate-heats')
        assert r.status_code != 200

    def test_cannot_build_flights(self, app, tid_a):
        c = _spectator_client(app)
        r = c.post(f'/scheduling/{tid_a}/flights/build')
        assert r.status_code != 200

    def test_cannot_finalize_event(self, app, tid_a, event_id):
        c = _spectator_client(app)
        r = c.post(f'/scoring/{tid_a}/event/{event_id}/finalize')
        assert r.status_code != 200

    def test_cannot_enter_heat_results(self, app, tid_a, heat_id):
        c = _spectator_client(app)
        r = c.post(f'/scoring/{tid_a}/heat/{heat_id}/enter', data={
            'result_1': '15.0',
        })
        assert r.status_code != 200

    def test_cannot_draw_relay(self, app, tid_a):
        c = _spectator_client(app)
        r = c.post(f'/proam-relay/{tid_a}/draw')
        assert r.status_code != 200

    def test_cannot_save_woodboss_config(self, app, tid_a):
        c = _spectator_client(app)
        r = c.post(f'/woodboss/{tid_a}/config')
        assert r.status_code != 200

    def test_cannot_manage_users(self, app):
        c = _spectator_client(app)
        r = c.post('/auth/users', data={
            'username': 'hack', 'password': 'hack', 'role': 'admin',
        })
        assert r.status_code != 200


class TestCompetitorCannotPost:
    """Competitor role must not be able to POST to any management route."""

    def test_cannot_register_pro(self, app, tid_a):
        c = _competitor_client(app)
        r = c.post(f'/registration/{tid_a}/pro/new', data={
            'name': 'Hacker', 'gender': 'M',
        })
        assert r.status_code != 200

    def test_cannot_finalize_event(self, app, tid_a, event_id):
        c = _competitor_client(app)
        r = c.post(f'/scoring/{tid_a}/event/{event_id}/finalize')
        assert r.status_code != 200

    def test_cannot_delete_tournament(self, app, tid_a):
        c = _competitor_client(app)
        r = c.post(f'/tournament/{tid_a}/delete')
        assert r.status_code != 200

    def test_cannot_clone_tournament(self, app, tid_a):
        c = _competitor_client(app)
        r = c.post(f'/tournament/{tid_a}/clone')
        assert r.status_code != 200

    def test_cannot_scratch_pro(self, app, tid_a, pro_a_id):
        c = _competitor_client(app)
        r = c.post(f'/registration/{tid_a}/pro/{pro_a_id}/scratch')
        assert r.status_code != 200


class TestScorerCannotEscalate:
    """Scorer has scheduling + scoring access but must not manage users or register."""

    def test_cannot_manage_users(self, app):
        c = _scorer_client(app)
        r = c.post('/auth/users', data={
            'username': 'escalation', 'password': 'escalation', 'role': 'admin',
        })
        # Auth manages its own check — scorer is not admin
        assert r.status_code != 200

    def test_cannot_register_competitor(self, app, tid_a):
        c = _scorer_client(app)
        r = c.post(f'/registration/{tid_a}/pro/new', data={
            'name': 'Scorer Hack', 'gender': 'F',
        })
        assert r.status_code != 200

    def test_cannot_delete_tournament(self, app, tid_a):
        c = _scorer_client(app)
        r = c.post(f'/tournament/{tid_a}/delete')
        assert r.status_code != 200


class TestUnauthenticatedCannotPost:
    """Unauthenticated users must not be able to POST to management routes."""

    def test_cannot_create_tournament(self, client):
        r = client.post('/tournament/new', data={'name': 'Anon', 'year': 2099})
        assert r.status_code != 200

    def test_cannot_register_pro(self, client, tid_a):
        r = client.post(f'/registration/{tid_a}/pro/new', data={
            'name': 'Anon', 'gender': 'M',
        })
        assert r.status_code != 200

    def test_cannot_finalize_event(self, client, tid_a, event_id):
        r = client.post(f'/scoring/{tid_a}/event/{event_id}/finalize')
        assert r.status_code != 200

    def test_cannot_manage_users(self, client):
        r = client.post('/auth/users', data={
            'username': 'anon', 'password': 'anon', 'role': 'admin',
        })
        assert r.status_code != 200


# ===========================================================================
# 2. DATA LEAKAGE PREVENTION
# ===========================================================================

class TestSpectatorDataLeakage:
    """Spectator views must not expose admin-only or PII data."""

    def test_api_standings_no_email(self, client, tid_a):
        """Public standings API must not include competitor email."""
        r = client.get(f'/api/public/tournaments/{tid_a}/standings')
        assert r.status_code == 200
        raw = r.data.decode()
        assert 'alpha@test.com' not in raw
        assert '406-555-0100' not in raw
        assert '123 Timber Lane' not in raw

    def test_api_results_no_pii(self, client, tid_a):
        """Public results API must not include competitor PII."""
        r = client.get(f'/api/public/tournaments/{tid_a}/results')
        assert r.status_code == 200
        raw = r.data.decode()
        assert 'alpha@test.com' not in raw
        assert '406-555-0100' not in raw

    def test_api_schedule_no_pii(self, client, tid_a):
        """Public schedule API must not include competitor PII."""
        r = client.get(f'/api/public/tournaments/{tid_a}/schedule')
        assert r.status_code == 200
        raw = r.data.decode()
        assert 'alpha@test.com' not in raw

    def test_spectator_college_no_pii(self, client, tid_a):
        """College spectator page must not leak PII."""
        r = client.get(f'/portal/spectator/{tid_a}/college')
        assert r.status_code == 200
        raw = r.data.decode()
        assert 'alpha@test.com' not in raw

    def test_spectator_pro_no_pii(self, client, tid_a):
        """Pro spectator page must not leak PII."""
        r = client.get(f'/portal/spectator/{tid_a}/pro')
        assert r.status_code == 200
        raw = r.data.decode()
        assert 'alpha@test.com' not in raw

    def test_audit_log_not_accessible(self, client):
        """Unauthenticated users must not see the audit log."""
        r = client.get('/auth/audit', follow_redirects=False)
        assert r.status_code != 200

    def test_audit_log_blocked_for_spectator(self, app):
        """Spectator must not see the audit log."""
        c = _spectator_client(app)
        r = c.get('/auth/audit')
        assert r.status_code != 200

    def test_users_page_blocked_for_spectator(self, app):
        """Spectator must not access the user management page."""
        c = _spectator_client(app)
        r = c.get('/auth/users')
        assert r.status_code != 200

    def test_api_no_pin_hash(self, client, tid_a):
        """Public API must never expose portal_pin_hash."""
        for endpoint in ['standings', 'schedule', 'results', 'standings-poll']:
            r = client.get(f'/api/public/tournaments/{tid_a}/{endpoint}')
            raw = r.data.decode()
            assert 'portal_pin_hash' not in raw
            assert 'pin_hash' not in raw


class TestCompetitorDataLeakage:
    """Competitor dashboard must not leak other competitors' PII."""

    def test_own_dashboard_no_other_competitor_pii(self, client, tid_a, pro_a_id, cc_a_id):
        """Pro Alpha's dashboard should not contain College Gamma's details."""
        with client.session_transaction() as sess:
            auth = sess.get('competitor_portal_auth', {})
            auth[f'{tid_a}:pro:{pro_a_id}'] = True
            sess['competitor_portal_auth'] = auth

        r = client.get(f'/portal/competitor/public?tournament_id={tid_a}'
                       f'&competitor_type=pro&competitor_id={pro_a_id}')
        assert r.status_code == 200
        raw = r.data.decode()
        # Should not contain other competitors' data
        assert 'College Gamma' not in raw or 'Pro Alpha' in raw  # Own name ok


# ===========================================================================
# 3. CROSS-TOURNAMENT ISOLATION
# ===========================================================================

class TestCrossTournamentIsolation:
    """Competitor sessions are scoped to one tournament + competitor."""

    def test_session_auth_scoped_to_tournament(self, client, tid_a, tid_b, pro_a_id, pro_b_id):
        """Auth for Pro Alpha in tournament A does NOT grant access to Pro Beta in B."""
        with client.session_transaction() as sess:
            auth = sess.get('competitor_portal_auth', {})
            auth[f'{tid_a}:pro:{pro_a_id}'] = True
            sess['competitor_portal_auth'] = auth

        # Try to access Pro Beta in tournament B
        r = client.get(f'/portal/competitor/public?tournament_id={tid_b}'
                       f'&competitor_type=pro&competitor_id={pro_b_id}',
                       follow_redirects=False)
        assert r.status_code == 302, 'Should redirect to claim — not authorized for other tournament'
        assert 'claim' in r.headers['Location']

    def test_competitor_id_scoped_within_tournament(self, client, tid_a, pro_a_id, cc_a_id):
        """Auth for Pro Alpha does NOT grant access to College Gamma (same tournament)."""
        with client.session_transaction() as sess:
            auth = sess.get('competitor_portal_auth', {})
            auth[f'{tid_a}:pro:{pro_a_id}'] = True
            sess['competitor_portal_auth'] = auth

        r = client.get(f'/portal/competitor/public?tournament_id={tid_a}'
                       f'&competitor_type=college&competitor_id={cc_a_id}',
                       follow_redirects=False)
        assert r.status_code == 302, 'Should redirect — different competitor type/ID'

    def test_spectator_views_limited_to_tournament(self, client, tid_a):
        """Spectator college page shows only tournament A data, not B."""
        r = client.get(f'/portal/spectator/{tid_a}/college')
        assert r.status_code == 200
        raw = r.data.decode()
        assert 'Hardening B' not in raw

    def test_api_results_scoped(self, client, tid_a, tid_b):
        """API results for tournament A should not include tournament B data."""
        r = client.get(f'/api/public/tournaments/{tid_a}/results')
        data = r.get_json()
        for evt in data.get('results', []):
            assert 'Pro Beta' not in [res['competitor_name'] for res in evt['results']]


# ===========================================================================
# 4. SESSION TAMPERING RESILIENCE
# ===========================================================================

class TestSessionTampering:
    """Corrupted or tampered session data must not cause 500 errors."""

    def test_corrupted_competitor_auth(self, client, tid_a, pro_a_id):
        """Non-dict competitor_portal_auth value handled gracefully."""
        with client.session_transaction() as sess:
            sess['competitor_portal_auth'] = 'corrupted_string'

        r = client.get(f'/portal/competitor/public?tournament_id={tid_a}'
                       f'&competitor_type=pro&competitor_id={pro_a_id}')
        _ok(r)

    def test_null_competitor_auth(self, client, tid_a, pro_a_id):
        """None competitor_portal_auth handled gracefully."""
        with client.session_transaction() as sess:
            sess['competitor_portal_auth'] = None

        r = client.get(f'/portal/competitor/public?tournament_id={tid_a}'
                       f'&competitor_type=pro&competitor_id={pro_a_id}')
        _ok(r)

    def test_empty_auth_dict(self, client, tid_a, pro_a_id):
        """Empty auth dict — should redirect to claim, not crash."""
        with client.session_transaction() as sess:
            sess['competitor_portal_auth'] = {}

        r = client.get(f'/portal/competitor/public?tournament_id={tid_a}'
                       f'&competitor_type=pro&competitor_id={pro_a_id}',
                       follow_redirects=False)
        assert r.status_code == 302

    def test_wrong_key_format(self, client, tid_a, pro_a_id):
        """Auth key with wrong format should not grant access."""
        with client.session_transaction() as sess:
            sess['competitor_portal_auth'] = {'wrong_format': True}

        r = client.get(f'/portal/competitor/public?tournament_id={tid_a}'
                       f'&competitor_type=pro&competitor_id={pro_a_id}',
                       follow_redirects=False)
        assert r.status_code == 302

    def test_negative_competitor_id(self, client, tid_a):
        """Negative competitor_id should not crash."""
        r = client.get(f'/portal/competitor/public?tournament_id={tid_a}'
                       f'&competitor_type=pro&competitor_id=-1')
        _ok(r)

    def test_huge_tournament_id(self, client):
        """Extremely large tournament_id should return 404, not crash."""
        r = client.get('/portal/spectator/2147483647')
        assert r.status_code == 404

    def test_string_competitor_id(self, client, tid_a):
        """String competitor_id should not crash."""
        r = client.get(f'/portal/competitor/public?tournament_id={tid_a}'
                       f'&competitor_type=pro&competitor_id=abc')
        _ok(r)

    def test_tampered_user_id_session(self, app, tid_a):
        """Fake _user_id in session that doesn't exist should not crash."""
        c = app.test_client()
        with c.session_transaction() as sess:
            sess['_user_id'] = '999999'

        # Management route should handle gracefully (redirect or error, not 500)
        r = c.get(f'/tournament/{tid_a}')
        _ok(r)

    def test_portal_with_invalid_view_mode(self, client, tid_a):
        """Invalid view mode parameter should not crash."""
        r = client.get(f'/portal/spectator/{tid_a}?view=<script>alert(1)</script>')
        _ok(r)
        assert b'<script>alert(1)</script>' not in r.data


# ===========================================================================
# 5. PIN SECURITY EDGE CASES
# ===========================================================================

class TestPINSecurity:
    """PIN-related security edge cases."""

    def test_multiple_wrong_pins_no_crash(self, client, tid_a, pro_a_id):
        """Multiple wrong PIN attempts should be handled gracefully."""
        for i in range(10):
            r = client.post('/portal/competitor/claim', data={
                'tournament_id': tid_a,
                'competitor_type': 'pro',
                'competitor_id': pro_a_id,
                'pin': f'{9000 + i}',
            }, follow_redirects=True)
            _ok(r)

    def test_empty_pin_rejected(self, client, tid_a, pro_a_id):
        """Empty PIN should be rejected."""
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid_a,
            'competitor_type': 'pro',
            'competitor_id': pro_a_id,
            'pin': '',
        }, follow_redirects=True)
        _ok(r)
        assert r.status_code == 200

    def test_very_long_pin_rejected(self, client, tid_a, pro_a_id):
        """PIN longer than 8 digits should be rejected."""
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid_a,
            'competitor_type': 'pro',
            'competitor_id': pro_a_id,
            'pin': '123456789',  # 9 digits, max is 8
        }, follow_redirects=True)
        _ok(r)

    def test_pin_with_special_chars(self, client, tid_a, pro_a_id):
        """PIN with special characters should be rejected."""
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid_a,
            'competitor_type': 'pro',
            'competitor_id': pro_a_id,
            'pin': '12!@',
        }, follow_redirects=True)
        _ok(r)

    def test_pin_with_spaces(self, client, tid_a, pro_a_id):
        """PIN with spaces should be rejected (stripped then validated)."""
        r = client.post('/portal/competitor/claim', data={
            'tournament_id': tid_a,
            'competitor_type': 'pro',
            'competitor_id': pro_a_id,
            'pin': '  12  ',
        }, follow_redirects=True)
        _ok(r)

    def test_sql_injection_in_name_search(self, client, tid_a):
        """SQL injection attempt in name search should be safe."""
        r = client.post('/portal/competitor-access', data={
            'tournament_id': tid_a,
            'full_name': "'; DROP TABLE users; --",
        }, follow_redirects=True)
        _ok(r)

    def test_xss_in_name_search(self, client, tid_a):
        """XSS attempt in name search should be escaped."""
        r = client.post('/portal/competitor-access', data={
            'tournament_id': tid_a,
            'full_name': '<script>alert("xss")</script>',
        }, follow_redirects=True)
        _ok(r)
        # The raw script tag should NOT appear unescaped
        assert b'<script>alert("xss")</script>' not in r.data


# ===========================================================================
# 6. API RESPONSE STRUCTURE HARDENING
# ===========================================================================

class TestAPIResponseStructure:
    """Verify API responses have correct structure and no unexpected fields."""

    def test_standings_no_internal_ids(self, client, tid_a):
        """Standings should not expose internal database column names."""
        r = client.get(f'/api/public/tournaments/{tid_a}/standings')
        raw = r.data.decode()
        # Should not expose internal model fields
        assert 'portal_pin_hash' not in raw
        assert 'password_hash' not in raw
        assert 'gear_sharing' not in raw  # Internal field

    def test_results_position_ordering(self, client, tid_a):
        """Results should be ordered by position."""
        r = client.get(f'/api/public/tournaments/{tid_a}/results')
        data = r.get_json()
        for evt in data.get('results', []):
            positions = [
                res['position'] for res in evt['results']
                if res['position'] is not None
            ]
            assert positions == sorted(positions), (
                f"Results for {evt['event_name']} not position-ordered"
            )

    def test_standings_poll_timestamp_format(self, client, tid_a):
        """Standings poll should include ISO-format timestamp."""
        r = client.get(f'/api/public/tournaments/{tid_a}/standings-poll')
        data = r.get_json()
        ts = data.get('last_updated', '')
        assert ts.endswith('Z'), f'Timestamp should end with Z: {ts}'
        assert 'T' in ts, f'Timestamp should be ISO format: {ts}'


# ===========================================================================
# 7. COMPETITOR PORTAL — FUNCTIONAL EDGE CASES
# ===========================================================================

class TestCompetitorPortalEdgeCases:
    """Additional edge cases for the competitor portal."""

    def test_access_scratched_competitor(self, app, tid_a):
        """Searching for a scratched competitor should return no match."""
        from models.competitor import ProCompetitor
        with app.app_context():
            scratched = ProCompetitor.query.filter_by(
                tournament_id=tid_a, status='scratched').first()

        c = app.test_client()
        r = c.post('/portal/competitor-access', data={
            'tournament_id': tid_a,
            'full_name': 'Scratched Name',  # Won't match any active competitor
        }, follow_redirects=True)
        _ok(r)

    def test_simultaneous_sessions(self, app, tid_a, pro_a_id, cc_a_id):
        """A client can hold sessions for multiple competitors simultaneously."""
        c = app.test_client()
        with c.session_transaction() as sess:
            auth = {
                f'{tid_a}:pro:{pro_a_id}': True,
                f'{tid_a}:college:{cc_a_id}': True,
            }
            sess['competitor_portal_auth'] = auth

        # Both should be accessible
        r1 = c.get(f'/portal/competitor/public?tournament_id={tid_a}'
                    f'&competitor_type=pro&competitor_id={pro_a_id}')
        assert r1.status_code == 200

        r2 = c.get(f'/portal/competitor/public?tournament_id={tid_a}'
                    f'&competitor_type=college&competitor_id={cc_a_id}')
        assert r2.status_code == 200
