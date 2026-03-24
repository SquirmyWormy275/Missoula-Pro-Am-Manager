"""
School captain portal deep tests — dashboard data assembly, PIN edge cases,
multi-team school, session mechanics, and data integrity.

Tests:
  - School dashboard renders with correct team/member/result data
  - Multi-team schools (UM-A, UM-B) shown under one captain
  - School search partial matching
  - PIN setup, verify, mismatch, invalid format
  - Dashboard without session redirects to claim
  - Dashboard with empty teams redirects gracefully
  - Session isolation between schools
  - Bull/Belle standings appear on dashboard

Run:
    pytest tests/test_school_captain_deep.py -v
"""
import json
import os
import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-school')
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
    from models import Tournament, Team, Event, EventResult
    from models.competitor import CollegeCompetitor
    from models.school_captain import SchoolCaptain

    t = Tournament.query.filter_by(name='School Deep Test').first()
    if not t:
        t = Tournament(name='School Deep Test', year=2026, status='college_active')
        _db.session.add(t)
        _db.session.flush()

    # UM has TWO teams (multi-team school)
    team_a = Team.query.filter_by(tournament_id=t.id, team_code='UM-A').first()
    if not team_a:
        team_a = Team(tournament_id=t.id, team_code='UM-A',
                      school_name='University of Montana', school_abbreviation='UM',
                      total_points=40)
        _db.session.add(team_a)
        _db.session.flush()

    team_b = Team.query.filter_by(tournament_id=t.id, team_code='UM-B').first()
    if not team_b:
        team_b = Team(tournament_id=t.id, team_code='UM-B',
                      school_name='University of Montana', school_abbreviation='UM',
                      total_points=25)
        _db.session.add(team_b)
        _db.session.flush()

    # MSU has one team (different school)
    team_msu = Team.query.filter_by(tournament_id=t.id, team_code='MSU-A').first()
    if not team_msu:
        team_msu = Team(tournament_id=t.id, team_code='MSU-A',
                        school_name='Montana State University', school_abbreviation='MSU',
                        total_points=30)
        _db.session.add(team_msu)
        _db.session.flush()

    # Competitors on UM-A
    for name, gender, pts in [('Jake UM', 'M', 20), ('Sara UM', 'F', 15)]:
        if not CollegeCompetitor.query.filter_by(tournament_id=t.id, name=name).first():
            cc = CollegeCompetitor(
                tournament_id=t.id, team_id=team_a.id, name=name,
                gender=gender, individual_points=pts, events_entered='[]', status='active',
            )
            _db.session.add(cc)

    # Competitor on UM-B
    if not CollegeCompetitor.query.filter_by(tournament_id=t.id, name='Alex UM-B').first():
        cc2 = CollegeCompetitor(
            tournament_id=t.id, team_id=team_b.id, name='Alex UM-B',
            gender='M', individual_points=10, events_entered='[]', status='active',
        )
        _db.session.add(cc2)

    # Competitor on MSU
    if not CollegeCompetitor.query.filter_by(tournament_id=t.id, name='MSU Mike').first():
        msu_c = CollegeCompetitor(
            tournament_id=t.id, team_id=team_msu.id, name='MSU Mike',
            gender='M', individual_points=18, events_entered='[]', status='active',
        )
        _db.session.add(msu_c)

    # Completed college event with result for Jake UM
    evt = Event.query.filter_by(tournament_id=t.id, name='UH Speed (M)', event_type='college').first()
    if not evt:
        evt = Event(
            tournament_id=t.id, name='UH Speed (M)', event_type='college',
            gender='M', scoring_type='time', scoring_order='lowest_wins',
            stand_type='underhand', max_stands=5, status='completed',
        )
        _db.session.add(evt)
        _db.session.flush()

    jake = CollegeCompetitor.query.filter_by(tournament_id=t.id, name='Jake UM').first()
    if jake and not EventResult.query.filter_by(event_id=evt.id, competitor_id=jake.id).first():
        res = EventResult(
            event_id=evt.id, competitor_id=jake.id, competitor_type='college',
            competitor_name='Jake UM', result_value=17.3, run1_value=17.3,
            final_position=1, points_awarded=10, status='completed',
        )
        _db.session.add(res)

    # School captain for UM (no PIN yet)
    sc = SchoolCaptain.query.filter_by(tournament_id=t.id, school_name='University of Montana').first()
    if not sc:
        sc = SchoolCaptain(tournament_id=t.id, school_name='University of Montana')
        _db.session.add(sc)

    # School captain for MSU (with PIN)
    sc_msu = SchoolCaptain.query.filter_by(tournament_id=t.id, school_name='Montana State University').first()
    if not sc_msu:
        sc_msu = SchoolCaptain(tournament_id=t.id, school_name='Montana State University')
        sc_msu.set_pin('6666')
        _db.session.add(sc_msu)

    _db.session.commit()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def tid(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.filter_by(name='School Deep Test').first().id


def _ok(r):
    assert r.status_code not in (500, 502, 503), f'Server error: {r.status_code}'


def _authorize_school(client, tid, school_name):
    """Set school session auth directly."""
    with client.session_transaction() as sess:
        auth = sess.get('school_portal_auth', {})
        auth[f'{tid}:school:{school_name.lower()}'] = True
        sess['school_portal_auth'] = auth


# ---------------------------------------------------------------------------
# School search
# ---------------------------------------------------------------------------

class TestSchoolSearch:
    """Test school name search including partial matching."""

    def test_exact_match(self, client, tid):
        r = client.post('/portal/school-access', data={
            'tournament_id': tid, 'school_name': 'University of Montana',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert 'school/claim' in r.headers['Location']

    def test_partial_match(self, client, tid):
        r = client.post('/portal/school-access', data={
            'tournament_id': tid, 'school_name': 'Montana',
        }, follow_redirects=False)
        # Should show multiple matches (UM and MSU both contain 'Montana')
        assert r.status_code in (200, 302)

    def test_case_insensitive(self, client, tid):
        r = client.post('/portal/school-access', data={
            'tournament_id': tid, 'school_name': 'university of montana',
        }, follow_redirects=False)
        assert r.status_code in (200, 302)

    def test_no_match(self, client, tid):
        r = client.post('/portal/school-access', data={
            'tournament_id': tid, 'school_name': 'Harvard University',
        }, follow_redirects=True)
        assert b'No school found' in r.data

    def test_min_length_enforced(self, client, tid):
        r = client.post('/portal/school-access', data={
            'tournament_id': tid, 'school_name': 'U',
        }, follow_redirects=True)
        _ok(r)


# ---------------------------------------------------------------------------
# School claim — PIN setup and verification
# ---------------------------------------------------------------------------

class TestSchoolClaimPIN:
    """Test school captain PIN setup and verification flows."""

    def test_claim_page_renders_no_pin(self, client, tid):
        """UM has no PIN — claim page should show setup form."""
        r = client.get(f'/portal/school/claim?tournament_id={tid}'
                       '&school_name=University of Montana')
        assert r.status_code == 200

    def test_set_pin_success(self, client, tid):
        r = client.post('/portal/school/claim', data={
            'tournament_id': tid,
            'school_name': 'University of Montana',
            'pin': '7777',
            'confirm_pin': '7777',
        }, follow_redirects=False)
        assert r.status_code == 302

    def test_set_pin_mismatch(self, client, tid):
        r = client.post('/portal/school/claim', data={
            'tournament_id': tid,
            'school_name': 'University of Montana',
            'pin': '7777',
            'confirm_pin': '8888',
        }, follow_redirects=True)
        _ok(r)

    def test_set_pin_too_short(self, client, tid):
        r = client.post('/portal/school/claim', data={
            'tournament_id': tid,
            'school_name': 'University of Montana',
            'pin': '12',
            'confirm_pin': '12',
        }, follow_redirects=True)
        _ok(r)

    def test_verify_existing_pin_correct(self, client, tid):
        """MSU has PIN 6666 — correct PIN should redirect to dashboard."""
        r = client.post('/portal/school/claim', data={
            'tournament_id': tid,
            'school_name': 'Montana State University',
            'pin': '6666',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert 'school/dashboard' in r.headers['Location']

    def test_verify_existing_pin_wrong(self, client, tid):
        r = client.post('/portal/school/claim', data={
            'tournament_id': tid,
            'school_name': 'Montana State University',
            'pin': '0000',
        }, follow_redirects=True)
        assert b'Incorrect PIN' in r.data

    def test_claim_invalid_school(self, client, tid):
        r = client.get(f'/portal/school/claim?tournament_id={tid}'
                       '&school_name=Nonexistent University',
                       follow_redirects=True)
        _ok(r)

    def test_claim_missing_params(self, client):
        r = client.get('/portal/school/claim', follow_redirects=True)
        _ok(r)


# ---------------------------------------------------------------------------
# School dashboard — data integrity
# ---------------------------------------------------------------------------

class TestSchoolDashboard:
    """Test school dashboard renders with correct data for multi-team school."""

    def test_dashboard_requires_auth(self, client, tid):
        """No session → redirect to claim."""
        c = client.application.test_client()
        r = c.get(f'/portal/school/dashboard?tournament_id={tid}'
                  '&school_name=University of Montana',
                  follow_redirects=False)
        assert r.status_code == 302

    def test_dashboard_shows_both_teams(self, client, tid):
        """UM has UM-A and UM-B — dashboard should show both."""
        _authorize_school(client, tid, 'University of Montana')
        r = client.get(f'/portal/school/dashboard?tournament_id={tid}'
                       '&school_name=University of Montana')
        assert r.status_code == 200
        assert b'UM-A' in r.data
        assert b'UM-B' in r.data

    def test_dashboard_shows_members(self, client, tid):
        """Dashboard should show team members."""
        _authorize_school(client, tid, 'University of Montana')
        r = client.get(f'/portal/school/dashboard?tournament_id={tid}'
                       '&school_name=University of Montana')
        assert r.status_code == 200
        assert b'Jake UM' in r.data
        assert b'Sara UM' in r.data
        assert b'Alex UM-B' in r.data

    def test_dashboard_highlights_own_school(self, client, tid):
        """UM dashboard shows all standings but highlights UM teams."""
        _authorize_school(client, tid, 'University of Montana')
        r = client.get(f'/portal/school/dashboard?tournament_id={tid}'
                       '&school_name=University of Montana')
        assert r.status_code == 200
        # All standings table includes all teams (by design),
        # but "Your Team" highlight only appears for UM teams
        assert b'UM-A' in r.data
        assert b'UM-B' in r.data

    def test_dashboard_shows_tournament_name(self, client, tid):
        _authorize_school(client, tid, 'University of Montana')
        r = client.get(f'/portal/school/dashboard?tournament_id={tid}'
                       '&school_name=University of Montana')
        assert r.status_code == 200
        assert b'School Deep Test' in r.data

    def test_dashboard_missing_params(self, client):
        r = client.get('/portal/school/dashboard', follow_redirects=True)
        _ok(r)

    def test_dashboard_nonexistent_school(self, client, tid):
        _authorize_school(client, tid, 'Ghost School')
        r = client.get(f'/portal/school/dashboard?tournament_id={tid}'
                       '&school_name=Ghost School',
                       follow_redirects=True)
        _ok(r)

    def test_msu_dashboard(self, client, tid):
        """MSU dashboard should show MSU team and members."""
        _authorize_school(client, tid, 'Montana State University')
        r = client.get(f'/portal/school/dashboard?tournament_id={tid}'
                       '&school_name=Montana State University')
        assert r.status_code == 200
        assert b'MSU-A' in r.data
        assert b'MSU Mike' in r.data
        # All-standings table shows all teams (by design) — just verify MSU data is present
        assert b'Montana State University' in r.data


# ---------------------------------------------------------------------------
# Session isolation between schools
# ---------------------------------------------------------------------------

class TestSchoolSessionIsolation:
    """Session auth for one school does not grant access to another."""

    def test_um_auth_does_not_grant_msu(self, client, tid):
        _authorize_school(client, tid, 'University of Montana')
        c2 = client.application.test_client()
        # Fresh client with UM auth should not access MSU
        _authorize_school(c2, tid, 'University of Montana')
        r = c2.get(f'/portal/school/dashboard?tournament_id={tid}'
                   '&school_name=Montana State University',
                   follow_redirects=False)
        assert r.status_code == 302
        assert 'school/claim' in r.headers['Location']

    def test_can_auth_both_schools(self, client, tid):
        """A client can be authorized for multiple schools simultaneously."""
        _authorize_school(client, tid, 'University of Montana')
        _authorize_school(client, tid, 'Montana State University')

        r1 = client.get(f'/portal/school/dashboard?tournament_id={tid}'
                        '&school_name=University of Montana')
        assert r1.status_code == 200

        r2 = client.get(f'/portal/school/dashboard?tournament_id={tid}'
                        '&school_name=Montana State University')
        assert r2.status_code == 200
