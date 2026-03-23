"""
Mark Assignment Service tests — STRATHMARK handicap start-mark pipeline.

Tests is_mark_assignment_eligible() and assign_handicap_marks() with mocked
STRATHMARK HandicapCalculator.  All external calls are mocked; no real
STRATHMARK package or Supabase connection required.

Run:
    pytest tests/test_mark_assignment.py -v

Requirements:
    pytest (pip install pytest)
    All app dependencies installed.
"""
import logging
import pytest
from unittest.mock import patch, MagicMock

from database import db as _db


# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_woodboss.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Create a test Flask app with in-memory SQLite."""
    import os
    os.environ.setdefault('SECRET_KEY', 'test-secret-marks')
    os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

    from app import create_app
    _app = create_app()
    _app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': False,
        'WTF_CSRF_CHECK_DEFAULT': False,
        'SERVER_NAME': None,
    })

    with _app.app_context():
        _db.create_all()
        yield _app
        _db.session.remove()
        # _db.drop_all() � skipped; in-memory SQLite is discarded on exit


@pytest.fixture(autouse=True)
def db_session(app):
    """Wrap each test in a transaction and roll back afterward."""
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def tournament(db_session):
    """Create a fresh tournament."""
    from models import Tournament
    t = Tournament(name='Mark Test 2026', year=2026, status='setup')
    db_session.add(t)
    db_session.flush()
    return t


def _make_event(db_session, tournament, **kwargs):
    """Helper: create an Event with sensible defaults."""
    from models import Event
    defaults = dict(
        tournament_id=tournament.id,
        name='Underhand Speed',
        event_type='pro',
        gender='M',
        scoring_type='time',
        scoring_order='lowest_wins',
        stand_type='underhand',
        is_handicap=False,
        status='pending',
    )
    defaults.update(kwargs)
    e = Event(**defaults)
    db_session.add(e)
    db_session.flush()
    return e


def _make_pro_competitor(db_session, tournament, name, gender='M', strathmark_id=None):
    """Helper: create a ProCompetitor."""
    from models import ProCompetitor
    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status='active',
    )
    if strathmark_id:
        c.strathmark_id = strathmark_id
    db_session.add(c)
    db_session.flush()
    return c


def _make_result(db_session, event, competitor, status='pending'):
    """Helper: create an EventResult row."""
    from models import EventResult
    r = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type=event.event_type,
        competitor_name=competitor.name,
        status=status,
    )
    db_session.add(r)
    db_session.flush()
    return r


# ---------------------------------------------------------------------------
# is_mark_assignment_eligible()
# ---------------------------------------------------------------------------

