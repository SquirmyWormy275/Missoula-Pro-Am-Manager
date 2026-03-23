"""
STRATHMARK sync integration tests — ID generation, enrollment, result push,
college name-match resolution, prediction residuals, and error handling.

Covers all public functions in services/strathmark_sync.py with mocked
external calls (push_competitors, push_results, pull_competitors,
record_prediction_residuals) and real DB fixtures for ORM-dependent paths.

Run:
    pytest tests/test_strathmark_sync.py -v

Requirements:
    pytest (pip install pytest)
    All app dependencies installed.
"""
import json
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
    os.environ.setdefault('SECRET_KEY', 'test-secret-strathmark')
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
    t = Tournament(name='Strathmark Test 2026', year=2026, status='setup')
    db_session.add(t)
    db_session.flush()
    return t


@pytest.fixture()
def sb_event_pro(db_session, tournament):
    """Create a pro Standing Block event (SB)."""
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name='Standing Block',
        event_type='pro',
        gender='M',
        scoring_type='time',
        stand_type='standing_block',
    )
    db_session.add(e)
    db_session.flush()
    return e


@pytest.fixture()
def uh_event_pro(db_session, tournament):
    """Create a pro Underhand event (UH)."""
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name='Underhand',
        event_type='pro',
        gender='M',
        scoring_type='time',
        stand_type='underhand',
    )
    db_session.add(e)
    db_session.flush()
    return e


@pytest.fixture()
def hot_saw_event(db_session, tournament):
    """Create a pro Hot Saw event (not SB/UH — should be skipped)."""
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name='Hot Saw',
        event_type='pro',
        gender=None,
        scoring_type='time',
        stand_type='hot_saw',
    )
    db_session.add(e)
    db_session.flush()
    return e


@pytest.fixture()
def college_sb_speed(db_session, tournament):
    """Create a college Standing Block Speed event."""
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name='Standing Block Speed',
        event_type='college',
        gender='M',
        scoring_type='time',
        stand_type='standing_block',
    )
    db_session.add(e)
    db_session.flush()
    return e


@pytest.fixture()
def college_uh_hard_hit(db_session, tournament):
    """Create a college Underhand Hard Hit event (not Speed — should not match)."""
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name='Underhand Hard Hit',
        event_type='college',
        gender='M',
        scoring_type='hits',
        stand_type='underhand',
    )
    db_session.add(e)
    db_session.flush()
    return e


def _make_pro(db_session, tournament, name, gender, strathmark_id=None):
    """Helper: create an active ProCompetitor."""
    from models import ProCompetitor
    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status='active',
        strathmark_id=strathmark_id,
    )
    db_session.add(c)
    db_session.flush()
    return c


def _make_college(db_session, tournament, name, gender, strathmark_id=None):
    """Helper: create an active CollegeCompetitor with a team."""
    from models import Team, CollegeCompetitor
    # Reuse existing team or create one
    team = Team.query.filter_by(tournament_id=tournament.id, team_code='TST-A').first()
    if team is None:
        team = Team(
            tournament_id=tournament.id,
            team_code='TST-A',
            school_name='Test University',
            school_abbreviation='TST',
        )
        db_session.add(team)
        db_session.flush()

    c = CollegeCompetitor(
        tournament_id=tournament.id,
        team_id=team.id,
        name=name,
        gender=gender,
        status='active',
        strathmark_id=strathmark_id,
    )
    db_session.add(c)
    db_session.flush()
    return c


def _make_result(db_session, event, competitor, result_value, status='completed',
                 predicted_time=None):
    """Helper: create an EventResult tied to an event and competitor."""
    from models import EventResult
    r = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type=event.event_type,
        competitor_name=competitor.name,
        result_value=result_value,
        status=status,
        predicted_time=predicted_time,
    )
    db_session.add(r)
    db_session.flush()
    return r


def _make_wood_config(db_session, tournament, config_key, species, size_value,
                      size_unit='in'):
    """Helper: create a WoodConfig entry."""
    from models import WoodConfig
    wc = WoodConfig(
        tournament_id=tournament.id,
        config_key=config_key,
        species=species,
        size_value=size_value,
        size_unit=size_unit,
    )
    db_session.add(wc)
    db_session.flush()
    return wc


# ---------------------------------------------------------------------------
# make_strathmark_id — pure function tests
# ---------------------------------------------------------------------------

