"""
Shared pytest fixtures for the Missoula Pro-Am Manager test suite.

Provides:
  - Flask app with temp-file SQLite built via ``flask db upgrade`` (module-scoped)
  - Per-test transactional rollback (db_session)
  - Auth client (logged in as admin/judge)
  - Seed helpers for tournaments, teams, competitors, events, heats, results
  - ``create_test_app()`` helper for test files that define their own app fixture

Existing test files that define their own `app` fixture are unaffected —
pytest resolves local fixtures before conftest.

IMPORTANT: Tests use ``flask db upgrade`` (not ``db.create_all()``) so that the
migration chain is exercised on every run.  If a migration fails to add a column,
the tests will fail — just like production would.
"""
import json
import os
import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-conftest')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

from database import db as _db
from tests.db_test_utils import create_test_app  # noqa: F401 — re-exported


# ---------------------------------------------------------------------------
# App + DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Create a test Flask app with a temp SQLite DB built via migrations."""
    _app, db_path = create_test_app()

    with _app.app_context():
        yield _app
        _db.session.remove()

    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture()
def db_session(app):
    """Wrap each test in a nested transaction and roll back afterward.

    NOT autouse — test files must request this explicitly (or via fixtures
    that depend on it).  Test files that define their own ``app`` fixture
    also define their own ``db_session`` and are unaffected.
    """
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


# ---------------------------------------------------------------------------
# Auth fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def admin_user(db_session):
    """Create and return an admin user."""
    from models.user import User
    u = User(username='test_admin', role='admin')
    u.set_password('testpass')
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture()
def judge_user(db_session):
    """Create and return a judge user."""
    from models.user import User
    u = User(username='test_judge', role='judge')
    u.set_password('judgepass')
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture()
def scorer_user(db_session):
    """Create and return a scorer user."""
    from models.user import User
    u = User(username='test_scorer', role='scorer')
    u.set_password('scorerpass')
    db_session.add(u)
    db_session.flush()
    return u


@pytest.fixture()
def client(app):
    """Return an unauthenticated test client."""
    return app.test_client()


@pytest.fixture()
def auth_client(app, admin_user):
    """Return a test client logged in as the admin user."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(admin_user.id)
    return c


# ---------------------------------------------------------------------------
# Seed helpers — importable by test files
# ---------------------------------------------------------------------------

def make_tournament(session, name='Test Tournament 2026', year=2026, status='setup'):
    from models import Tournament
    t = Tournament(name=name, year=year, status=status)
    session.add(t)
    session.flush()
    return t


def make_team(session, tournament, code='UM-A', school='University of Montana', abbrev='UM'):
    from models import Team
    t = Team(
        tournament_id=tournament.id,
        team_code=code,
        school_name=school,
        school_abbreviation=abbrev,
    )
    session.add(t)
    session.flush()
    return t


def make_college_competitor(session, tournament, team, name, gender='M',
                            events=None, status='active'):
    from models.competitor import CollegeCompetitor
    c = CollegeCompetitor(
        tournament_id=tournament.id,
        team_id=team.id,
        name=name,
        gender=gender,
        events_entered=json.dumps(events or []),
        status=status,
    )
    session.add(c)
    session.flush()
    return c


def make_pro_competitor(session, tournament, name, gender='M', events=None,
                        gear_sharing=None, partners=None, status='active',
                        is_left_handed_springboard=False,
                        springboard_slow_heat=False,
                        strathmark_id=None):
    from models.competitor import ProCompetitor
    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        events_entered=json.dumps(events or []),
        gear_sharing=json.dumps(gear_sharing or {}),
        partners=json.dumps(partners or {}),
        status=status,
        is_left_handed_springboard=is_left_handed_springboard,
        springboard_slow_heat=springboard_slow_heat,
    )
    if strathmark_id:
        c.strathmark_id = strathmark_id
    session.add(c)
    session.flush()
    return c


def make_event(session, tournament, name, event_type='pro', gender=None,
               scoring_type='time', scoring_order='lowest_wins',
               stand_type='underhand', max_stands=5, is_partnered=False,
               requires_dual_runs=False, requires_triple_runs=False,
               is_handicap=False, is_open=False, has_prelims=False,
               payouts=None, status='pending'):
    from models.event import Event
    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender=gender,
        scoring_type=scoring_type,
        scoring_order=scoring_order,
        stand_type=stand_type,
        max_stands=max_stands,
        is_partnered=is_partnered,
        requires_dual_runs=requires_dual_runs,
        requires_triple_runs=requires_triple_runs,
        is_handicap=is_handicap,
        is_open=is_open,
        has_prelims=has_prelims,
        payouts=json.dumps(payouts or {}),
        status=status,
    )
    session.add(e)
    session.flush()
    return e


def make_heat(session, event, heat_number=1, run_number=1,
              competitors=None, stand_assignments=None, status='pending',
              flight_id=None, flight_position=None):
    from models.heat import Heat
    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
        competitors=json.dumps(competitors or []),
        stand_assignments=json.dumps(stand_assignments or {}),
        status=status,
        flight_id=flight_id,
        flight_position=flight_position,
    )
    session.add(h)
    session.flush()
    return h


def make_event_result(session, event, competitor, competitor_type='pro',
                      result_value=None, run1_value=None, run2_value=None,
                      run3_value=None, best_run=None, tiebreak_value=None,
                      handicap_factor=1.0, predicted_time=None,
                      final_position=None, points_awarded=0,
                      payout_amount=0.0, status='pending',
                      partner_name=None):
    from models.event import EventResult
    r = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type=competitor_type,
        competitor_name=competitor.name,
        partner_name=partner_name,
        result_value=result_value,
        run1_value=run1_value,
        run2_value=run2_value,
        run3_value=run3_value,
        best_run=best_run,
        tiebreak_value=tiebreak_value,
        handicap_factor=handicap_factor,
        predicted_time=predicted_time,
        final_position=final_position,
        points_awarded=points_awarded,
        payout_amount=payout_amount,
        status=status,
    )
    session.add(r)
    session.flush()
    return r


def make_flight(session, tournament, flight_number=1, name=None,
                status='pending'):
    from models.heat import Flight
    f = Flight(
        tournament_id=tournament.id,
        flight_number=flight_number,
        name=name or f'Flight {flight_number}',
        status=status,
    )
    session.add(f)
    session.flush()
    return f