class TestIsMarkAssignmentEligible:
    """Tests for is_mark_assignment_eligible(event)."""

    def test_eligible_underhand_handicap(self, db_session, tournament):
        """Handicap underhand time event is eligible."""
        from services.mark_assignment import is_mark_assignment_eligible
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        assert is_mark_assignment_eligible(event) is True

    def test_eligible_standing_block_handicap(self, db_session, tournament):
        """Handicap standing_block time event is eligible."""
        from services.mark_assignment import is_mark_assignment_eligible
        event = _make_event(db_session, tournament,
                            name='Standing Block Speed',
                            stand_type='standing_block', scoring_type='time', is_handicap=True)
        assert is_mark_assignment_eligible(event) is True

    def test_eligible_springboard_handicap(self, db_session, tournament):
        """Handicap springboard time event is eligible."""
        from services.mark_assignment import is_mark_assignment_eligible
        event = _make_event(db_session, tournament,
                            name='Springboard',
                            stand_type='springboard', scoring_type='time', is_handicap=True)
        assert is_mark_assignment_eligible(event) is True

    def test_not_eligible_non_handicap(self, db_session, tournament):
        """Championship event (is_handicap=False) is NOT eligible."""
        from services.mark_assignment import is_mark_assignment_eligible
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=False)
        assert is_mark_assignment_eligible(event) is False

    def test_not_eligible_ineligible_stand_type_saw_hand(self, db_session, tournament):
        """saw_hand stand type is NOT eligible even if is_handicap=True."""
        from services.mark_assignment import is_mark_assignment_eligible
        event = _make_event(db_session, tournament,
                            name='Single Buck',
                            stand_type='saw_hand', scoring_type='time', is_handicap=True)
        assert is_mark_assignment_eligible(event) is False

    def test_not_eligible_ineligible_stand_type_hot_saw(self, db_session, tournament):
        """hot_saw stand type is NOT eligible even if is_handicap=True."""
        from services.mark_assignment import is_mark_assignment_eligible
        event = _make_event(db_session, tournament,
                            name='Hot Saw',
                            stand_type='hot_saw', scoring_type='time', is_handicap=True)
        assert is_mark_assignment_eligible(event) is False

    def test_not_eligible_hits_scoring(self, db_session, tournament):
        """Hard Hit events (scoring_type='hits') are NOT eligible."""
        from services.mark_assignment import is_mark_assignment_eligible
        event = _make_event(db_session, tournament,
                            name='Underhand Hard Hit',
                            stand_type='underhand', scoring_type='hits', is_handicap=True)
        assert is_mark_assignment_eligible(event) is False

    def test_not_eligible_score_scoring(self, db_session, tournament):
        """score-type events are NOT eligible (only time events qualify)."""
        from services.mark_assignment import is_mark_assignment_eligible
        event = _make_event(db_session, tournament,
                            name='Axe Throw',
                            stand_type='axe_throw', scoring_type='score',
                            scoring_order='highest_wins', is_handicap=True)
        assert is_mark_assignment_eligible(event) is False

    def test_not_eligible_bracket_scoring(self, db_session, tournament):
        """bracket scoring events are NOT eligible."""
        from services.mark_assignment import is_mark_assignment_eligible
        event = _make_event(db_session, tournament,
                            name='Birling',
                            stand_type='birling', scoring_type='bracket', is_handicap=True)
        assert is_mark_assignment_eligible(event) is False

    def test_not_eligible_no_is_handicap_attr(self, db_session, tournament):
        """An event object missing is_handicap attribute returns False (getattr guard)."""
        from services.mark_assignment import is_mark_assignment_eligible

        class FakeEvent:
            scoring_type = 'time'
            stand_type = 'underhand'

        assert is_mark_assignment_eligible(FakeEvent()) is False


# ---------------------------------------------------------------------------
# assign_handicap_marks() — not eligible / unconfigured guard paths
# ---------------------------------------------------------------------------

class TestAssignHandicapMarksGuards:
    """Test the early-return guard paths in assign_handicap_marks()."""

    def test_not_eligible_returns_not_eligible(self, db_session, tournament):
        """Non-eligible event returns status='not_eligible'."""
        from services.mark_assignment import assign_handicap_marks
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=False)
        result = assign_handicap_marks(event)
        assert result['status'] == 'not_eligible'
        assert result['assigned'] == 0
        assert result['skipped'] == 0

    @patch.dict('os.environ', {'STRATHMARK_SUPABASE_URL': '', 'STRATHMARK_SUPABASE_KEY': ''})
    def test_unconfigured_returns_unconfigured(self, db_session, tournament):
        """When STRATHMARK env vars are empty, returns status='unconfigured'."""
        from services.mark_assignment import assign_handicap_marks
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        result = assign_handicap_marks(event)
        assert result['status'] == 'unconfigured'
        assert result['assigned'] == 0


# ---------------------------------------------------------------------------
# assign_handicap_marks() — calculator failure paths
# ---------------------------------------------------------------------------