class TestMakeStrathmarkId:
    """Tests for deterministic ID generation."""

    def test_basic_male(self):
        from services.strathmark_sync import make_strathmark_id
        assert make_strathmark_id('Alex Kaper', 'M') == 'AKAPERM'

    def test_basic_female(self):
        from services.strathmark_sync import make_strathmark_id
        assert make_strathmark_id('Jane Doe', 'F') == 'JDOEF'

    def test_gender_case_insensitive(self):
        from services.strathmark_sync import make_strathmark_id
        assert make_strathmark_id('Alex Kaper', 'm') == 'AKAPERM'
        assert make_strathmark_id('Jane Doe', 'female') == 'JDOEF'

    def test_middle_name_ignored(self):
        from services.strathmark_sync import make_strathmark_id
        # Middle name should be ignored; uses first initial + last name
        assert make_strathmark_id('John Michael Smith', 'M') == 'JSMITHM'

    def test_single_name(self):
        from services.strathmark_sync import make_strathmark_id
        # Single name: first initial = first letter, last name = same word
        result = make_strathmark_id('Prince', 'M')
        assert result == 'PPRINCEM'

    def test_hyphenated_last_name(self):
        from services.strathmark_sync import make_strathmark_id
        result = make_strathmark_id('Mary Smith-Jones', 'F')
        assert result == 'MSMITH-JONESF'

    def test_whitespace_stripped(self):
        from services.strathmark_sync import make_strathmark_id
        result = make_strathmark_id('  Alex Kaper  ', 'M')
        assert result == 'AKAPERM'

    def test_empty_name_raises(self):
        from services.strathmark_sync import make_strathmark_id
        with pytest.raises(ValueError, match='empty name'):
            make_strathmark_id('', 'M')

    def test_whitespace_only_raises(self):
        from services.strathmark_sync import make_strathmark_id
        with pytest.raises(ValueError, match='empty name'):
            make_strathmark_id('   ', 'M')

    def test_collision_handling_appends_suffix(self):
        from services.strathmark_sync import make_strathmark_id
        existing = {'AKAPERM'}
        result = make_strathmark_id('Alex Kaper', 'M', existing_ids=existing)
        assert result == 'AKAPERM2'

    def test_collision_increments_until_unique(self):
        from services.strathmark_sync import make_strathmark_id
        existing = {'AKAPERM', 'AKAPERM2', 'AKAPERM3'}
        result = make_strathmark_id('Alex Kaper', 'M', existing_ids=existing)
        assert result == 'AKAPERM4'

    def test_no_collision_with_empty_set(self):
        from services.strathmark_sync import make_strathmark_id
        result = make_strathmark_id('Alex Kaper', 'M', existing_ids=set())
        assert result == 'AKAPERM'

    def test_no_collision_with_none(self):
        from services.strathmark_sync import make_strathmark_id
        result = make_strathmark_id('Alex Kaper', 'M', existing_ids=None)
        assert result == 'AKAPERM'


# ---------------------------------------------------------------------------
# is_configured — env var check
# ---------------------------------------------------------------------------

class TestIsConfigured:
    """Tests for STRATHMARK env var configuration check."""

    def test_both_vars_set(self):
        from services.strathmark_sync import is_configured
        with patch.dict('os.environ', {
            'STRATHMARK_SUPABASE_URL': 'https://example.supabase.co',
            'STRATHMARK_SUPABASE_KEY': 'secret-key',
        }):
            assert is_configured() is True

    def test_url_missing(self):
        from services.strathmark_sync import is_configured
        env = {'STRATHMARK_SUPABASE_KEY': 'secret-key'}
        with patch.dict('os.environ', env, clear=True):
            assert is_configured() is False

    def test_key_missing(self):
        from services.strathmark_sync import is_configured
        env = {'STRATHMARK_SUPABASE_URL': 'https://example.supabase.co'}
        with patch.dict('os.environ', env, clear=True):
            assert is_configured() is False

    def test_both_missing(self):
        from services.strathmark_sync import is_configured
        with patch.dict('os.environ', {}, clear=True):
            assert is_configured() is False

    def test_empty_string_url(self):
        from services.strathmark_sync import is_configured
        with patch.dict('os.environ', {
            'STRATHMARK_SUPABASE_URL': '',
            'STRATHMARK_SUPABASE_KEY': 'secret-key',
        }):
            assert is_configured() is False

    def test_empty_string_key(self):
        from services.strathmark_sync import is_configured
        with patch.dict('os.environ', {
            'STRATHMARK_SUPABASE_URL': 'https://example.supabase.co',
            'STRATHMARK_SUPABASE_KEY': '',
        }):
            assert is_configured() is False


