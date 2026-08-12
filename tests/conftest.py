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

SAFEGUARD: Three layers prevent test data from touching the production DB:
  1. pytest_configure() verifies instance/proam.db is untouched after every session
  2. create_app() refuses to start with TESTING=True if DB URI points to proam.db
  3. create_test_app() sets DATABASE_URL env var BEFORE calling create_app()
"""
import json
import os
import pathlib
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import text as sa_text

os.environ.setdefault('SECRET_KEY', 'test-secret-conftest')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

from database import db as _db
from tests.db_test_utils import create_test_app  # noqa: F401 — re-exported

# ---------------------------------------------------------------------------
# SAFEGUARD: Production DB protection
# ---------------------------------------------------------------------------

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PROD_DB_PATH = _PROJECT_ROOT / 'instance' / 'proam.db'


def _prod_db_fingerprint():
    """Return (exists, size, mtime) for the production DB file."""
    if _PROD_DB_PATH.exists():
        stat = _PROD_DB_PATH.stat()
        return (True, stat.st_size, stat.st_mtime)
    return (False, 0, 0)


def pytest_configure(config):
    """Record production DB state before tests run."""
    config._prod_db_before = _prod_db_fingerprint()


def pytest_unconfigure(config):
    """Verify production DB was not modified by the test session."""
    before = getattr(config, '_prod_db_before', None)
    if before is None:
        return
    after = _prod_db_fingerprint()
    if before != after:
        import warnings
        warnings.warn(
            f'\n\n*** PRODUCTION DATABASE MODIFIED BY TESTS ***\n'
            f'Before: exists={before[0]}, size={before[1]}, mtime={before[2]}\n'
            f'After:  exists={after[0]}, size={after[1]}, mtime={after[2]}\n'
            f'Path: {_PROD_DB_PATH}\n'
            f'This should NEVER happen. Investigate immediately.\n',
            stacklevel=1,
        )


@pytest.fixture(autouse=True, scope='session')
def _guard_production_db():
    """Session-scoped autouse fixture: abort if any test touches proam.db.

    Checks the production DB file before and after the entire test session.
    If size or mtime changes, the test session fails loudly.
    """
    before = _prod_db_fingerprint()
    yield
    after = _prod_db_fingerprint()
    if before != after:
        pytest.fail(
            f'FATAL: Tests modified the production database!\n'
            f'Before: exists={before[0]}, size={before[1]}\n'
            f'After:  exists={after[0]}, size={after[1]}\n'
            f'Path: {_PROD_DB_PATH}\n'
            f'All test data MUST use temporary databases via create_test_app().'
        )


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
def reference_gate_disarmed():
    """Let a test seed a deliberately bad competitor reference.

    ``services/reference_gate.py`` is armed on the scoped session by the app
    factory, so a module whose *subject* is bad references cannot write its own
    subject matter. Two modules need this and neither of them is testing the
    gate: ``test_reference_audit.py``, which needs damage to detect, and
    ``test_repair_era1_references.py``, which needs damage to repair. The
    gate's own tests in ``test_reference_gate.py`` arm it on purpose and do not
    use this.

    Restores whatever it found rather than unconditionally re-arming. ``app``
    is module scoped and the scoped session outlives the test, so a fixture
    that armed a gate a module never had would leak a listener forward.
    """
    from database import db
    from services import reference_gate

    was_armed = reference_gate.is_installed(db.session)
    reference_gate.uninstall(db.session)
    try:
        yield
    finally:
        if was_armed:
            reference_gate.install(db.session)


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


def ensure_competitors(session, tournament, ids, competitor_type='pro',
                       team=None, events=None):
    """Create a real competitor row for each id in `ids`, if it is missing.

    ``heat_assignments.uid`` is NOT NULL with a foreign key onto the identity
    spine as of s8a0b2c3d4e5, so ``Heat.set_roster`` raises
    ``BadHeatAssignment`` for a roster naming an id that is in no competitor
    row. A number of fixtures in this suite predate that and build heats out
    of invented ids like [1, 2, 3, 4] because nothing ever checked. This
    materialises exactly those ids so those fixtures keep meaning what they
    meant.

    The ids are set explicitly. That is normally the wrong thing to do here,
    because the pro and college id sequences are shared with rows a test did
    not create and a hardcoded id is a guess about somebody else's counter.
    It is safe in this one function: every caller passes ids it invented, the
    rows are created inside the test's nested transaction, and on PostgreSQL
    the sequence is advanced past whatever was inserted so a later
    default-valued insert in the same test cannot land on one of them. SQLite
    picks max(rowid)+1 and needs no help.

    Returns {id: competitor}, including ids that already existed.
    """
    from models.competitor import CollegeCompetitor, ProCompetitor

    if competitor_type == 'college':
        from models import Team
        model = CollegeCompetitor
        if team is None:
            # Reuse the tournament's team rather than making one per call.
            # team_code is unique per tournament and callers hit this helper
            # once per heat, so a fresh team every time is an IntegrityError on
            # the second heat.
            team = (Team.query.filter_by(tournament_id=tournament.id)
                    .order_by(Team.id).first())
            if team is None:
                team = make_team(session, tournament)
    elif competitor_type == 'pro':
        model = ProCompetitor
        team = None
    else:
        raise ValueError(f'competitor_type must be pro or college, '
                         f'not {competitor_type!r}')

    out = {}
    created = False
    for raw in ids:
        comp_id = int(raw)
        existing = session.get(model, comp_id)
        if existing is not None:
            out[comp_id] = existing
            continue
        kwargs = dict(
            id=comp_id,
            tournament_id=tournament.id,
            name=f'{competitor_type.title()} {comp_id}',
            gender='M',
            events_entered=json.dumps(events or []),
            status='active',
        )
        if team is not None:
            kwargs['team_id'] = team.id
        comp = model(**kwargs)
        session.add(comp)
        out[comp_id] = comp
        created = True

    session.flush()

    if created and session.get_bind().dialect.name == 'postgresql':
        session.execute(sa_text(
            "SELECT setval(pg_get_serial_sequence(:t, 'id'), "
            "GREATEST((SELECT COALESCE(MAX(id), 1) FROM " + model.__tablename__
            + "), 1))"
        ), {'t': model.__tablename__})

    return out


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


def seat_roster(session, heat, comp_ids, stands=None):
    """Seat `comp_ids` on an already-flushed `heat` as real assignment rows.

    D12-C commit E moved the roster off `heats.competitors` and onto
    `heat_assignments`, so a fixture that only writes the JSON column builds a
    heat that every reader in the app sees as empty. A dozen modules in this
    suite carry their own private `_make_heat` that does exactly that, and
    each one wants the same four lines: find the event, make the invented ids
    real, write the rows, flush. They live here instead of being retyped
    twelve times.

    `ensure_competitors` runs first because most of those fixtures name ids
    like [1, 2, 3, 4] that never belonged to a competitor row. An assignment
    row carries a uid with a foreign key onto the identity spine, so the ids
    have to exist before they can be seated.
    """
    from models import Tournament
    from models.event import Event

    if not comp_ids:
        return heat

    event = session.get(Event, heat.event_id)
    tournament = session.get(Tournament, event.tournament_id)
    ensure_competitors(session, tournament, comp_ids,
                       competitor_type=event.event_type)
    heat.set_roster(event.event_type, list(comp_ids), stands or {})
    session.flush()
    return heat


def make_heat(session, event, heat_number=1, run_number=1,
              competitors=None, stand_assignments=None, status='pending',
              flight_id=None, flight_position=None):
    """Build a heat, seating `competitors` as real assignment rows.

    ``competitors`` and ``stand_assignments`` are still the parameter names
    because 312 call sites in this suite use them, and they still describe
    what the caller means: a roster and a stand map. What changed under them
    is where that lands. Until D12-C commit E they were written straight into
    two JSON columns on ``heats``; since E they go through
    ``Heat.set_roster`` into ``heat_assignments`` rows, and as of commit F3
    the columns are gone from the table entirely.

    A ``seat=False`` parameter stood here through F2, reproducing the old
    columns-only write for fixtures whose subject was the drift between the
    two stores. F2 deleted all of those subjects and F3 deleted the columns,
    so there is no longer a way to build an unseated heat and nothing that
    wanted one.
    """
    from models.heat import Heat
    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
        status=status,
        flight_id=flight_id,
        flight_position=flight_position,
    )
    session.add(h)
    session.flush()

    # D12-C commit E: the roster is `heat_assignments` rows now. Writing the
    # JSON columns used to be the whole job; as of that commit it seeded a
    # heat every reader in the app saw as empty, and as of F3 there are no
    # columns to write. 312 call sites in this suite pass `competitors=`, so
    # the seating belongs here and not in each of them.
    #
    # `ensure_competitors` runs first because most of those call sites invent
    # ids like [1, 2, 3, 4]. An assignment row carries a uid with a foreign
    # key onto the identity spine, so an invented id has to become a real
    # competitor before it can be seated. See that helper for why setting the
    # ids explicitly is safe in this one place.
    if competitors:
        from models import Tournament
        tournament = session.get(Tournament, event.tournament_id)
        ensure_competitors(session, tournament, competitors,
                           competitor_type=event.event_type)
        h.set_roster(event.event_type, list(competitors),
                     stand_assignments or {})
        session.flush()

    return h


def make_event_result(session, event, competitor, competitor_type='pro',
                      result_value=None, run1_value=None, run2_value=None,
                      run3_value=None, best_run=None, tiebreak_value=None,
                      handicap_factor=0.0, predicted_time=None,
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


# ---------------------------------------------------------------------------
# The mock-suite bracket harness
# ---------------------------------------------------------------------------


@contextmanager
def patched_bracket_deps():
    """Run ``BirlingBracket`` with no database, the way the mock suite does.

    Forty call sites used to spell this as two bare patches::

        with patch('services.birling_bracket.db'), \
             patch('services.birling_bracket.birling_rows'):

    The service now reads only the normalized tables. This harness therefore
    gives each fake event a private in-memory row document. It is deliberately
    not a production fallback: real authority and migration behavior are
    covered by database-backed Birling tests.

    Yields ``(mock_db, mock_rows)`` for the handful of callers that assert on
    the writer.
    """
    from services import birling_rows as _real_rows

    def load_document(event):
        document = event.__dict__.get('_mock_birling_document')
        if document is not None:
            return document
        document = _real_rows.parse_document(getattr(event, 'payouts', None))
        if any(key in document for key in _real_rows.BRACKET_KEYS):
            event.__dict__['_mock_birling_document'] = document
            return document
        return _real_rows.empty_document()

    def project_document(event, document):
        event.__dict__['_mock_birling_document'] = document

    with patch('services.birling_bracket.db') as mock_db, \
            patch('services.birling_bracket.birling_rows') as mock_rows:
        mock_rows.empty_document = _real_rows.empty_document
        mock_rows.UnloadableBracket = _real_rows.UnloadableBracket
        mock_rows.load_document.side_effect = load_document
        mock_rows.project_document.side_effect = project_document
        yield mock_db, mock_rows