class TestAssignHandicapMarksCalculatorFailures:
    """Test graceful failure when STRATHMARK is unavailable or broken."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._get_handicap_calculator', return_value=None)
    def test_calculator_init_failure(self, mock_calc, db_session, tournament):
        """If HandicapCalculator cannot be created, returns status='error'."""
        from services.mark_assignment import assign_handicap_marks
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        comp = _make_pro_competitor(db_session, tournament, 'John Doe', strathmark_id='JDOEM')
        _make_result(db_session, event, comp)

        result = assign_handicap_marks(event)
        assert result['status'] == 'error'
        assert 'Could not initialise' in result['errors'][0]
        assert result['skipped'] == 1

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    def test_import_error_returns_none_calculator(self, db_session, tournament):
        """When strathmark package is not installed, _get_handicap_calculator returns None."""
        from services.mark_assignment import _get_handicap_calculator

        with patch.dict('sys.modules', {'strathmark': None, 'strathmark.calculator': None}):
            # Force a fresh import attempt by patching the import mechanism
            with patch('builtins.__import__', side_effect=ImportError('No module named strathmark')):
                calc = _get_handicap_calculator()
                assert calc is None

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    def test_calculator_constructor_exception(self, db_session, tournament):
        """When HandicapCalculator raises during init, returns None."""
        from services.mark_assignment import _get_handicap_calculator

        mock_module = MagicMock()
        mock_module.HandicapCalculator.side_effect = RuntimeError('bad config')

        with patch.dict('sys.modules', {'strathmark': MagicMock(), 'strathmark.calculator': mock_module}):
            calc = _get_handicap_calculator()
            assert calc is None


# ---------------------------------------------------------------------------
# assign_handicap_marks() — successful mark assignment
# ---------------------------------------------------------------------------

class TestAssignHandicapMarksSuccess:
    """Test the happy path: marks are fetched and stored on EventResult."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._get_handicap_calculator')
    @patch('services.mark_assignment._fetch_start_mark')
    def test_marks_stored_on_event_results(self, mock_fetch, mock_calc_factory, db_session, tournament):
        """handicap_factor is written to each EventResult from the mock calculator."""
        mock_calculator = MagicMock()
        mock_calc_factory.return_value = mock_calculator
        # _fetch_start_mark returns a float mark for each call
        mock_fetch.return_value = 5.25

        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        comp1 = _make_pro_competitor(db_session, tournament, 'Alice', 'F', strathmark_id='AALICEF')
        comp2 = _make_pro_competitor(db_session, tournament, 'Bob', 'M', strathmark_id='BBOBM')
        r1 = _make_result(db_session, event, comp1)
        r2 = _make_result(db_session, event, comp2)

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['status'] == 'ok'
        assert result['assigned'] == 2
        assert result['skipped'] == 0
        assert result['errors'] == []

        # Verify handicap_factor was written
        assert r1.handicap_factor == 5.25
        assert r2.handicap_factor == 5.25

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._get_handicap_calculator')
    @patch('services.mark_assignment._fetch_start_mark')
    def test_predicted_time_set_to_none(self, mock_fetch, mock_calc_factory, db_session, tournament):
        """predicted_time is set to None (current behavior pending MarkResult integration)."""
        mock_calc_factory.return_value = MagicMock()
        mock_fetch.return_value = 3.50

        event = _make_event(db_session, tournament,
                            stand_type='springboard', scoring_type='time', is_handicap=True,
                            name='Springboard')
        comp = _make_pro_competitor(db_session, tournament, 'Charlie', 'M', strathmark_id='CCHARLM')
        r = _make_result(db_session, event, comp)

        from services.mark_assignment import assign_handicap_marks
        assign_handicap_marks(event)

        assert r.predicted_time is None

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._get_handicap_calculator')
    @patch('services.mark_assignment._fetch_start_mark')
    def test_different_marks_per_competitor(self, mock_fetch, mock_calc_factory, db_session, tournament):
        """Different competitors can receive different start marks."""
        mock_calc_factory.return_value = MagicMock()
        # Return different marks for successive calls
        mock_fetch.side_effect = [4.0, 7.5]

        event = _make_event(db_session, tournament,
                            stand_type='standing_block', scoring_type='time', is_handicap=True,
                            name='Standing Block Speed')
        comp1 = _make_pro_competitor(db_session, tournament, 'Dan', 'M', strathmark_id='DDANM')
        comp2 = _make_pro_competitor(db_session, tournament, 'Eve', 'F', strathmark_id='EEVEF')
        r1 = _make_result(db_session, event, comp1)
        r2 = _make_result(db_session, event, comp2)

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['assigned'] == 2
        assert r1.handicap_factor == 4.0
        assert r2.handicap_factor == 7.5


# ---------------------------------------------------------------------------
# assign_handicap_marks() — skip/partial paths
# ---------------------------------------------------------------------------