# ---------------------------------------------------------------------------
# is_college_sb_uh_speed — event name matching
# ---------------------------------------------------------------------------

class TestIsCollegeSbUhSpeed:
    """Tests for college Standing Block / Underhand Speed event detection."""

    def test_standing_block_speed(self, db_session, tournament, college_sb_speed):
        from services.strathmark_sync import is_college_sb_uh_speed
        assert is_college_sb_uh_speed(college_sb_speed) is True

    def test_underhand_speed(self, db_session, tournament):
        from models import Event
        from services.strathmark_sync import is_college_sb_uh_speed
        e = Event(
            tournament_id=tournament.id, name='Underhand Speed',
            event_type='college', gender='M',
            scoring_type='time', stand_type='underhand',
        )
        db_session.add(e)
        db_session.flush()
        assert is_college_sb_uh_speed(e) is True

    def test_sb_speed_abbreviation(self, db_session, tournament):
        from models import Event
        from services.strathmark_sync import is_college_sb_uh_speed
        e = Event(
            tournament_id=tournament.id, name='SB Speed',
            event_type='college', gender='F',
            scoring_type='time', stand_type='standing_block',
        )
        db_session.add(e)
        db_session.flush()
        assert is_college_sb_uh_speed(e) is True

    def test_uh_speed_abbreviation(self, db_session, tournament):
        from models import Event
        from services.strathmark_sync import is_college_sb_uh_speed
        e = Event(
            tournament_id=tournament.id, name='UH Speed',
            event_type='college', gender='M',
            scoring_type='time', stand_type='underhand',
        )
        db_session.add(e)
        db_session.flush()
        assert is_college_sb_uh_speed(e) is True

    def test_case_insensitive(self, db_session, tournament):
        from models import Event
        from services.strathmark_sync import is_college_sb_uh_speed
        e = Event(
            tournament_id=tournament.id, name='standing block speed',
            event_type='college', gender='M',
            scoring_type='time', stand_type='standing_block',
        )
        db_session.add(e)
        db_session.flush()
        assert is_college_sb_uh_speed(e) is True

    def test_hard_hit_not_speed(self, db_session, tournament, college_uh_hard_hit):
        from services.strathmark_sync import is_college_sb_uh_speed
        assert is_college_sb_uh_speed(college_uh_hard_hit) is False

    def test_pro_event_rejected(self, db_session, tournament, sb_event_pro):
        from services.strathmark_sync import is_college_sb_uh_speed
        assert is_college_sb_uh_speed(sb_event_pro) is False

    def test_non_block_event_rejected(self, db_session, tournament):
        from models import Event
        from services.strathmark_sync import is_college_sb_uh_speed
        e = Event(
            tournament_id=tournament.id, name='Speed Climb',
            event_type='college', gender='M',
            scoring_type='time', stand_type='speed_climb',
        )
        db_session.add(e)
        db_session.flush()
        assert is_college_sb_uh_speed(e) is False

    def test_speed_in_name_with_time_scoring(self, db_session, tournament):
        """Event with 'speed' in name + correct stand_type + time scoring matches."""
        from models import Event
        from services.strathmark_sync import is_college_sb_uh_speed
        e = Event(
            tournament_id=tournament.id, name='Custom Underhand Speed Event',
            event_type='college', gender='M',
            scoring_type='time', stand_type='underhand',
        )
        db_session.add(e)
        db_session.flush()
        assert is_college_sb_uh_speed(e) is True


# ---------------------------------------------------------------------------
# _get_wood_for_event — WoodConfig lookup + unit conversion
# ---------------------------------------------------------------------------

