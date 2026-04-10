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
from unittest.mock import MagicMock, patch

import pytest

from database import db as _db

# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_woodboss.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Create a test Flask app with temp-file SQLite built via flask db upgrade."""
    import os

    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()

    with _app.app_context():
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


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


def _make_wood_for_event(db_session, event):
    """Set up a default WoodConfig for *event* so the bulletproofed
    `_build_wood_profile()` returns a valid profile instead of None.

    The bulletproofed mark_assignment.assign_handicap_marks() refuses to run
    when no wood config is set (returns status='no_wood_config').  Tests
    that exercise the success / skip paths must therefore configure wood
    first.
    """
    from models.wood_config import WoodConfig
    gender = getattr(event, 'gender', None) or 'M'
    event_type = getattr(event, 'event_type', 'pro')
    config_key = f'block_{event.stand_type}_{event_type}_{gender}'
    wc = WoodConfig(
        tournament_id=event.tournament_id,
        config_key=config_key,
        species='Cottonwood',
        size_value=12.0,
        size_unit='in',
    )
    db_session.add(wc)
    db_session.flush()
    return wc


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

    def test_not_eligible_springboard_yet(self, db_session, tournament):
        """Springboard is NOT eligible until upstream STRATHMARK ships SPB support.

        STRATHMARK currently rejects any event_code outside {SB, UH} with a
        ValueError, so we gate springboard out at the door rather than letting
        the route reach the calculator with an unsupported event.  When
        STRATHMARK adds 'SPB' the eligibility check (and this test) flip.
        """
        from services.mark_assignment import is_mark_assignment_eligible
        event = _make_event(db_session, tournament,
                            name='Springboard',
                            stand_type='springboard', scoring_type='time', is_handicap=True)
        assert is_mark_assignment_eligible(event) is False

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
# assign_handicap_marks() — wood-config guardrail
# ---------------------------------------------------------------------------

class TestAssignHandicapMarksNoWoodConfig:
    """The bulletproofed contract refuses to run when no WoodConfig is set.

    This is the operator-error guardrail: silently producing wrong marks
    against a guessed Pine 300mm profile is worse than failing loudly.
    """

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    def test_no_wood_config_returns_no_wood_status(self, db_session, tournament):
        """When the event has no matching WoodConfig, status='no_wood_config'."""
        from services.mark_assignment import assign_handicap_marks
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        comp = _make_pro_competitor(db_session, tournament, 'NoWood', 'M', strathmark_id='NWOODM')
        _make_result(db_session, event, comp)

        result = assign_handicap_marks(event)

        assert result['status'] == 'no_wood_config'
        assert result['assigned'] == 0
        # The bulletproofed contract returns _empty_result('no_wood_config'),
        # so skipped is 0 (we never inspected individual rows yet) and the
        # route surfaces the missing-wood error from the status alone.
        assert result['skipped'] == 0
        assert result['errors'] == []

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    def test_no_wood_config_does_not_call_calculator(self, db_session, tournament):
        """The calculator factory must NOT be called when wood is missing."""
        from services.mark_assignment import assign_handicap_marks
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        comp = _make_pro_competitor(db_session, tournament, 'NoWood2', 'M', strathmark_id='NWOOD2M')
        _make_result(db_session, event, comp)

        with patch('services.mark_assignment._get_handicap_calculator') as mock_factory:
            assign_handicap_marks(event)
            mock_factory.assert_not_called()


# ---------------------------------------------------------------------------
# assign_handicap_marks() — calculator failure paths
# ---------------------------------------------------------------------------

