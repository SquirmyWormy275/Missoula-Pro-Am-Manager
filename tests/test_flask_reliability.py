"""
Flask infrastructure reliability tests — guards against recurring
Flask-level issues that have caused production breakage.

Uses conftest's shared session-scoped app and per-test db_session fixtures.

Run:
    pytest tests/test_flask_reliability.py -v
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Fixtures — uses conftest's app + db_session
# ---------------------------------------------------------------------------

@pytest.fixture()
def seed(app, db_session):
    """Seed users + tournament — only used by tests that need DB data."""
    from models.user import User
    from models import Tournament, Team

    roles = {
        'admin_user': 'admin',
        'judge_user': 'judge',
        'scorer_user': 'scorer',
        'registrar_user': 'registrar',
        'spectator_user': 'spectator',
        'viewer_user': 'viewer',
    }
    for uname, role in roles.items():
        u = User(username=uname, role=role)
        u.set_password(uname)
        db_session.add(u)

    t = Tournament(name='Reliability 2026', year=2026, status='setup')
    db_session.add(t)
    db_session.flush()

    team = Team(tournament_id=t.id, team_code='UM-A',
                school_name='University of Montana',
                school_abbreviation='UM', status='active')
    db_session.add(team)
    db_session.flush()
    return t


def _login(client, username):
    from models.user import User
    user = User.query.filter_by(username=username).first()
    assert user is not None, f'Test user {username!r} not seeded — add seed fixture'
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)


def _tid():
    from models import Tournament
    t = Tournament.query.first()
    assert t is not None
    return t.id


# ===========================================================================
# 1. APP FACTORY
# ===========================================================================

class TestAppFactory:
    def test_create_app_returns_flask(self, app):
        from flask import Flask
        assert isinstance(app, Flask)

    def test_all_expected_blueprints_registered(self, app):
        expected = {'main', 'registration', 'scheduling', 'scoring', 'reporting',
                    'proam_relay', 'partnered_axe', 'validation', 'import_pro',
                    'woodboss', 'woodboss_public', 'strathmark'}
        from app import HAS_FLASK_LOGIN
        if HAS_FLASK_LOGIN:
            expected |= {'auth', 'portal', 'api', 'api_v1'}
        assert not (expected - set(app.blueprints.keys()))

    def test_no_stray_app_routes(self, app):
        app_rules = [r.rule for r in app.url_map.iter_rules()
                     if r.endpoint and '.' not in r.endpoint
                     and r.rule != '/static/<path:filename>']
        for rule in app_rules:
            assert rule in ('/sw.js',), f'Unexpected: {rule}'

    def test_csrf_initialized(self, app):
        from app import csrf
        assert csrf is not None

    def test_upload_folder_exists(self, app):
        assert os.path.isdir(app.config['UPLOAD_FOLDER'])


# ===========================================================================
# 2. CONFIGURATION
# ===========================================================================

class TestConfiguration:
    def test_default_sqlite(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('DATABASE_URL', None)
            from config import _normalized_database_url
            assert _normalized_database_url().startswith('sqlite')

    def test_postgres_normalized(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'postgres://u:p@h/d'}):
            from config import _normalized_database_url
            assert _normalized_database_url().startswith('postgresql://')

    def test_postgresql_unchanged(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://u:p@h/d'}):
            from config import _normalized_database_url
            assert _normalized_database_url().startswith('postgresql://')

    def test_dev_config_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('FLASK_ENV', None)
            os.environ.pop('PRODUCTION', None)
            from config import get_config, DevelopmentConfig
            assert get_config() is DevelopmentConfig

    def test_production_config(self):
        with patch.dict(os.environ, {'PRODUCTION': '1'}):
            from config import get_config, ProductionConfig
            assert get_config() is ProductionConfig

    def test_weak_secret_rejected(self):
        from config import validate_runtime
        with pytest.raises(RuntimeError):
            validate_runtime({'ENV_NAME': 'production', 'SECRET_KEY': 'changeme'})

    def test_short_secret_rejected(self):
        from config import validate_runtime
        with pytest.raises(RuntimeError):
            validate_runtime({'ENV_NAME': 'production', 'SECRET_KEY': 'short'})

    def test_strong_secret_accepted(self):
        from config import validate_runtime
        validate_runtime({'ENV_NAME': 'production', 'SECRET_KEY': 'a-very-strong-random-secret-key-here'})

    def test_dev_skips_validation(self):
        from config import validate_runtime
        validate_runtime({'ENV_NAME': 'development', 'SECRET_KEY': 'dev'})

    def test_pool_pre_ping(self, app):
        assert app.config.get('SQLALCHEMY_ENGINE_OPTIONS', {}).get('pool_pre_ping') is True


# ===========================================================================
# 3. CSRF
# ===========================================================================

class TestCSRFProtection:
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    def test_extension_registered(self, app):
        from app import csrf
        assert csrf is not None

    def test_get_not_blocked(self, app):
        c = app.test_client()
        _login(c, 'admin_user')
        assert c.get(f'/tournament/{_tid()}').status_code in (200, 302)

    def test_public_get_ok(self, app):
        assert app.test_client().get('/health').status_code == 200


# ===========================================================================
# 4. AUTH HOOKS
# ===========================================================================

class TestAuthHooks:
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    def test_unauth_redirects(self, app):
        r = app.test_client().get(f'/tournament/{_tid()}', follow_redirects=False)
        assert r.status_code in (302, 401)

    def test_spectator_403(self, app):
        c = app.test_client()
        _login(c, 'spectator_user')
        assert c.get(f'/tournament/{_tid()}').status_code == 403

    def test_judge_access(self, app):
        c = app.test_client()
        _login(c, 'admin_user')
        assert c.get(f'/tournament/{_tid()}').status_code == 200

    def test_scorer_not_blocked(self, app):
        c = app.test_client()
        _login(c, 'scorer_user')
        assert c.get(f'/scoring/{_tid()}/event/9999/results').status_code != 403

    def test_registrar_access(self, app):
        c = app.test_client()
        _login(c, 'registrar_user')
        assert c.get(f'/registration/{_tid()}/college').status_code in (200, 302)

    def test_portal_public(self, app):
        r = app.test_client().get(f'/portal/{_tid()}/spectator')
        assert r.status_code not in (403, 500, 502, 503)

    def test_api_public(self, app):
        assert app.test_client().get(f'/api/public/tournaments/{_tid()}/standings').status_code in (200, 404)

    def test_strathmark_public(self, app):
        assert app.test_client().get('/strathmark/status').status_code in (200, 302)

    def test_login_page_public(self, app):
        assert app.test_client().get('/auth/login').status_code == 200

    def test_health_public(self, app):
        assert app.test_client().get('/health').status_code == 200

    def test_static_not_blocked(self, app):
        assert app.test_client().get('/static/js/onboarding.js').status_code != 403


# ===========================================================================
# 5. LOGIN / LOGOUT
# ===========================================================================

class TestLoginLifecycle:
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    def test_session_login(self, app):
        c = app.test_client()
        _login(c, 'admin_user')
        assert c.get(f'/tournament/{_tid()}').status_code == 200

    def test_bogus_id(self, app):
        c = app.test_client()
        with c.session_transaction() as s:
            s['_user_id'] = '99999'
        assert c.get(f'/tournament/{_tid()}', follow_redirects=False).status_code in (302, 401, 403)

    def test_no_session(self, app):
        assert app.test_client().get(f'/tournament/{_tid()}', follow_redirects=False).status_code in (302, 401)

    def test_logout_post_only(self, app):
        c = app.test_client()
        _login(c, 'admin_user')
        assert c.get('/auth/logout', follow_redirects=False).status_code == 405

    def test_bootstrap_locked(self, app):
        assert app.test_client().get('/auth/bootstrap').status_code in (302, 403)

    def test_password_check(self, app):
        from models.user import User
        u = User.query.filter_by(username='admin_user').first()
        assert u and u.check_password('admin_user') and not u.check_password('wrong')


# ===========================================================================
# 6. DATABASE
# ===========================================================================

class TestDatabaseIntegrity:
    def test_foreign_keys_on(self, app):
        from database import db
        if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
            assert db.session.execute(db.text('PRAGMA foreign_keys')).scalar() == 1

    def test_all_tables(self, app):
        from database import db
        tables = set(db.inspect(db.engine).get_table_names())
        expected = {'tournaments', 'teams', 'college_competitors', 'pro_competitors',
                    'events', 'event_results', 'heats', 'heat_assignments', 'flights',
                    'users', 'audit_logs', 'school_captains', 'wood_configs',
                    'pro_event_ranks', 'payout_templates'}
        assert not (expected - tables)


# ===========================================================================
# 7. JSON RESILIENCE
# ===========================================================================

class TestJSONFieldResilience:
    def test_heat_corrupt_competitors(self):
        from models.heat import Heat
        assert Heat(event_id=1, heat_number=1, competitors='{BAD').get_competitors() == []

    def test_heat_corrupt_stands(self):
        from models.heat import Heat
        assert Heat(event_id=1, heat_number=1, stand_assignments='{X').get_stand_assignments() == {}

    def test_event_corrupt_payouts(self):
        from models.event import Event
        assert Event(tournament_id=1, name='T', event_type='pro', scoring_type='time', payouts='X').get_payouts() == {}

    def test_college_corrupt_events(self):
        from models.competitor import CollegeCompetitor
        assert CollegeCompetitor(tournament_id=1, team_id=1, name='X', gender='M', events_entered='{').get_events_entered() == []

    def test_college_corrupt_partners(self):
        from models.competitor import CollegeCompetitor
        assert CollegeCompetitor(tournament_id=1, team_id=1, name='X', gender='M', partners='[').get_partners() == {}

    def test_pro_corrupt_events(self):
        from models.competitor import ProCompetitor
        assert ProCompetitor(tournament_id=1, name='Y', gender='M', events_entered='<>').get_events_entered() == []

    def test_tournament_corrupt_config(self):
        from models.tournament import Tournament
        assert Tournament(name='T', year=2026, schedule_config='[[').get_schedule_config() == {}

    def test_null_fields(self):
        from models.heat import Heat
        from models.event import Event
        assert Heat(event_id=1, heat_number=1, competitors=None).get_competitors() == []
        assert Event(tournament_id=1, name='T', event_type='pro', scoring_type='time', payouts=None).get_payouts() == {}


# ===========================================================================
# 8. MODEL VALIDATION
# ===========================================================================

class TestModelValidation:
    def test_name_truncated(self):
        from models.competitor import CollegeCompetitor, ProCompetitor, MAX_NAME_LENGTH
        assert len(CollegeCompetitor(tournament_id=1, team_id=1, name='A'*200, gender='M').name) == MAX_NAME_LENGTH
        assert len(ProCompetitor(tournament_id=1, name='B'*200, gender='F').name) == MAX_NAME_LENGTH

    def test_short_name_ok(self):
        from models.competitor import CollegeCompetitor
        assert CollegeCompetitor(tournament_id=1, team_id=1, name='Alex', gender='M').name == 'Alex'

    def test_role_properties(self):
        from models.user import User
        assert User(username='a', role='admin').is_judge is True
        assert User(username='j', role='judge').is_judge is True
        assert User(username='s', role='scorer').is_judge is False
        assert User(username='a', role='admin').can_manage_users is True
        assert User(username='j', role='judge').can_manage_users is False

    def test_password_hashing(self):
        from models.user import User
        u = User(username='pw', role='scorer')
        u.set_password('test')
        assert u.check_password('test') and not u.check_password('wrong')

    def test_empty_hash(self):
        from models.user import User
        assert User(username='e', role='scorer', password_hash='').check_password('x') is False


# ===========================================================================
# 9. OPTIMISTIC LOCKING
# ===========================================================================

class TestOptimisticLocking:
    def test_event_result_version_configured(self):
        from models.event import EventResult
        assert EventResult.__mapper__.version_id_col is not None

    def test_heat_version_configured(self):
        from models.heat import Heat
        assert Heat.__mapper__.version_id_col is not None

    def test_defaults(self):
        from models.event import EventResult
        from models.heat import Heat
        # version_id default may be 1 or None before flush depending on SQLAlchemy version
        er = EventResult(event_id=1, competitor_id=1, competitor_type='pro', competitor_name='T')
        assert er.version_id in (1, None)
        h = Heat(event_id=1, heat_number=1)
        assert h.version_id in (1, None)


# ===========================================================================
# 10. HEAT LOCKING
# ===========================================================================

class TestHeatLocking:
    def test_new_not_locked(self):
        from models.heat import Heat
        assert not Heat(event_id=1, heat_number=1).is_locked()

    def test_acquire(self):
        from models.heat import Heat
        h = Heat(event_id=1, heat_number=1)
        assert h.acquire_lock(1) and h.is_locked()

    def test_reacquire_same_user(self):
        from models.heat import Heat
        h = Heat(event_id=1, heat_number=1)
        h.acquire_lock(1)
        assert h.acquire_lock(1)

    def test_blocked_different_user(self):
        from models.heat import Heat
        h = Heat(event_id=1, heat_number=1)
        h.acquire_lock(1)
        assert not h.acquire_lock(2)

    def test_expires(self):
        from models.heat import Heat
        from config import HEAT_LOCK_TTL_SECONDS
        h = Heat(event_id=1, heat_number=1)
        h.acquire_lock(1)
        h.locked_at = datetime.now(timezone.utc) - timedelta(seconds=HEAT_LOCK_TTL_SECONDS + 10)
        assert not h.is_locked()

    def test_release(self):
        from models.heat import Heat
        h = Heat(event_id=1, heat_number=1)
        h.acquire_lock(1)
        h.release_lock(1)
        assert not h.is_locked()

    def test_release_wrong_user(self):
        from models.heat import Heat
        h = Heat(event_id=1, heat_number=1)
        h.acquire_lock(1)
        h.release_lock(999)
        assert h.is_locked()


# ===========================================================================
# 11. CONTEXT PROCESSOR
# ===========================================================================

class TestContextProcessor:
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    def test_injected_vars(self, app):
        with app.test_request_context('/'):
            ctx = {}
            for fn in app.template_context_processors[None]:
                ctx.update(fn())
            assert all(k in ctx for k in ('NAV', 'COMPETITION', 'LANGUAGES', 'ui'))

    def test_unscored_heats_zero(self, app):
        # Use a tournament_id that definitely has no heats seeded
        tid = 99999
        with app.test_request_context(f'/tournament/{tid}'):
            from flask import request as _req
            _req.view_args = {'tournament_id': tid}
            ctx = {}
            for fn in app.template_context_processors[None]:
                ctx.update(fn())
            assert ctx.get('unscored_heats', 0) == 0


# ===========================================================================
# 12. EVENTRESULT METHODS
# ===========================================================================

class TestEventResultMethods:
    def _er(self):
        from models.event import EventResult
        return EventResult(event_id=1, competitor_id=1, competitor_type='pro', competitor_name='T')

    def test_best_lowest(self):
        er = self._er(); er.run1_value = 15.2; er.run2_value = 14.8
        er.calculate_best_run('lowest_wins')
        assert er.best_run == 14.8

    def test_best_highest(self):
        er = self._er(); er.run1_value = 22.0; er.run2_value = 25.3
        er.calculate_best_run('highest_wins')
        assert er.best_run == 25.3

    def test_best_single(self):
        er = self._er(); er.run1_value = 18.0
        er.calculate_best_run('lowest_wins')
        assert er.best_run == 18.0

    def test_best_none(self):
        assert self._er().calculate_best_run('lowest_wins') is None

    def test_cumulative_all(self):
        er = self._er(); er.run1_value = 5; er.run2_value = 7; er.run3_value = 3
        er.calculate_cumulative_score()
        assert er.result_value == 15

    def test_cumulative_partial(self):
        er = self._er(); er.run1_value = 5; er.run3_value = 3
        er.calculate_cumulative_score()
        assert er.result_value == 8

    def test_cumulative_none(self):
        er = self._er(); er.calculate_cumulative_score()
        assert er.result_value is None


# ===========================================================================
# 13-15. SERVICE WORKER, STATUS CONSTANTS, EVENT PROPERTIES
# ===========================================================================

class TestServiceWorker:
    def test_sw_from_root(self, app):
        r = app.test_client().get('/sw.js')
        assert r.status_code in (200, 404)

class TestTournamentStatus:
    def test_values(self):
        from config import TournamentStatus
        assert TournamentStatus.SETUP == 'setup'
        assert 'completed' not in TournamentStatus.ACTIVE_STATUSES

class TestEventProperties:
    def test_display_names(self):
        from models.event import Event
        assert Event(name='UH', gender='M', event_type='pro', tournament_id=1, scoring_type='time').display_name == "Men's UH"
        assert Event(name='SB', gender='F', event_type='pro', tournament_id=1, scoring_type='time').display_name == "Women's SB"
        assert Event(name='JJ', gender=None, event_type='pro', tournament_id=1, scoring_type='time').display_name == 'JJ'

    def test_is_hard_hit(self):
        from models.event import Event
        assert Event(name='Underhand Hard Hit', event_type='college', tournament_id=1, scoring_type='hits').is_hard_hit
        assert not Event(name='Underhand Speed', event_type='college', tournament_id=1, scoring_type='time').is_hard_hit

    def test_is_axe_cumulative(self):
        from models.event import Event
        assert Event(name='Axe Throw', event_type='college', tournament_id=1, scoring_type='score').is_axe_throw_cumulative
        assert not Event(name='Obstacle Pole', event_type='pro', tournament_id=1, scoring_type='time').is_axe_throw_cumulative


# ===========================================================================
# 16-19. CONFIG LISTS, PERMISSION COVERAGE, HEAT JSON, RANK CATEGORY
# ===========================================================================

class TestConfigEventLists:
    def test_required_keys(self):
        from config import COLLEGE_OPEN_EVENTS, COLLEGE_CLOSED_EVENTS, PRO_EVENTS
        for evt in COLLEGE_OPEN_EVENTS + COLLEGE_CLOSED_EVENTS + PRO_EVENTS:
            assert all(k in evt for k in ('name', 'scoring_type', 'stand_type'))

    def test_no_duplicates(self):
        from config import COLLEGE_OPEN_EVENTS, COLLEGE_CLOSED_EVENTS, PRO_EVENTS
        for evts in [COLLEGE_OPEN_EVENTS, COLLEGE_CLOSED_EVENTS, PRO_EVENTS]:
            names = [e['name'] for e in evts]
            assert len(names) == len(set(names))

    def test_stand_types_valid(self):
        from config import COLLEGE_OPEN_EVENTS, COLLEGE_CLOSED_EVENTS, PRO_EVENTS, STAND_CONFIGS
        for evt in COLLEGE_OPEN_EVENTS + COLLEGE_CLOSED_EVENTS + PRO_EVENTS:
            assert evt['stand_type'] in STAND_CONFIGS

    def test_no_pro_birling(self):
        from config import PRO_EVENTS
        assert all(e['stand_type'] != 'birling' for e in PRO_EVENTS)

    def test_scoring_types_valid(self):
        from config import COLLEGE_OPEN_EVENTS, COLLEGE_CLOSED_EVENTS, PRO_EVENTS
        valid = {'time', 'score', 'distance', 'hits', 'bracket'}
        for evt in COLLEGE_OPEN_EVENTS + COLLEGE_CLOSED_EVENTS + PRO_EVENTS:
            assert evt['scoring_type'] in valid

class TestBlueprintPermissionCoverage:
    def test_all_have_permission(self):
        from app import MANAGEMENT_BLUEPRINTS, BLUEPRINT_PERMISSIONS
        assert all(bp in BLUEPRINT_PERMISSIONS for bp in MANAGEMENT_BLUEPRINTS)

    def test_attrs_exist(self):
        from app import BLUEPRINT_PERMISSIONS
        from models.user import User
        for bp, attr in BLUEPRINT_PERMISSIONS.items():
            assert hasattr(User, attr)

class TestHeatJSONRoundTrip:
    def test_competitors(self):
        from models.heat import Heat
        h = Heat(event_id=1, heat_number=1)
        h.set_competitors([10, 20])
        assert h.get_competitors() == [10, 20]

    def test_add_remove(self):
        from models.heat import Heat
        h = Heat(event_id=1, heat_number=1)
        h.set_competitors([1, 2])
        h.add_competitor(3); h.remove_competitor(2)
        assert h.get_competitors() == [1, 3]

    def test_stands(self):
        from models.heat import Heat
        h = Heat(event_id=1, heat_number=1)
        h.set_stand_assignment(10, 3)
        assert h.get_stand_for_competitor(10) == 3
        assert h.get_stand_for_competitor(99) is None

class TestEventRankCategory:
    def test_mappings(self):
        from config import event_rank_category
        from types import SimpleNamespace as NS
        assert event_rank_category(NS(stand_type='springboard', is_partnered=False)) == 'springboard'
        assert event_rank_category(NS(stand_type='saw_hand', is_partnered=False)) == 'singlebuck'
        assert event_rank_category(NS(stand_type='saw_hand', is_partnered=True, partner_gender='mixed')) == 'jack_jill'
        assert event_rank_category(NS(stand_type='saw_hand', is_partnered=True, partner_gender=None)) == 'doublebuck'
        assert event_rank_category(NS(stand_type='peavey')) is None
        assert event_rank_category(None) is None