class TestGetWoodForEvent:
    """Tests for wood config lookup and inch-to-mm conversion."""

    def test_sb_pro_male_lookup(self, db_session, tournament, sb_event_pro):
        from services.strathmark_sync import _get_wood_for_event
        _make_wood_config(db_session, tournament, 'block_standing_pro_M',
                          'Cottonwood', 12.0, 'in')
        species, size_mm = _get_wood_for_event(sb_event_pro, 'pro')
        assert species == 'Cottonwood'
        assert size_mm == round(12.0 * 25.4, 1)

    def test_uh_pro_male_lookup(self, db_session, tournament, uh_event_pro):
        from services.strathmark_sync import _get_wood_for_event
        _make_wood_config(db_session, tournament, 'block_underhand_pro_M',
                          'Poplar', 14.0, 'in')
        species, size_mm = _get_wood_for_event(uh_event_pro, 'pro')
        assert species == 'Poplar'
        assert size_mm == round(14.0 * 25.4, 1)

    def test_mm_unit_no_conversion(self, db_session, tournament, sb_event_pro):
        from services.strathmark_sync import _get_wood_for_event
        _make_wood_config(db_session, tournament, 'block_standing_pro_M',
                          'Cottonwood', 305.0, 'mm')
        species, size_mm = _get_wood_for_event(sb_event_pro, 'pro')
        assert species == 'Cottonwood'
        assert size_mm == 305.0

    def test_missing_config_returns_none(self, db_session, tournament, sb_event_pro):
        from services.strathmark_sync import _get_wood_for_event
        species, size_mm = _get_wood_for_event(sb_event_pro, 'pro')
        assert species is None
        assert size_mm is None

    def test_unsupported_stand_type_returns_none(self, db_session, tournament, hot_saw_event):
        from services.strathmark_sync import _get_wood_for_event
        species, size_mm = _get_wood_for_event(hot_saw_event, 'pro')
        assert species is None
        assert size_mm is None

    def test_missing_species_returns_none(self, db_session, tournament, sb_event_pro):
        from services.strathmark_sync import _get_wood_for_event
        _make_wood_config(db_session, tournament, 'block_standing_pro_M',
                          None, 12.0, 'in')
        species, size_mm = _get_wood_for_event(sb_event_pro, 'pro')
        assert species is None
        assert size_mm is None

    def test_missing_size_returns_none(self, db_session, tournament, sb_event_pro):
        from services.strathmark_sync import _get_wood_for_event
        _make_wood_config(db_session, tournament, 'block_standing_pro_M',
                          'Cottonwood', None, 'in')
        species, size_mm = _get_wood_for_event(sb_event_pro, 'pro')
        assert species is None
        assert size_mm is None

    def test_college_key_lookup(self, db_session, tournament, college_sb_speed):
        from services.strathmark_sync import _get_wood_for_event
        _make_wood_config(db_session, tournament, 'block_standing_college_M',
                          'Aspen', 11.0, 'in')
        species, size_mm = _get_wood_for_event(college_sb_speed, 'college')
        assert species == 'Aspen'
        assert size_mm == round(11.0 * 25.4, 1)


# ---------------------------------------------------------------------------
# enroll_pro_competitor — mocked external call
# ---------------------------------------------------------------------------