class TestAssignHandicapMarksCalculatorFailures:
    """Test graceful failure when STRATHMARK is unavailable or broken."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._build_wood_profile', return_value=MagicMock())
    @patch('services.mark_assignment._get_handicap_calculator', return_value=None)
    def test_calculator_init_failure(self, mock_calc, mock_wood, db_session, tournament):
        """If HandicapCalculator cannot be created, returns status='error'."""
        from services.mark_assignment import assign_handicap_marks
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        _make_wood_for_event(db_session, event)
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
# Helpers for mocking the new V2.7.x batched calculator pipeline
# ---------------------------------------------------------------------------

def _mock_calc_returning(mark_results):
    """Build a MagicMock HandicapCalculator whose .calculate() returns *mark_results*."""
    calc = MagicMock()
    calc.calculate.return_value = mark_results
    return calc


def _mark_result(name: str, mark, predicted: float = 0.0, method: str = 'mock'):
    """Build a MagicMock that quacks like strathmark.predictor.MarkResult."""
    mr = MagicMock()
    mr.name = name
    mr.mark = mark
    mr.predicted_time = predicted
    mr.method_used = method
    return mr


# ---------------------------------------------------------------------------
# assign_handicap_marks() — successful mark assignment (batched pipeline)
# ---------------------------------------------------------------------------

class TestAssignHandicapMarksSuccess:
    """Test the happy path: marks are fetched and stored on EventResult.

    Tests mock _get_handicap_calculator to return a calculator whose
    .calculate() yields a per-test list of MarkResult mocks.  The pipeline
    matches each MarkResult back to its EventResult by competitor_name and
    writes both .mark (→ handicap_factor) and .predicted_time.
    """

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._pull_global_results_df', return_value=None)
    @patch('services.mark_assignment._get_handicap_calculator')
    def test_marks_stored_on_event_results(self, mock_calc_factory, mock_pull, db_session, tournament):
        """handicap_factor and predicted_time are written from MarkResult mocks."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        _make_wood_for_event(db_session, event)
        comp1 = _make_pro_competitor(db_session, tournament, 'Alice', 'F', strathmark_id='AALICEF')
        comp2 = _make_pro_competitor(db_session, tournament, 'Bob', 'M', strathmark_id='BBOBM')
        r1 = _make_result(db_session, event, comp1)
        r2 = _make_result(db_session, event, comp2)

        mock_calc_factory.return_value = _mock_calc_returning([
            _mark_result('Alice', mark=5, predicted=12.5),
            _mark_result('Bob', mark=8, predicted=18.0),
        ])

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['status'] == 'ok'
        assert result['assigned'] == 2
        assert result['skipped'] == 0
        assert result['errors'] == []

        assert r1.handicap_factor == 5.0
        assert r1.predicted_time == 12.5
        assert r2.handicap_factor == 8.0
        assert r2.predicted_time == 18.0

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._pull_global_results_df', return_value=None)
    @patch('services.mark_assignment._get_handicap_calculator')
    def test_zero_predicted_time_preserved(self, mock_calc_factory, mock_pull, db_session, tournament):
        """A MarkResult with predicted_time == 0.0 is stored as 0.0, not None.

        This is the Bug 2 fix: an `if mr.predicted_time` falsy check would
        lose an exact 0.0, which is a valid (if unlikely) prediction.
        """
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        _make_wood_for_event(db_session, event)
        comp = _make_pro_competitor(db_session, tournament, 'Charlie', 'M', strathmark_id='CCHARLM')
        r = _make_result(db_session, event, comp)

        mock_calc_factory.return_value = _mock_calc_returning([
            _mark_result('Charlie', mark=3, predicted=0.0),
        ])

        from services.mark_assignment import assign_handicap_marks
        assign_handicap_marks(event)

        assert r.handicap_factor == 3.0
        assert r.predicted_time == 0.0  # NOT None

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._pull_global_results_df', return_value=None)
    @patch('services.mark_assignment._get_handicap_calculator')
    def test_different_marks_per_competitor(self, mock_calc_factory, mock_pull, db_session, tournament):
        """Different competitors can receive different start marks."""
        event = _make_event(db_session, tournament,
                            stand_type='standing_block', scoring_type='time', is_handicap=True,
                            name='Standing Block Speed')
        _make_wood_for_event(db_session, event)
        comp1 = _make_pro_competitor(db_session, tournament, 'Dan', 'M', strathmark_id='DDANM')
        comp2 = _make_pro_competitor(db_session, tournament, 'Eve', 'F', strathmark_id='EEVEF')
        r1 = _make_result(db_session, event, comp1)
        r2 = _make_result(db_session, event, comp2)

        mock_calc_factory.return_value = _mock_calc_returning([
            _mark_result('Dan', mark=4, predicted=15.0),
            _mark_result('Eve', mark=8, predicted=22.5),
        ])

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['assigned'] == 2
        assert r1.handicap_factor == 4.0
        assert r2.handicap_factor == 8.0

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._pull_global_results_df', return_value=None)
    @patch('services.mark_assignment._get_handicap_calculator')
    def test_calculator_value_error_returns_error_status(self, mock_calc_factory, mock_pull, db_session, tournament):
        """STRATHMARK rejecting the input (e.g. unknown event_code) → status='error'."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        _make_wood_for_event(db_session, event)
        comp = _make_pro_competitor(db_session, tournament, 'Vince', 'M', strathmark_id='VVINCEM')
        _make_result(db_session, event, comp)

        bad_calc = MagicMock()
        bad_calc.calculate.side_effect = ValueError("Invalid event_code: 'XX'")
        mock_calc_factory.return_value = bad_calc

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['status'] == 'error'
        assert result['assigned'] == 0
        assert result['skipped'] == 1
        assert any('STRATHMARK rejected input' in e for e in result['errors'])


# ---------------------------------------------------------------------------
# assign_handicap_marks() — skip/partial paths
# ---------------------------------------------------------------------------

class TestAssignHandicapMarksSkipPaths:
    """Test skipping for competitors without strathmark_id or no calculator match."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._pull_global_results_df', return_value=None)
    @patch('services.mark_assignment._get_handicap_calculator')
    def test_competitor_not_in_calculator_output_skipped(self, mock_calc_factory, mock_pull, db_session, tournament):
        """When the calculator returns no MarkResult for a competitor (e.g.
        the panel-mark fallback dropped them), that competitor is skipped
        and their handicap_factor stays at the DB default of 0.0 (scratch)."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        _make_wood_for_event(db_session, event)
        comp_with = _make_pro_competitor(db_session, tournament, 'Frank', 'M', strathmark_id='FFRANKM')
        comp_without = _make_pro_competitor(db_session, tournament, 'Grace', 'F')
        r_with = _make_result(db_session, event, comp_with)
        r_without = _make_result(db_session, event, comp_without)

        # The mock calculator only returns a result for Frank.  Grace's row
        # will not match by name and should be counted as skipped.
        mock_calc_factory.return_value = _mock_calc_returning([
            _mark_result('Frank', mark=5, predicted=14.0),
        ])

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['assigned'] == 1
        assert result['skipped'] == 1
        assert r_with.handicap_factor == 5.0
        assert r_without.handicap_factor == 0.0  # untouched DB default (scratch)

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._pull_global_results_df', return_value=None)
    @patch('services.mark_assignment._get_handicap_calculator')
    def test_empty_calculator_output_skips_all(self, mock_calc_factory, mock_pull, db_session, tournament):
        """When calculator.calculate() returns [], every competitor is skipped."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        _make_wood_for_event(db_session, event)
        comp = _make_pro_competitor(db_session, tournament, 'Hank', 'M', strathmark_id='HHANKM')
        r = _make_result(db_session, event, comp)

        mock_calc_factory.return_value = _mock_calc_returning([])

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['assigned'] == 0
        assert result['skipped'] == 1
        assert r.handicap_factor == 0.0  # untouched DB default (scratch)

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._pull_global_results_df', return_value=None)
    @patch('services.mark_assignment._get_handicap_calculator')
    def test_no_results_returns_ok_zero(self, mock_calc_factory, mock_pull, db_session, tournament):
        """Event with no EventResult rows short-circuits before the calculator."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        _make_wood_for_event(db_session, event)

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['status'] == 'ok'
        assert result['assigned'] == 0
        assert result['skipped'] == 0
        # The calculator factory should not be called when there are no rows.
        mock_calc_factory.assert_not_called()

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._pull_global_results_df', return_value=None)
    @patch('services.mark_assignment._get_handicap_calculator')
    def test_scratched_results_excluded(self, mock_calc_factory, mock_pull, db_session, tournament):
        """Results with status='scratched' are filtered out before the calculator."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        _make_wood_for_event(db_session, event)
        comp = _make_pro_competitor(db_session, tournament, 'Ivy', 'F', strathmark_id='IIVYF')
        _make_result(db_session, event, comp, status='scratched')

        from services.mark_assignment import assign_handicap_marks
        result = assign_handicap_marks(event)

        assert result['assigned'] == 0
        assert result['skipped'] == 0
        mock_calc_factory.assert_not_called()


