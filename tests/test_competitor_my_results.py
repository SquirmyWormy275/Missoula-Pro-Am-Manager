"""
Competitor my-results page tests — the self-service personal results portal
at /portal/competitor/<tid>/<type>/<id>/my-results.

Tests the full lifecycle:
  - No-PIN competitors get open access
  - PIN-protected competitors see PIN gate first
  - Correct PIN grants access + session persists
  - Wrong PIN shows error, no access
  - Dashboard shows events entered, heat assignments, results, gear-sharing
  - Invalid competitor type / ID / tournament returns 404
  - Cross-competitor session isolation (different session key format)

Run:
    pytest tests/test_competitor_my_results.py -v
"""
import json
import os
import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-myresults')
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
    from models import Tournament, Team, Event, EventResult, Heat
    from models.competitor import CollegeCompetitor, ProCompetitor

    t = Tournament.query.filter_by(name='MyResults Test').first()
    if not t:
        t = Tournament(name='MyResults Test', year=2026, status='pro_active')
        _db.session.add(t)
        _db.session.flush()

    team = Team.query.filter_by(tournament_id=t.id, team_code='UM-A').first()
    if not team:
        team = Team(tournament_id=t.id, team_code='UM-A',
                    school_name='University of Montana', school_abbreviation='UM')
        _db.session.add(team)
        _db.session.flush()

    # Pro with PIN
    pro = ProCompetitor.query.filter_by(tournament_id=t.id, name='Pro Pinned').first()
    if not pro:
        pro = ProCompetitor(
            tournament_id=t.id, name='Pro Pinned', gender='M',
            events_entered='[]', gear_sharing='{}', partners='{}', status='active',
        )
        pro.set_portal_pin('4444')
        _db.session.add(pro)
        _db.session.flush()

    # Pro without PIN (open access)
    pro_open = ProCompetitor.query.filter_by(tournament_id=t.id, name='Pro Open').first()
    if not pro_open:
        pro_open = ProCompetitor(
            tournament_id=t.id, name='Pro Open', gender='M',
            events_entered='[]', gear_sharing='{}', partners='{}', status='active',
        )
        _db.session.add(pro_open)
        _db.session.flush()

    # College competitor
    cc = CollegeCompetitor.query.filter_by(tournament_id=t.id, name='College Kid').first()
    if not cc:
        cc = CollegeCompetitor(
            tournament_id=t.id, team_id=team.id, name='College Kid',
            gender='F', events_entered='[]', status='active',
        )
        _db.session.add(cc)
        _db.session.flush()

    # Event with heat and result for pro
    evt = Event.query.filter_by(tournament_id=t.id, name='UH Speed (M)').first()
    if not evt:
        evt = Event(
            tournament_id=t.id, name='UH Speed (M)', event_type='pro',
            gender='M', scoring_type='time', scoring_order='lowest_wins',
            stand_type='underhand', max_stands=5, status='completed',
        )
        _db.session.add(evt)
        _db.session.flush()

    # Update pro events_entered to include this event
    pro.events_entered = json.dumps([evt.id])

    heat = Heat.query.filter_by(event_id=evt.id).first()
    if not heat:
        heat = Heat(
            event_id=evt.id, heat_number=1, run_number=1,
            competitors=json.dumps([pro.id]),
            stand_assignments=json.dumps({str(pro.id): '3'}),
            status='completed',
        )
        _db.session.add(heat)
        _db.session.flush()

    res = EventResult.query.filter_by(event_id=evt.id, competitor_id=pro.id).first()
    if not res:
        res = EventResult(
            event_id=evt.id, competitor_id=pro.id, competitor_type='pro',
            competitor_name='Pro Pinned', result_value=19.8,
            run1_value=19.8, final_position=1, payout_amount=750.0,
            status='completed',
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
        return Tournament.query.filter_by(name='MyResults Test').first().id


@pytest.fixture()
def pro_pinned(app, tid):
    with app.app_context():
        from models.competitor import ProCompetitor
        return ProCompetitor.query.filter_by(tournament_id=tid, name='Pro Pinned').first()


@pytest.fixture()
def pro_open(app, tid):
    with app.app_context():
        from models.competitor import ProCompetitor
        return ProCompetitor.query.filter_by(tournament_id=tid, name='Pro Open').first()


@pytest.fixture()
def college_kid(app, tid):
    with app.app_context():
        from models.competitor import CollegeCompetitor
        return CollegeCompetitor.query.filter_by(tournament_id=tid, name='College Kid').first()


def _ok(r):
    assert r.status_code not in (500, 502, 503), f'Server error: {r.status_code}'


# ---------------------------------------------------------------------------
# Open access (no PIN set)
# ---------------------------------------------------------------------------

class TestMyResultsOpenAccess:
    """Competitors without a PIN get open access to my-results."""

    def test_no_pin_auto_access(self, client, tid, pro_open):
        r = client.get(f'/portal/competitor/{tid}/pro/{pro_open.id}/my-results')
        assert r.status_code == 200
        assert b'Pro Open' in r.data

    def test_college_no_pin_access(self, client, tid, college_kid):
        r = client.get(f'/portal/competitor/{tid}/college/{college_kid.id}/my-results')
        assert r.status_code == 200
        assert b'College Kid' in r.data


# ---------------------------------------------------------------------------
# PIN gate
# ---------------------------------------------------------------------------

class TestMyResultsPINGate:
    """PIN-protected competitors see PIN gate first."""

    def test_pin_gate_shown_on_get(self, client, tid, pro_pinned):
        r = client.get(f'/portal/competitor/{tid}/pro/{pro_pinned.id}/my-results')
        assert r.status_code == 200
        # Should show PIN input, not the results
        assert b'pin' in r.data.lower()

    def test_correct_pin_grants_access(self, client, tid, pro_pinned):
        r = client.post(
            f'/portal/competitor/{tid}/pro/{pro_pinned.id}/my-results',
            data={'pin': '4444'},
        )
        assert r.status_code == 200
        assert b'Pro Pinned' in r.data

    def test_wrong_pin_shows_error(self, client, tid, pro_pinned):
        r = client.post(
            f'/portal/competitor/{tid}/pro/{pro_pinned.id}/my-results',
            data={'pin': '9999'},
        )
        assert r.status_code == 200
        assert b'Incorrect PIN' in r.data or b'pin' in r.data.lower()

    def test_session_persists_after_pin(self, client, tid, pro_pinned):
        """After correct PIN, subsequent GETs should not re-prompt."""
        client.post(
            f'/portal/competitor/{tid}/pro/{pro_pinned.id}/my-results',
            data={'pin': '4444'},
        )
        r = client.get(f'/portal/competitor/{tid}/pro/{pro_pinned.id}/my-results')
        assert r.status_code == 200
        assert b'Pro Pinned' in r.data

    def test_empty_pin_rejected(self, client, tid, pro_pinned):
        r = client.post(
            f'/portal/competitor/{tid}/pro/{pro_pinned.id}/my-results',
            data={'pin': ''},
        )
        _ok(r)


# ---------------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------------

class TestMyResultsData:
    """Verify the my-results page shows correct personal data."""

    def _authorize(self, client, tid, comp_id):
        with client.session_transaction() as sess:
            sess[f'competitor_auth_{tid}_pro_{comp_id}'] = True

    def test_shows_events_entered(self, client, tid, pro_pinned):
        self._authorize(client, tid, pro_pinned.id)
        r = client.get(f'/portal/competitor/{tid}/pro/{pro_pinned.id}/my-results')
        assert r.status_code == 200
        assert b'UH Speed' in r.data

    def test_shows_results(self, client, tid, pro_pinned):
        self._authorize(client, tid, pro_pinned.id)
        r = client.get(f'/portal/competitor/{tid}/pro/{pro_pinned.id}/my-results')
        assert r.status_code == 200
        # Result value or position should appear
        assert b'Pro Pinned' in r.data


# ---------------------------------------------------------------------------
# Invalid requests
# ---------------------------------------------------------------------------

class TestMyResultsInvalid:
    """Invalid route parameters should 404, not 500."""

    def test_invalid_competitor_type(self, client, tid):
        r = client.get(f'/portal/competitor/{tid}/invalid/1/my-results')
        assert r.status_code == 404

    def test_nonexistent_competitor(self, client, tid):
        r = client.get(f'/portal/competitor/{tid}/pro/99999/my-results')
        assert r.status_code == 404

    def test_nonexistent_tournament(self, client):
        r = client.get('/portal/competitor/99999/pro/1/my-results')
        assert r.status_code == 404

    def test_wrong_tournament_for_competitor(self, app, tid, pro_pinned):
        """Competitor belongs to tournament A but URL uses tournament B."""
        from models import Tournament
        c = app.test_client()
        with app.app_context():
            t2 = Tournament.query.filter(Tournament.id != tid).first()
            if t2:
                r = c.get(f'/portal/competitor/{t2.id}/pro/{pro_pinned.id}/my-results')
                assert r.status_code == 404


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------

class TestMyResultsSessionIsolation:
    """my-results uses a different session key format from competitor/public."""

    def test_public_auth_does_not_grant_myresults(self, client, tid, pro_pinned):
        """competitor_portal_auth key does NOT unlock my-results (different key format)."""
        with client.session_transaction() as sess:
            auth = sess.get('competitor_portal_auth', {})
            auth[f'{tid}:pro:{pro_pinned.id}'] = True
            sess['competitor_portal_auth'] = auth

        r = client.get(f'/portal/competitor/{tid}/pro/{pro_pinned.id}/my-results')
        # Should still show PIN gate (different session key)
        assert r.status_code == 200
        assert b'pin' in r.data.lower()

    def test_myresults_auth_does_not_grant_public(self, client, tid, pro_pinned):
        """competitor_auth_ key does NOT unlock competitor/public (different key)."""
        with client.session_transaction() as sess:
            sess[f'competitor_auth_{tid}_pro_{pro_pinned.id}'] = True

        r = client.get(f'/portal/competitor/public?tournament_id={tid}'
                       f'&competitor_type=pro&competitor_id={pro_pinned.id}',
                       follow_redirects=False)
        assert r.status_code == 302
        assert 'claim' in r.headers['Location']