class TestEnrollProCompetitor:
    """Tests for pro competitor STRATHMARK enrollment."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    @patch('services.strathmark_sync.push_competitors', create=True)
    @patch('services.strathmark_sync.pd', create=True)
    def test_successful_enrollment(self, mock_pd, mock_push, db_session, tournament):
        """Enrollment generates ID, calls push_competitors, stores strathmark_id."""
        import importlib
        import services.strathmark_sync as mod

        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M')
        assert comp.strathmark_id is None

        # Patch at the point of import inside the function
        mock_df = MagicMock()
        mock_pd.DataFrame.return_value = mock_df

        with patch.dict('sys.modules', {
            'pandas': mock_pd,
            'strathmark': MagicMock(push_competitors=mock_push),
        }):
            result = mod.enroll_pro_competitor(comp)

        assert result is True
        assert comp.strathmark_id == 'AKAPERM'

    @patch.dict('os.environ', {}, clear=True)
    def test_not_configured_returns_false(self, db_session, tournament):
        from services.strathmark_sync import enroll_pro_competitor
        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M')
        result = enroll_pro_competitor(comp)
        assert result is False
        assert comp.strathmark_id is None

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_import_error_returns_false(self, db_session, tournament):
        """If strathmark package is not installed, enrollment fails gracefully."""
        from services.strathmark_sync import enroll_pro_competitor
        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M')

        # Force ImportError when trying to import strathmark
        import sys
        with patch.dict('sys.modules', {'strathmark': None}):
            result = enroll_pro_competitor(comp)

        assert result is False

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_push_failure_returns_false(self, db_session, tournament):
        """If push_competitors raises, enrollment returns False (non-blocking)."""
        from services.strathmark_sync import enroll_pro_competitor
        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M')

        mock_strathmark = MagicMock()
        mock_strathmark.push_competitors.side_effect = RuntimeError('network error')

        with patch.dict('sys.modules', {
            'strathmark': mock_strathmark,
            'pandas': MagicMock(),
        }):
            result = enroll_pro_competitor(comp)

        assert result is False


# ---------------------------------------------------------------------------
# push_pro_event_results — mocked external call
# ---------------------------------------------------------------------------

class TestPushProEventResults:
    """Tests for pushing finalized pro SB/UH results."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_successful_push(self, db_session, tournament, sb_event_pro):
        from services.strathmark_sync import push_pro_event_results

        _make_wood_config(db_session, tournament, 'block_standing_pro_M',
                          'Cottonwood', 12.0, 'in')

        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M',
                         strathmark_id='AKAPERM')
        _make_result(db_session, sb_event_pro, comp, 15.5)

        mock_push = MagicMock(return_value=1)
        mock_strathmark = MagicMock(
            push_results=mock_push,
            record_prediction_residuals=MagicMock(),
        )

        with patch.dict('sys.modules', {
            'strathmark': mock_strathmark,
            'pandas': pytest.importorskip('pandas'),
        }):
            push_pro_event_results(sb_event_pro, 2026)

        mock_push.assert_called_once()
        call_args = mock_push.call_args
        df = call_args[0][0]
        assert len(df) == 1
        assert df.iloc[0]['CompetitorID'] == 'AKAPERM'
        assert df.iloc[0]['Event'] == 'SB'
        assert df.iloc[0]['Time (seconds)'] == 15.5

    @patch.dict('os.environ', {}, clear=True)
    def test_not_configured_skips(self, db_session, tournament, sb_event_pro):
        from services.strathmark_sync import push_pro_event_results
        # Should return without error
        push_pro_event_results(sb_event_pro, 2026)

    def test_non_sb_uh_event_skips(self, db_session, tournament, hot_saw_event):
        """Hot Saw has no event_code mapping — push should exit early."""
        from services.strathmark_sync import push_pro_event_results
        with patch.dict('os.environ', {
            'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
            'STRATHMARK_SUPABASE_KEY': 'key',
        }):
            # Should not raise
            push_pro_event_results(hot_saw_event, 2026)

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_missing_wood_config_skips(self, db_session, tournament, sb_event_pro):
        """No WoodConfig for the event — push should log warning and return."""
        from services.strathmark_sync import push_pro_event_results
        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M',
                         strathmark_id='AKAPERM')
        _make_result(db_session, sb_event_pro, comp, 15.5)
        # No wood config created — should skip without error
        push_pro_event_results(sb_event_pro, 2026)

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_competitor_without_strathmark_id_skipped(self, db_session, tournament,
                                                      sb_event_pro):
        """Competitor without strathmark_id is not included in push."""
        from services.strathmark_sync import push_pro_event_results

        _make_wood_config(db_session, tournament, 'block_standing_pro_M',
                          'Cottonwood', 12.0, 'in')

        comp = _make_pro(db_session, tournament, 'No ID Guy', 'M',
                         strathmark_id=None)
        _make_result(db_session, sb_event_pro, comp, 15.5)

        mock_push = MagicMock(return_value=0)
        mock_strathmark = MagicMock(
            push_results=mock_push,
            record_prediction_residuals=MagicMock(),
        )

        with patch.dict('sys.modules', {
            'strathmark': mock_strathmark,
            'pandas': pytest.importorskip('pandas'),
        }):
            push_pro_event_results(sb_event_pro, 2026)

        # No rows to push — push_results should NOT have been called
        mock_push.assert_not_called()

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_push_failure_non_blocking(self, db_session, tournament, sb_event_pro):
        """push_results raising does not propagate — non-blocking."""
        from services.strathmark_sync import push_pro_event_results

        _make_wood_config(db_session, tournament, 'block_standing_pro_M',
                          'Cottonwood', 12.0, 'in')

        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M',
                         strathmark_id='AKAPERM')
        _make_result(db_session, sb_event_pro, comp, 15.5)

        mock_strathmark = MagicMock()
        mock_strathmark.push_results.side_effect = RuntimeError('Supabase down')
        mock_strathmark.record_prediction_residuals = MagicMock()

        with patch.dict('sys.modules', {
            'strathmark': mock_strathmark,
            'pandas': pytest.importorskip('pandas'),
        }):
            # Should not raise
            push_pro_event_results(sb_event_pro, 2026)

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_null_result_value_excluded(self, db_session, tournament, sb_event_pro):
        """Results with null result_value should be excluded from push."""
        from services.strathmark_sync import push_pro_event_results

        _make_wood_config(db_session, tournament, 'block_standing_pro_M',
                          'Cottonwood', 12.0, 'in')

        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M',
                         strathmark_id='AKAPERM')
        _make_result(db_session, sb_event_pro, comp, None)

        mock_push = MagicMock(return_value=0)
        mock_strathmark = MagicMock(
            push_results=mock_push,
            record_prediction_residuals=MagicMock(),
        )

        with patch.dict('sys.modules', {
            'strathmark': mock_strathmark,
            'pandas': pytest.importorskip('pandas'),
        }):
            push_pro_event_results(sb_event_pro, 2026)

        mock_push.assert_not_called()


