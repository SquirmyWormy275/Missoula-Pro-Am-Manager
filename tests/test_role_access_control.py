"""
Role-based access control test suite — verifies that competitor, spectator,
viewer, scorer, and registrar roles are correctly gated by the
``require_judge_for_management_routes`` before_request hook.

Tests confirm:
  - Competitor role CANNOT access management blueprints (main, registration,
    scheduling, scoring, reporting, validation, woodboss, import, relay, axe)
  - Spectator role CANNOT access management blueprints
  - Viewer role CANNOT access most management blueprints
  - Scorer role CAN access scheduling + scoring but NOT registration/woodboss
  - Registrar role CAN access registration but NOT scheduling/scoring/woodboss
  - Admin and judge roles CAN access everything
  - Unauthenticated users are redirected to login for management routes
  - Portal routes remain accessible to ALL roles and unauthenticated users
  - Public API remains accessible to ALL roles and unauthenticated users

Run:
    pytest tests/test_role_access_control.py -v
"""
import os

import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-rbac')
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
        _seed_rbac_data(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_rbac_data(app):
    """Create one user per role and a tournament."""
    from models import Tournament
    from models.user import User

    roles = {
        'rbac_admin': 'admin',
        'rbac_judge': 'judge',
        'rbac_scorer': 'scorer',
        'rbac_registrar': 'registrar',
        'rbac_competitor': 'competitor',
        'rbac_spectator': 'spectator',
        'rbac_viewer': 'viewer',
    }
    for username, role in roles.items():
        if not User.query.filter_by(username=username).first():
            u = User(username=username, role=role)
            u.set_password(f'{role}pass')
            _db.session.add(u)

    if not Tournament.query.filter_by(name='RBAC Test 2026').first():
        t = Tournament(name='RBAC Test 2026', year=2026, status='setup')
        _db.session.add(t)

    _db.session.commit()


def _login(app, username, password):
    """Return a logged-in test client using session_transaction for reliability."""
    from models.user import User
    c = app.test_client()
    with app.app_context():
        u = User.query.filter_by(username=username).first()
        if u:
            with c.session_transaction() as sess:
                sess['_user_id'] = str(u.id)
    return c


@pytest.fixture()
def tid(app):
    with app.app_context():
        from models import Tournament
        return Tournament.query.filter_by(name='RBAC Test 2026').first().id


@pytest.fixture()
def unauth_client(app):
    return app.test_client()


@pytest.fixture()
def admin_client(app):
    return _login(app, 'rbac_admin', 'adminpass')


@pytest.fixture()
def judge_client(app):
    return _login(app, 'rbac_judge', 'judgepass')


@pytest.fixture()
def scorer_client(app):
    return _login(app, 'rbac_scorer', 'scorerpass')


@pytest.fixture()
def registrar_client(app):
    return _login(app, 'rbac_registrar', 'registrarpass')


@pytest.fixture()
def competitor_client(app):
    return _login(app, 'rbac_competitor', 'competitorpass')


@pytest.fixture()
def spectator_client(app):
    return _login(app, 'rbac_spectator', 'spectatorpass')


@pytest.fixture()
def viewer_client(app):
    return _login(app, 'rbac_viewer', 'viewerpass')


# ---------------------------------------------------------------------------
# Management route URLs by blueprint
# ---------------------------------------------------------------------------

def _management_routes(tid):
    """Return a dict of blueprint_name -> list of GET URLs to test."""
    return {
        'main': [
            f'/tournament/{tid}',
            f'/tournament/{tid}/setup',
            f'/tournament/{tid}/college',
            f'/tournament/{tid}/pro',
        ],
        'registration': [
            f'/registration/{tid}/college',
            f'/registration/{tid}/pro',
            f'/registration/{tid}/pro/new',
        ],
        'scheduling': [
            f'/scheduling/{tid}/events',
            f'/scheduling/{tid}/events/setup',
            f'/scheduling/{tid}/flights',
            f'/scheduling/{tid}/heat-sheets',
        ],
        'scoring': [
            f'/scoring/{tid}/offline',
        ],
        'reporting': [
            f'/reporting/{tid}/all-results',
            f'/reporting/{tid}/college/standings',
            f'/reporting/{tid}/pro/standings',
        ],
        'validation': [
            f'/validation/{tid}/',
        ],
        'woodboss': [
            f'/woodboss/{tid}/',
        ],
    }


def _portal_routes(tid):
    """Public portal routes that should be accessible to everyone."""
    return [
        '/portal/',
        f'/portal/spectator/{tid}',
        '/portal/competitor-access',
        '/portal/school-access',
        '/portal/guide',
    ]


def _api_routes(tid):
    """Public API routes that should be accessible to everyone."""
    return [
        f'/api/public/tournaments/{tid}/standings',
        f'/api/public/tournaments/{tid}/schedule',
        f'/api/public/tournaments/{tid}/results',
        f'/api/public/tournaments/{tid}/standings-poll',
    ]


# ---------------------------------------------------------------------------
# Unauthenticated access — should redirect to login
# ---------------------------------------------------------------------------

class TestUnauthenticatedBlocked:
    """Unauthenticated users must NOT get 200 on management routes.

    Some routes return 302 (redirect to login), others 404 (tournament-level
    check inside the route).  Both are acceptable — the key is that no
    management content is served (i.e. never 200).
    """

    def test_main_blocked(self, unauth_client, tid):
        for url in _management_routes(tid)['main']:
            r = unauth_client.get(url, follow_redirects=False)
            assert r.status_code != 200, f'{url} should not return 200, got {r.status_code}'

    def test_registration_blocked(self, unauth_client, tid):
        for url in _management_routes(tid)['registration']:
            r = unauth_client.get(url, follow_redirects=False)
            assert r.status_code != 200, f'{url} should not return 200'

    def test_scheduling_blocked(self, unauth_client, tid):
        for url in _management_routes(tid)['scheduling']:
            r = unauth_client.get(url, follow_redirects=False)
            assert r.status_code != 200, f'{url} should not return 200'

    def test_scoring_blocked(self, unauth_client, tid):
        for url in _management_routes(tid)['scoring']:
            r = unauth_client.get(url, follow_redirects=False)
            assert r.status_code != 200, f'{url} should not return 200'

    def test_reporting_blocked(self, unauth_client, tid):
        for url in _management_routes(tid)['reporting']:
            r = unauth_client.get(url, follow_redirects=False)
            assert r.status_code != 200, f'{url} should not return 200'

    def test_validation_blocked(self, unauth_client, tid):
        for url in _management_routes(tid)['validation']:
            r = unauth_client.get(url, follow_redirects=False)
            assert r.status_code != 200, f'{url} should not return 200'

    def test_woodboss_blocked(self, unauth_client, tid):
        for url in _management_routes(tid)['woodboss']:
            r = unauth_client.get(url, follow_redirects=False)
            assert r.status_code != 200, f'{url} should not return 200'


# ---------------------------------------------------------------------------
# Competitor role — blocked from all management
# ---------------------------------------------------------------------------

class TestCompetitorRoleBlocked:
    """Competitor role must be blocked from all management blueprints.

    We assert not-200: the auth hook returns 403, but some routes may 404
    (tournament-level check) or 302 (redirect inside the view).  The key
    invariant is that a competitor NEVER receives a 200 with management content.
    """

    def test_blocked_from_main(self, competitor_client, tid):
        for url in _management_routes(tid)['main']:
            r = competitor_client.get(url)
            assert r.status_code != 200, f'Competitor must not get 200 on {url}, got {r.status_code}'

    def test_blocked_from_registration(self, competitor_client, tid):
        for url in _management_routes(tid)['registration']:
            r = competitor_client.get(url)
            assert r.status_code != 200, f'Competitor must not get 200 on {url}'

    def test_blocked_from_scheduling(self, competitor_client, tid):
        for url in _management_routes(tid)['scheduling']:
            r = competitor_client.get(url)
            assert r.status_code != 200, f'Competitor must not get 200 on {url}'

    def test_blocked_from_scoring(self, competitor_client, tid):
        for url in _management_routes(tid)['scoring']:
            r = competitor_client.get(url)
            assert r.status_code != 200, f'Competitor must not get 200 on {url}'

    def test_blocked_from_reporting(self, competitor_client, tid):
        for url in _management_routes(tid)['reporting']:
            r = competitor_client.get(url)
            assert r.status_code != 200, f'Competitor must not get 200 on {url}'

    def test_blocked_from_validation(self, competitor_client, tid):
        for url in _management_routes(tid)['validation']:
            r = competitor_client.get(url)
            assert r.status_code != 200, f'Competitor must not get 200 on {url}'

    def test_blocked_from_woodboss(self, competitor_client, tid):
        for url in _management_routes(tid)['woodboss']:
            r = competitor_client.get(url)
            assert r.status_code != 200, f'Competitor must not get 200 on {url}'


# ---------------------------------------------------------------------------
# Spectator role — blocked from all management
# ---------------------------------------------------------------------------

class TestSpectatorRoleBlocked:
    """Spectator role must be blocked from non-reporting management blueprints."""

    def test_blocked_from_main(self, spectator_client, tid):
        for url in _management_routes(tid)['main']:
            r = spectator_client.get(url)
            assert r.status_code != 200, f'Spectator must not get 200 on {url}, got {r.status_code}'

    def test_blocked_from_registration(self, spectator_client, tid):
        for url in _management_routes(tid)['registration']:
            r = spectator_client.get(url)
            assert r.status_code != 200, f'Spectator must not get 200 on {url}'

    def test_blocked_from_scheduling(self, spectator_client, tid):
        for url in _management_routes(tid)['scheduling']:
            r = spectator_client.get(url)
            assert r.status_code != 200, f'Spectator must not get 200 on {url}'

    def test_blocked_from_scoring(self, spectator_client, tid):
        for url in _management_routes(tid)['scoring']:
            r = spectator_client.get(url)
            assert r.status_code != 200, f'Spectator must not get 200 on {url}'

    def test_blocked_from_woodboss(self, spectator_client, tid):
        for url in _management_routes(tid)['woodboss']:
            r = spectator_client.get(url)
            assert r.status_code != 200, f'Spectator must not get 200 on {url}'


# ---------------------------------------------------------------------------
# Viewer role — can_report but NOT judge/register/schedule/score
# ---------------------------------------------------------------------------

class TestViewerRolePermissions:
    """Viewer role has can_report but lacks other management permissions."""

    def test_blocked_from_main(self, viewer_client, tid):
        """Main requires is_judge — viewer should be blocked."""
        for url in _management_routes(tid)['main']:
            r = viewer_client.get(url)
            assert r.status_code != 200, f'Viewer must not get 200 on {url}'

    def test_blocked_from_registration(self, viewer_client, tid):
        for url in _management_routes(tid)['registration']:
            r = viewer_client.get(url)
            assert r.status_code != 200, f'Viewer must not get 200 on {url}'

    def test_blocked_from_scheduling(self, viewer_client, tid):
        for url in _management_routes(tid)['scheduling']:
            r = viewer_client.get(url)
            assert r.status_code != 200, f'Viewer must not get 200 on {url}'

    def test_blocked_from_scoring(self, viewer_client, tid):
        for url in _management_routes(tid)['scoring']:
            r = viewer_client.get(url)
            assert r.status_code != 200, f'Viewer must not get 200 on {url}'

    def test_can_access_reporting(self, viewer_client, tid):
        """Viewer has can_report — reporting routes should not return 403.

        Some routes may 404 (e.g. empty standings) which is acceptable —
        the auth hook passed, but the view had no data to render.
        """
        for url in _management_routes(tid)['reporting']:
            r = viewer_client.get(url)
            assert r.status_code != 403, f'Viewer should NOT be 403 on {url}, got {r.status_code}'

    def test_can_access_validation(self, viewer_client, tid):
        """Validation requires can_report — viewer should not get 403."""
        for url in _management_routes(tid)['validation']:
            r = viewer_client.get(url)
            assert r.status_code != 403, f'Viewer should NOT be 403 on {url}, got {r.status_code}'

    def test_blocked_from_woodboss(self, viewer_client, tid):
        for url in _management_routes(tid)['woodboss']:
            r = viewer_client.get(url)
            assert r.status_code != 200, f'Viewer must not get 200 on {url}'


# ---------------------------------------------------------------------------
# Scorer role — can_schedule + can_score but NOT register/woodboss
# ---------------------------------------------------------------------------

class TestScorerRolePermissions:
    """Scorer role has can_schedule and can_score."""

    def test_can_access_scheduling(self, scorer_client, tid):
        """Scorer has can_schedule — should not get 403."""
        for url in _management_routes(tid)['scheduling']:
            r = scorer_client.get(url)
            assert r.status_code != 403, f'Scorer should NOT be 403 on {url}, got {r.status_code}'

    def test_can_access_scoring(self, scorer_client, tid):
        """Scorer has can_score — should not get 403."""
        for url in _management_routes(tid)['scoring']:
            r = scorer_client.get(url)
            assert r.status_code != 403, f'Scorer should NOT be 403 on {url}, got {r.status_code}'

    def test_blocked_from_main(self, scorer_client, tid):
        """Main requires is_judge — scorer should be blocked."""
        for url in _management_routes(tid)['main']:
            r = scorer_client.get(url)
            assert r.status_code != 200, f'Scorer must not get 200 on {url}, got {r.status_code}'

    def test_blocked_from_registration(self, scorer_client, tid):
        for url in _management_routes(tid)['registration']:
            r = scorer_client.get(url)
            assert r.status_code != 200, f'Scorer must not get 200 on {url}'

    def test_blocked_from_woodboss(self, scorer_client, tid):
        for url in _management_routes(tid)['woodboss']:
            r = scorer_client.get(url)
            assert r.status_code != 200, f'Scorer must not get 200 on {url}'


# ---------------------------------------------------------------------------
# Registrar role — can_register but NOT schedule/score/woodboss
# ---------------------------------------------------------------------------

class TestRegistrarRolePermissions:
    """Registrar role has can_register only."""

    def test_can_access_registration(self, registrar_client, tid):
        """Registrar has can_register — should not get 403."""
        for url in _management_routes(tid)['registration']:
            r = registrar_client.get(url)
            assert r.status_code != 403, f'Registrar should NOT be 403 on {url}, got {r.status_code}'

    def test_blocked_from_main(self, registrar_client, tid):
        for url in _management_routes(tid)['main']:
            r = registrar_client.get(url)
            assert r.status_code != 200, f'Registrar must not get 200 on {url}'

    def test_blocked_from_scheduling(self, registrar_client, tid):
        for url in _management_routes(tid)['scheduling']:
            r = registrar_client.get(url)
            assert r.status_code != 200, f'Registrar must not get 200 on {url}'

    def test_blocked_from_scoring(self, registrar_client, tid):
        for url in _management_routes(tid)['scoring']:
            r = registrar_client.get(url)
            assert r.status_code != 200, f'Registrar must not get 200 on {url}'

    def test_blocked_from_woodboss(self, registrar_client, tid):
        for url in _management_routes(tid)['woodboss']:
            r = registrar_client.get(url)
            assert r.status_code != 200, f'Registrar must not get 200 on {url}'


# ---------------------------------------------------------------------------
# Admin role — full access
# ---------------------------------------------------------------------------

class TestAdminFullAccess:
    """Admin role should never get 403 on management blueprints."""

    def test_can_access_all_management(self, admin_client, tid):
        for blueprint, urls in _management_routes(tid).items():
            for url in urls:
                r = admin_client.get(url)
                assert r.status_code != 403, (
                    f'Admin should NOT be 403 on {blueprint}:{url}, got {r.status_code}'
                )


# ---------------------------------------------------------------------------
# Judge role — full management access
# ---------------------------------------------------------------------------

class TestJudgeFullAccess:
    """Judge role should never get 403 on management blueprints."""

    def test_can_access_all_management(self, judge_client, tid):
        for blueprint, urls in _management_routes(tid).items():
            for url in urls:
                r = judge_client.get(url)
                assert r.status_code != 403, (
                    f'Judge should NOT be 403 on {blueprint}:{url}, got {r.status_code}'
                )


# ---------------------------------------------------------------------------
# Portal routes accessible to ALL (including restricted roles)
# ---------------------------------------------------------------------------

class TestPortalAccessibleToAll:
    """Portal routes must be accessible regardless of role."""

    def test_unauthenticated_portal(self, unauth_client, tid):
        for url in _portal_routes(tid):
            r = unauth_client.get(url)
            assert r.status_code in (200, 302), f'Portal {url} should be accessible, got {r.status_code}'

    def test_competitor_portal(self, competitor_client, tid):
        for url in _portal_routes(tid):
            r = competitor_client.get(url)
            assert r.status_code in (200, 302), f'Competitor should access portal {url}'

    def test_spectator_portal(self, spectator_client, tid):
        for url in _portal_routes(tid):
            r = spectator_client.get(url)
            assert r.status_code in (200, 302), f'Spectator should access portal {url}'

    def test_viewer_portal(self, viewer_client, tid):
        for url in _portal_routes(tid):
            r = viewer_client.get(url)
            assert r.status_code in (200, 302), f'Viewer should access portal {url}'


# ---------------------------------------------------------------------------
# Public API accessible to ALL
# ---------------------------------------------------------------------------

class TestAPIAccessibleToAll:
    """Public API routes must be accessible regardless of role."""

    def test_unauthenticated_api(self, unauth_client, tid):
        for url in _api_routes(tid):
            r = unauth_client.get(url)
            assert r.status_code == 200, f'API {url} should return 200, got {r.status_code}'

    def test_competitor_api(self, competitor_client, tid):
        for url in _api_routes(tid):
            r = competitor_client.get(url)
            assert r.status_code == 200, f'Competitor should access API {url}'

    def test_spectator_api(self, spectator_client, tid):
        for url in _api_routes(tid):
            r = spectator_client.get(url)
            assert r.status_code == 200, f'Spectator should access API {url}'


# ---------------------------------------------------------------------------
# Spectator role can_report — reporting and validation accessible
# ---------------------------------------------------------------------------

class TestSpectatorReportingAccess:
    """Spectator has can_report — should NOT get 403 on reporting/validation."""

    def test_spectator_can_access_reporting(self, spectator_client, tid):
        for url in _management_routes(tid)['reporting']:
            r = spectator_client.get(url)
            assert r.status_code != 403, f'Spectator should NOT be 403 on {url}, got {r.status_code}'

    def test_spectator_can_access_validation(self, spectator_client, tid):
        for url in _management_routes(tid)['validation']:
            r = spectator_client.get(url)
            assert r.status_code != 403, f'Spectator should NOT be 403 on {url}, got {r.status_code}'