class TestAssignHandicapMarksSkipPaths:
    """Test skipping for competitors without strathmark_id or failed mark fetch."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._get_handicap_calculator')
    @patch('services.mark_assignment._fetch_start_mark')
    def test_no_strathmark_id_skipped(self, mock_fetch, mock_calc_factory, db_session, tournament):
        """Competitor without strathmark_id is skipped (competes from scratch)."""
        mock_calc_factory.return_value = MagicMock()
        mock_fetch.return_value = 5.0

        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        # comp_with has a strathmark_id; comp_without does not
        comp_with = _make_pro_competitor(db_session, tournament, 'Frank', 'M', strathmark_id='FFRANKM')
        comp_without = _make_pro_competitor(db_session, tournament, 'Grace', 'F')
        r_with = _make_result(db_session, event, comp_with)
        r_without = _make_result(db_session, event, comp_without)

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['assigned'] == 1
        assert result['skipped'] == 1
        # comp_with got a mark; comp_without stays at default
        assert r_with.handicap_factor == 5.0
        assert r_without.handicap_factor == 1.0  # default

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._get_handicap_calculator')
    @patch('services.mark_assignment._fetch_start_mark')
    def test_fetch_returns_none_skipped(self, mock_fetch, mock_calc_factory, db_session, tournament):
        """When _fetch_start_mark returns None, competitor is skipped."""
        mock_calc_factory.return_value = MagicMock()
        mock_fetch.return_value = None

        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        comp = _make_pro_competitor(db_session, tournament, 'Hank', 'M', strathmark_id='HHANKM')
        r = _make_result(db_session, event, comp)

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['assigned'] == 0
        assert result['skipped'] == 1
        assert r.handicap_factor == 1.0  # unchanged

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._get_handicap_calculator')
    @patch('services.mark_assignment._fetch_start_mark')
    def test_no_results_returns_ok_zero(self, mock_fetch, mock_calc_factory, db_session, tournament):
        """Event with no EventResult rows returns status='ok', assigned=0."""
        mock_calc_factory.return_value = MagicMock()

        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['status'] == 'ok'
        assert result['assigned'] == 0
        assert result['skipped'] == 0
        # _fetch_start_mark should never be called
        mock_fetch.assert_not_called()

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._get_handicap_calculator')
    @patch('services.mark_assignment._fetch_start_mark')
    def test_scratched_results_excluded(self, mock_fetch, mock_calc_factory, db_session, tournament):
        """Results with status='scratched' are not queried."""
        mock_calc_factory.return_value = MagicMock()
        mock_fetch.return_value = 5.0

        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        comp = _make_pro_competitor(db_session, tournament, 'Ivy', 'F', strathmark_id='IIVYF')
        _make_result(db_session, event, comp, status='scratched')

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        # scratched results are filtered out by the .filter() query
        assert result['assigned'] == 0
        assert result['skipped'] == 0


# ---------------------------------------------------------------------------
# assign_handicap_marks() — audit logging
# ---------------------------------------------------------------------------

class TestAssignHandicapMarksLogging:
    """Verify that assign_handicap_marks logs mark assignment activity."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._get_handicap_calculator')
    @patch('services.mark_assignment._fetch_start_mark')
    def test_info_log_on_successful_marks(self, mock_fetch, mock_calc_factory, db_session, tournament, caplog):
        """An INFO log line with the mark count is emitted."""
        mock_calc_factory.return_value = MagicMock()
        mock_fetch.return_value = 3.0

        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        comp = _make_pro_competitor(db_session, tournament, 'Jack', 'M', strathmark_id='JJACKM')
        _make_result(db_session, event, comp)

        from services.mark_assignment import assign_handicap_marks

        with caplog.at_level(logging.INFO, logger='services.mark_assignment'):
            assign_handicap_marks(event)

        assert any('HandicapCalculator produced 1 marks' in msg for msg in caplog.messages)

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._get_handicap_calculator')
    @patch('services.mark_assignment._fetch_start_mark')
    def test_info_log_zero_marks(self, mock_fetch, mock_calc_factory, db_session, tournament, caplog):
        """Even with zero marks assigned, the INFO log line is emitted."""
        mock_calc_factory.return_value = MagicMock()
        mock_fetch.return_value = None

        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        comp = _make_pro_competitor(db_session, tournament, 'Kay', 'F', strathmark_id='KKAYF')
        _make_result(db_session, event, comp)

        from services.mark_assignment import assign_handicap_marks

        with caplog.at_level(logging.INFO, logger='services.mark_assignment'):
            assign_handicap_marks(event)

        assert any('HandicapCalculator produced 0 marks' in msg for msg in caplog.messages)

    @patch.dict('os.environ', {'STRATHMARK_SUPABASE_URL': '', 'STRATHMARK_SUPABASE_KEY': ''})
    def test_info_log_when_unconfigured(self, db_session, tournament, caplog):
        """Unconfigured STRATHMARK logs an INFO message about skipping."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)

        from services.mark_assignment import assign_handicap_marks

        with caplog.at_level(logging.INFO, logger='services.mark_assignment'):
            assign_handicap_marks(event)

        assert any('STRATHMARK not configured' in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# _fetch_start_mark() — unit tests
# ---------------------------------------------------------------------------

class TestFetchStartMark:
    """Direct tests for the _fetch_start_mark() helper."""

    def test_returns_float_mark(self, app):
        """Normal path: calculator returns a positive float."""
        from services.mark_assignment import _fetch_start_mark
        calc = MagicMock()
        calc.get_start_mark.return_value = 6.75
        with app.app_context():
            mark = _fetch_start_mark(calc, 'SM123', 'UH', 'Test Comp')
        assert mark == 6.75

    def test_returns_zero_scratch(self, app):
        """Zero is a valid mark (scratch competitor)."""
        from services.mark_assignment import _fetch_start_mark
        calc = MagicMock()
        calc.get_start_mark.return_value = 0.0
        with app.app_context():
            mark = _fetch_start_mark(calc, 'SM123', 'UH', 'Test Comp')
        assert mark == 0.0

    def test_negative_mark_clamped_to_zero(self, app):
        """Negative marks are clamped to 0.0."""
        from services.mark_assignment import _fetch_start_mark
        calc = MagicMock()
        calc.get_start_mark.return_value = -2.5
        with app.app_context():
            mark = _fetch_start_mark(calc, 'SM123', 'UH', 'Test Comp')
        assert mark == 0.0

    def test_none_returned_when_calculator_returns_none(self, app):
        """When calculator returns None, _fetch_start_mark returns None."""
        from services.mark_assignment import _fetch_start_mark
        calc = MagicMock()
        calc.get_start_mark.return_value = None
        with app.app_context():
            mark = _fetch_start_mark(calc, 'SM123', 'UH', 'Test Comp')
        assert mark is None

    def test_exception_returns_none(self, app):
        """When calculator raises, _fetch_start_mark catches it and returns None."""
        from services.mark_assignment import _fetch_start_mark
        calc = MagicMock()
        calc.get_start_mark.side_effect = ConnectionError('timeout')
        with app.app_context():
            mark = _fetch_start_mark(calc, 'SM123', 'UH', 'Test Comp')
        assert mark is None


# ---------------------------------------------------------------------------
# _build_strathmark_id_lookup() — unit tests
# ---------------------------------------------------------------------------

class TestBuildStrathmarkIdLookup:
    """Tests for the internal _build_strathmark_id_lookup helper."""

    def test_pro_lookup(self, db_session, tournament):
        """Pro event returns strathmark_id for pro competitors."""
        from services.mark_assignment import _build_strathmark_id_lookup
        event = _make_event(db_session, tournament, event_type='pro')
        comp = _make_pro_competitor(db_session, tournament, 'Zoe', 'F', strathmark_id='ZZOEF')

        lookup = _build_strathmark_id_lookup(event, [comp.id])
        assert lookup[comp.id] == 'ZZOEF'

    def test_pro_without_strathmark_id(self, db_session, tournament):
        """Pro without strathmark_id has None in lookup."""
        from services.mark_assignment import _build_strathmark_id_lookup
        event = _make_event(db_session, tournament, event_type='pro')
        comp = _make_pro_competitor(db_session, tournament, 'Noid', 'M')

        lookup = _build_strathmark_id_lookup(event, [comp.id])
        assert lookup[comp.id] is None

    def test_empty_ids(self, db_session, tournament):
        """Empty competitor_ids list returns empty dict."""
        from services.mark_assignment import _build_strathmark_id_lookup
        event = _make_event(db_session, tournament, event_type='pro')

        lookup = _build_strathmark_id_lookup(event, [])
        assert lookup == {}

    def test_college_lookup(self, db_session, tournament):
        """College event queries CollegeCompetitor model."""
        from services.mark_assignment import _build_strathmark_id_lookup
        from models import Team, CollegeCompetitor

        team = Team(
            tournament_id=tournament.id,
            team_code='UM-A',
            school_name='University of Montana',
            school_abbreviation='UM',
        )
        db_session.add(team)
        db_session.flush()

        cc = CollegeCompetitor(
            tournament_id=tournament.id,
            team_id=team.id,
            name='College Carl',
            gender='M',
            status='active',
        )
        cc.strathmark_id = 'CCARLM'
        db_session.add(cc)
        db_session.flush()

        event = _make_event(db_session, tournament, event_type='college',
                            name='Underhand Speed', stand_type='underhand')

        lookup = _build_strathmark_id_lookup(event, [cc.id])
        assert lookup[cc.id] == 'CCARLM'