# ---------------------------------------------------------------------------
# push_college_event_results — mocked external calls
# ---------------------------------------------------------------------------

class TestPushCollegeEventResults:
    """Tests for pushing finalized college Speed results with name-match."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_existing_strathmark_id_used(self, db_session, tournament, college_sb_speed):
        """College competitor with an existing strathmark_id skips pull_competitors."""
        from services.strathmark_sync import push_college_event_results
        import pandas as pd

        _make_wood_config(db_session, tournament, 'block_standing_college_M',
                          'Aspen', 11.0, 'in')

        comp = _make_college(db_session, tournament, 'Alice Pro', 'M',
                             strathmark_id='APROF')
        _make_result(db_session, college_sb_speed, comp, 18.2)

        mock_push = MagicMock(return_value=1)
        mock_pull = MagicMock()
        mock_strathmark = MagicMock(
            push_results=mock_push,
            pull_competitors=mock_pull,
        )

        with patch.dict('sys.modules', {
            'strathmark': mock_strathmark,
            'pandas': pd,
        }):
            push_college_event_results(college_sb_speed, 2026)

        mock_push.assert_called_once()
        # pull_competitors should not have been called — existing ID
        mock_pull.assert_not_called()

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_name_match_resolves_id(self, db_session, tournament, college_sb_speed):
        """College competitor without strathmark_id gets matched by name."""
        from services.strathmark_sync import push_college_event_results
        import pandas as pd

        _make_wood_config(db_session, tournament, 'block_standing_college_M',
                          'Aspen', 11.0, 'in')

        comp = _make_college(db_session, tournament, 'Alice Pro', 'M',
                             strathmark_id=None)
        _make_result(db_session, college_sb_speed, comp, 18.2)

        # Mock pull_competitors to return a DataFrame with the match
        global_df = pd.DataFrame([{
            'CompetitorID': 'APROF',
            'Name': 'Alice Pro',
        }])
        mock_push = MagicMock(return_value=1)
        mock_pull = MagicMock(return_value=global_df)
        mock_strathmark = MagicMock(
            push_results=mock_push,
            pull_competitors=mock_pull,
        )

        with patch.dict('sys.modules', {
            'strathmark': mock_strathmark,
            'pandas': pd,
        }):
            push_college_event_results(college_sb_speed, 2026)

        mock_push.assert_called_once()
        # The competitor should now have the strathmark_id set locally
        assert comp.strathmark_id == 'APROF'

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_no_name_match_skipped(self, db_session, tournament, college_sb_speed):
        """College competitor not found in global DB is skipped."""
        from services.strathmark_sync import push_college_event_results
        import pandas as pd

        _make_wood_config(db_session, tournament, 'block_standing_college_M',
                          'Aspen', 11.0, 'in')

        comp = _make_college(db_session, tournament, 'Unknown Competitor', 'M',
                             strathmark_id=None)
        _make_result(db_session, college_sb_speed, comp, 18.2)

        global_df = pd.DataFrame([{
            'CompetitorID': 'APROF',
            'Name': 'Alice Pro',
        }])
        mock_push = MagicMock(return_value=0)
        mock_pull = MagicMock(return_value=global_df)
        mock_strathmark = MagicMock(
            push_results=mock_push,
            pull_competitors=mock_pull,
        )

        with patch.dict('sys.modules', {
            'strathmark': mock_strathmark,
            'pandas': pd,
        }):
            push_college_event_results(college_sb_speed, 2026)

        # No rows to push — push_results not called
        mock_push.assert_not_called()

    @patch.dict('os.environ', {}, clear=True)
    def test_not_configured_skips(self, db_session, tournament, college_sb_speed):
        from services.strathmark_sync import push_college_event_results
        # Should return without error
        push_college_event_results(college_sb_speed, 2026)

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_push_failure_non_blocking(self, db_session, tournament, college_sb_speed):
        """push_results raising does not propagate."""
        from services.strathmark_sync import push_college_event_results
        import pandas as pd

        _make_wood_config(db_session, tournament, 'block_standing_college_M',
                          'Aspen', 11.0, 'in')

        comp = _make_college(db_session, tournament, 'Alice Pro', 'M',
                             strathmark_id='APROF')
        _make_result(db_session, college_sb_speed, comp, 18.2)

        mock_strathmark = MagicMock()
        mock_strathmark.push_results.side_effect = RuntimeError('boom')

        with patch.dict('sys.modules', {
            'strathmark': mock_strathmark,
            'pandas': pd,
        }):
            # Should not raise
            push_college_event_results(college_sb_speed, 2026)


# ---------------------------------------------------------------------------
# _record_prediction_residuals_for_pro_event
# ---------------------------------------------------------------------------

class TestRecordPredictionResiduals:
    """Tests for prediction residual recording after pro event finalization."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_residuals_recorded_when_predicted_time_present(self, db_session, tournament,
                                                            sb_event_pro):
        from services.strathmark_sync import _record_prediction_residuals_for_pro_event

        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M',
                         strathmark_id='AKAPERM')
        _make_result(db_session, sb_event_pro, comp, 15.5, predicted_time=14.0)

        mock_record = MagicMock()
        mock_strathmark = MagicMock(record_prediction_residuals=mock_record)

        with patch.dict('sys.modules', {'strathmark': mock_strathmark}):
            _record_prediction_residuals_for_pro_event(sb_event_pro, 'SB')

        mock_record.assert_called_once()
        call_kwargs = mock_record.call_args[1]
        assert call_kwargs['predicted'] == {'AKAPERM': 14.0}
        assert call_kwargs['actual'] == {'AKAPERM': 15.5}
        assert call_kwargs['event_code'] == 'SB'

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_no_predicted_time_skips_all(self, db_session, tournament, sb_event_pro):
        """When predicted_time is None for all, record_prediction_residuals is not called."""
        from services.strathmark_sync import _record_prediction_residuals_for_pro_event

        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M',
                         strathmark_id='AKAPERM')
        _make_result(db_session, sb_event_pro, comp, 15.5, predicted_time=None)

        mock_record = MagicMock()
        mock_strathmark = MagicMock(record_prediction_residuals=mock_record)

        with patch.dict('sys.modules', {'strathmark': mock_strathmark}):
            _record_prediction_residuals_for_pro_event(sb_event_pro, 'SB')

        mock_record.assert_not_called()

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_mixed_predicted_times(self, db_session, tournament, sb_event_pro):
        """Only competitors with predicted_time are included in residuals."""
        from services.strathmark_sync import _record_prediction_residuals_for_pro_event

        comp1 = _make_pro(db_session, tournament, 'Alex Kaper', 'M',
                          strathmark_id='AKAPERM')
        comp2 = _make_pro(db_session, tournament, 'Bob Smith', 'M',
                          strathmark_id='BSMITHM')
        _make_result(db_session, sb_event_pro, comp1, 15.5, predicted_time=14.0)
        _make_result(db_session, sb_event_pro, comp2, 16.0, predicted_time=None)

        mock_record = MagicMock()
        mock_strathmark = MagicMock(record_prediction_residuals=mock_record)

        with patch.dict('sys.modules', {'strathmark': mock_strathmark}):
            _record_prediction_residuals_for_pro_event(sb_event_pro, 'SB')

        mock_record.assert_called_once()
        call_kwargs = mock_record.call_args[1]
        assert 'AKAPERM' in call_kwargs['predicted']
        assert 'BSMITHM' not in call_kwargs['predicted']

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_competitor_without_strathmark_id_skipped(self, db_session, tournament,
                                                      sb_event_pro):
        from services.strathmark_sync import _record_prediction_residuals_for_pro_event

        comp = _make_pro(db_session, tournament, 'No ID Guy', 'M',
                         strathmark_id=None)
        _make_result(db_session, sb_event_pro, comp, 15.5, predicted_time=14.0)

        mock_record = MagicMock()
        mock_strathmark = MagicMock(record_prediction_residuals=mock_record)

        with patch.dict('sys.modules', {'strathmark': mock_strathmark}):
            _record_prediction_residuals_for_pro_event(sb_event_pro, 'SB')

        mock_record.assert_not_called()

    @patch.dict('os.environ', {}, clear=True)
    def test_not_configured_returns_early(self, db_session, tournament, sb_event_pro):
        from services.strathmark_sync import _record_prediction_residuals_for_pro_event
        # Should not raise
        _record_prediction_residuals_for_pro_event(sb_event_pro, 'SB')

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_record_failure_non_blocking(self, db_session, tournament, sb_event_pro):
        """Exception in record_prediction_residuals is caught — non-blocking."""
        from services.strathmark_sync import _record_prediction_residuals_for_pro_event

        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M',
                         strathmark_id='AKAPERM')
        _make_result(db_session, sb_event_pro, comp, 15.5, predicted_time=14.0)

        mock_strathmark = MagicMock()
        mock_strathmark.record_prediction_residuals.side_effect = RuntimeError('DB down')

        with patch.dict('sys.modules', {'strathmark': mock_strathmark}):
            # Should not raise
            _record_prediction_residuals_for_pro_event(sb_event_pro, 'SB')