# ---------------------------------------------------------------------------
# assign_handicap_marks() — audit logging
# ---------------------------------------------------------------------------

class TestAssignHandicapMarksLogging:
    """Verify that assign_handicap_marks logs mark assignment activity."""

    _LOGGER_NAME = 'services.mark_assignment'

    def _enable_logger(self):
        """Force the logger to be enabled and at INFO level.

        Flask/pytest may disable loggers or set root to WARNING during test
        setup.  We need INFO records to flow for these assertions.
        """
        lgr = logging.getLogger(self._LOGGER_NAME)
        lgr.disabled = False
        lgr.setLevel(logging.INFO)

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._pull_global_results_df', return_value=None)
    @patch('services.mark_assignment._get_handicap_calculator')
    def test_info_log_on_successful_marks(self, mock_calc_factory, mock_pull, db_session, tournament, caplog):
        """An INFO log line with the assigned/skipped counts is emitted."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        _make_wood_for_event(db_session, event)
        comp = _make_pro_competitor(db_session, tournament, 'Jack', 'M', strathmark_id='JJACKM')
        _make_result(db_session, event, comp)

        mock_calc_factory.return_value = _mock_calc_returning([
            _mark_result('Jack', mark=3, predicted=10.0),
        ])

        from services.mark_assignment import assign_handicap_marks

        self._enable_logger()
        with caplog.at_level(logging.INFO, logger=self._LOGGER_NAME):
            assign_handicap_marks(event)

        assert any('assigned=1' in msg and 'skipped=0' in msg for msg in caplog.messages)

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://fake.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'fake-key',
    })
    @patch('services.mark_assignment._pull_global_results_df', return_value=None)
    @patch('services.mark_assignment._get_handicap_calculator')
    def test_info_log_zero_marks(self, mock_calc_factory, mock_pull, db_session, tournament, caplog):
        """Even with zero marks assigned, the INFO log line is emitted."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        _make_wood_for_event(db_session, event)
        comp = _make_pro_competitor(db_session, tournament, 'Kay', 'F', strathmark_id='KKAYF')
        _make_result(db_session, event, comp)

        mock_calc_factory.return_value = _mock_calc_returning([])  # no matches

        from services.mark_assignment import assign_handicap_marks

        self._enable_logger()
        with caplog.at_level(logging.INFO, logger=self._LOGGER_NAME):
            assign_handicap_marks(event)

        assert any('assigned=0' in msg and 'skipped=1' in msg for msg in caplog.messages)

    @patch.dict('os.environ', {'STRATHMARK_SUPABASE_URL': '', 'STRATHMARK_SUPABASE_KEY': ''})
    def test_info_log_when_unconfigured(self, db_session, tournament, caplog):
        """Unconfigured STRATHMARK logs an INFO message about skipping."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)

        from services.mark_assignment import assign_handicap_marks

        self._enable_logger()
        with caplog.at_level(logging.INFO, logger=self._LOGGER_NAME):
            assign_handicap_marks(event)

        assert any('STRATHMARK not configured' in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# parse_marks_csv() — CSV upload parser unit tests (Fix 3)
# ---------------------------------------------------------------------------

class _FakeUpload:
    """Minimal stand-in for werkzeug.FileStorage in unit tests."""

    def __init__(self, content, filename='marks.csv'):
        if isinstance(content, str):
            content = content.encode('utf-8')
        self._content = content
        self.filename = filename

    def read(self):
        return self._content


class TestParseMarksCSV:
    """Tests for the offline pre-computed-marks CSV parser."""

    def _results(self, db_session, tournament):
        """Build two pending EventResult rows on a handicap event."""
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        c1 = _make_pro_competitor(db_session, tournament, 'Alice Smith', 'F')
        c2 = _make_pro_competitor(db_session, tournament, 'Bob Jones', 'M')
        r1 = _make_result(db_session, event, c1)
        r2 = _make_result(db_session, event, c2)
        return event, [r1, r2]

    def test_happy_path_name_match(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        _event, results = self._results(db_session, tournament)
        csv_text = 'competitor_name,proposed_mark\nAlice Smith,4.5\nBob Jones,7.0\n'
        rows, errors = parse_marks_csv(_FakeUpload(csv_text), results)
        assert errors == []
        assert len(rows) == 2
        assert rows[0]['matched_result_id'] == results[0].id
        assert rows[0]['proposed_mark'] == 4.5
        assert rows[0]['warning'] is None
        assert rows[1]['matched_result_id'] == results[1].id
        assert rows[1]['proposed_mark'] == 7.0

    def test_id_match_takes_precedence(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        _event, results = self._results(db_session, tournament)
        csv_text = f'competitor_id,proposed_mark\n{results[0].competitor_id},3.0\n'
        rows, errors = parse_marks_csv(_FakeUpload(csv_text), results)
        assert errors == []
        assert rows[0]['matched_result_id'] == results[0].id
        assert rows[0]['proposed_mark'] == 3.0

    def test_unknown_name_warning(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        _event, results = self._results(db_session, tournament)
        csv_text = 'competitor_name,proposed_mark\nGhost Person,5.0\n'
        rows, errors = parse_marks_csv(_FakeUpload(csv_text), results)
        assert errors == []
        assert rows[0]['matched_result_id'] is None
        assert 'no competitor' in rows[0]['warning']

    def test_ambiguous_name_warning(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        # Two competitors with the same normalised name
        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        c1 = _make_pro_competitor(db_session, tournament, 'John Doe', 'M')
        c2 = _make_pro_competitor(db_session, tournament, 'john  doe', 'M')
        r1 = _make_result(db_session, event, c1)
        r2 = _make_result(db_session, event, c2)
        csv_text = 'competitor_name,proposed_mark\nJohn Doe,2.0\n'
        rows, errors = parse_marks_csv(_FakeUpload(csv_text), [r1, r2])
        assert errors == []
        assert rows[0]['matched_result_id'] is None
        assert 'ambiguous' in rows[0]['warning'] or 'matches 2' in rows[0]['warning']

    def test_invalid_mark_value(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        _event, results = self._results(db_session, tournament)
        csv_text = 'competitor_name,proposed_mark\nAlice Smith,not_a_number\n'
        rows, errors = parse_marks_csv(_FakeUpload(csv_text), results)
        assert errors == []
        assert rows[0]['matched_result_id'] == results[0].id
        assert rows[0]['proposed_mark'] is None
        assert 'invalid mark' in rows[0]['warning']

    def test_negative_mark_clamped(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        _event, results = self._results(db_session, tournament)
        csv_text = 'competitor_name,proposed_mark\nAlice Smith,-2.5\n'
        rows, errors = parse_marks_csv(_FakeUpload(csv_text), results)
        assert rows[0]['proposed_mark'] == 0.0
        assert 'clamping to 0' in rows[0]['warning']

    def test_missing_mark_column(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        _event, results = self._results(db_session, tournament)
        csv_text = 'competitor_name,nope\nAlice Smith,4.5\n'
        rows, errors = parse_marks_csv(_FakeUpload(csv_text), results)
        assert any('proposed_mark' in e for e in errors)
        assert rows == []

    def test_missing_name_and_id_columns(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        _event, results = self._results(db_session, tournament)
        csv_text = 'foo,proposed_mark\nbar,4.5\n'
        rows, errors = parse_marks_csv(_FakeUpload(csv_text), results)
        assert any('competitor_name' in e or 'competitor_id' in e for e in errors)

    def test_empty_file(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        _event, results = self._results(db_session, tournament)
        rows, errors = parse_marks_csv(_FakeUpload(b''), results)
        assert any('empty' in e.lower() for e in errors)

    def test_alternate_column_names(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        _event, results = self._results(db_session, tournament)
        # Use 'name' + 'mark' instead of canonical column names
        csv_text = 'name,mark\nAlice Smith,4.5\n'
        rows, errors = parse_marks_csv(_FakeUpload(csv_text), results)
        assert errors == []
        assert rows[0]['proposed_mark'] == 4.5
        assert rows[0]['matched_result_id'] == results[0].id

    def test_utf8_bom_tolerated(self, db_session, tournament):
        from services.mark_assignment import parse_marks_csv
        _event, results = self._results(db_session, tournament)
        # Some Excel exports prefix with a UTF-8 BOM
        bom = '\ufeff'
        csv_text = bom + 'competitor_name,proposed_mark\nAlice Smith,4.5\n'
        rows, errors = parse_marks_csv(_FakeUpload(csv_text), results)
        assert errors == []
        assert rows[0]['proposed_mark'] == 4.5


# ---------------------------------------------------------------------------
# Cascade degradation smoke test (Fix 4)
# ---------------------------------------------------------------------------
#
# Verifies that when both Ollama AND Gemini are unreachable, the assign_marks
# route still returns a 200 with a functional preview table.  Race-day Railway
# scenario: no Ollama on the host, no GEMINI_API_KEY set, STRATHMARK package
# absent or unconfigured.

class TestAssignMarksRouteCascadeDegradation:
    """End-to-end smoke for the assign-marks route under no-LLM conditions."""

    def _logged_in_client(self, app, db_session):
        """Create a judge user and return a test client with their session set.

        This file defines its own ``app`` fixture (module-scoped temp DB), so
        the conftest ``auth_client`` fixture cannot be used directly.  Each
        call generates a unique username because db_session.commit() inside
        the test flushes through the nested savepoint and the user can leak
        to sibling tests in the module.
        """
        import uuid

        from models.user import User
        username = f'cascade_judge_{uuid.uuid4().hex[:8]}'
        u = User(username=username, role='judge')
        u.set_password('judgepass')
        db_session.add(u)
        db_session.commit()
        c = app.test_client()
        with c.session_transaction() as sess:
            sess['_user_id'] = str(u.id)
        return c

    def test_get_returns_200_when_strathmark_unconfigured(self, app, db_session, tournament):
        """The page must render even with STRATHMARK_SUPABASE_* unset."""
        import os

        # Make sure no STRATHMARK env vars are leaking from the test process.
        for key in (
            'STRATHMARK_SUPABASE_URL',
            'STRATHMARK_SUPABASE_KEY',
            'STRATHMARK_OLLAMA_URL',
            'OLLAMA_HOST',
            'GEMINI_API_KEY',
        ):
            os.environ.pop(key, None)

        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        comp = _make_pro_competitor(db_session, tournament, 'Race Day', 'M')
        _make_result(db_session, event, comp)
        db_session.commit()

        client = self._logged_in_client(app, db_session)
        url = f'/scheduling/{tournament.id}/events/{event.id}/assign-marks'
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'Assign Handicap Marks' in body
        assert 'Race Day' in body
        # CSV upload card must be present even when STRATHMARK is unconfigured.
        assert 'Upload Pre-Computed Marks' in body

    def test_csv_upload_round_trip_no_llm(self, app, db_session, tournament):
        """A judge can upload a CSV and confirm the marks even with no LLM access."""
        import io
        import os

        for key in (
            'STRATHMARK_SUPABASE_URL',
            'STRATHMARK_SUPABASE_KEY',
            'STRATHMARK_OLLAMA_URL',
            'OLLAMA_HOST',
            'GEMINI_API_KEY',
        ):
            os.environ.pop(key, None)

        event = _make_event(db_session, tournament,
                            stand_type='underhand', scoring_type='time', is_handicap=True)
        comp = _make_pro_competitor(db_session, tournament, 'CSV Carl', 'M')
        result = _make_result(db_session, event, comp)
        db_session.commit()

        client = self._logged_in_client(app, db_session)
        url = f'/scheduling/{tournament.id}/events/{event.id}/assign-marks'

        csv_bytes = b'competitor_name,proposed_mark\nCSV Carl,6.5\n'
        upload = (io.BytesIO(csv_bytes), 'marks.csv')

        # Step 1: upload + preview
        resp = client.post(
            url,
            data={'action': 'upload_csv', 'marks_csv': upload},
            content_type='multipart/form-data',
            follow_redirects=False,
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
        body = resp.get_data(as_text=True)
        assert 'CSV Preview' in body
        assert 'CSV Carl' in body

        # Step 2: confirm — write the mark via the per-result form field
        resp = client.post(
            url,
            data={
                'action': 'confirm_csv',
                f'mark_{result.id}': '6.5',
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302  # redirect back to the GET page

        # Refresh from DB and confirm the mark was written.
        from models.event import EventResult
        refreshed = db_session.get(EventResult, result.id)
        assert refreshed.handicap_factor == 6.5

    def test_get_under_5s_when_ollama_and_gemini_dead(self, app, db_session, tournament):
        """Race-day budget: GET must complete fast even with no LLM tier."""
        import os
        import time

        for key in ('STRATHMARK_OLLAMA_URL', 'OLLAMA_HOST', 'GEMINI_API_KEY'):
            os.environ.pop(key, None)
        # Force OLLAMA_HOST=disabled so the strathmark llm module short-circuits
        # without ever attempting a socket connection.
        os.environ['OLLAMA_HOST'] = 'disabled'
        try:
            event = _make_event(db_session, tournament,
                                stand_type='underhand', scoring_type='time', is_handicap=True)
            comp = _make_pro_competitor(db_session, tournament, 'Speedy', 'M')
            _make_result(db_session, event, comp)
            db_session.commit()

            client = self._logged_in_client(app, db_session)
            url = f'/scheduling/{tournament.id}/events/{event.id}/assign-marks'
            t0 = time.monotonic()
            resp = client.get(url)
            elapsed = time.monotonic() - t0
            assert resp.status_code == 200
            assert elapsed < 5.0, f'assign-marks GET took {elapsed:.2f}s (race-day budget is 5s)'
        finally:
            os.environ.pop('OLLAMA_HOST', None)