# ---------------------------------------------------------------------------
# Error handling — non-blocking guarantees
# ---------------------------------------------------------------------------

class TestNonBlockingGuarantees:
    """Cross-cutting tests ensuring all public functions never raise."""

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_enroll_with_corrupted_db_query(self, db_session, tournament):
        """enroll_pro_competitor catches any exception, including DB errors."""
        from services.strathmark_sync import enroll_pro_competitor
        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M')

        with patch('services.strathmark_sync._get_existing_strathmark_ids',
                   side_effect=RuntimeError('DB exploded')):
            result = enroll_pro_competitor(comp)

        assert result is False

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_push_pro_with_strathmark_import_error(self, db_session, tournament,
                                                    sb_event_pro):
        """push_pro_event_results handles missing pandas gracefully."""
        from services.strathmark_sync import push_pro_event_results

        _make_wood_config(db_session, tournament, 'block_standing_pro_M',
                          'Cottonwood', 12.0, 'in')
        comp = _make_pro(db_session, tournament, 'Alex Kaper', 'M',
                         strathmark_id='AKAPERM')
        _make_result(db_session, sb_event_pro, comp, 15.5)

        # Simulate import error for pandas
        with patch.dict('sys.modules', {'pandas': None, 'strathmark': None}):
            # Should not raise
            push_pro_event_results(sb_event_pro, 2026)

    @patch.dict('os.environ', {
        'STRATHMARK_SUPABASE_URL': 'https://x.supabase.co',
        'STRATHMARK_SUPABASE_KEY': 'key',
    })
    def test_push_college_with_pull_failure(self, db_session, tournament, college_sb_speed):
        """pull_competitors failure still allows graceful completion."""
        from services.strathmark_sync import push_college_event_results
        import pandas as pd

        _make_wood_config(db_session, tournament, 'block_standing_college_M',
                          'Aspen', 11.0, 'in')
        comp = _make_college(db_session, tournament, 'Alice Pro', 'M',
                             strathmark_id=None)
        _make_result(db_session, college_sb_speed, comp, 18.2)

        mock_strathmark = MagicMock()
        mock_strathmark.pull_competitors.side_effect = RuntimeError('network error')
        mock_strathmark.push_results = MagicMock(return_value=0)

        with patch.dict('sys.modules', {
            'strathmark': mock_strathmark,
            'pandas': pd,
        }):
            # Should not raise — pull failure is handled inside _global_df()
            push_college_event_results(college_sb_speed, 2026)
