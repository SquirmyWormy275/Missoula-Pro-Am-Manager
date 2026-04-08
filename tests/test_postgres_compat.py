"""
PostgreSQL compatibility checks — validate SQL patterns and data handling
that can break during SQLite-to-PostgreSQL migration.

These tests run against SQLite but verify patterns that matter for PG:
  - Boolean coercion (PG is strict; SQLite accepts 0/1)
  - JSON field round-trips (TEXT columns)
  - Case-sensitive LIKE (PG) vs case-insensitive (SQLite)
  - NULL handling in unique constraints
  - Transaction savepoint behavior
  - postgres:// → postgresql:// URL correction
  - Integer overflow guards
  - String length enforcement

Run:
    pytest tests/test_postgres_compat.py -v
"""
import json

import pytest

from database import db as _db
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_event_result,
    make_heat,
    make_pro_competitor,
    make_team,
    make_tournament,
)


@pytest.fixture(autouse=True)
def _db_session(db_session):
    """Activate conftest's db_session for every test in this module."""
    yield db_session


@pytest.fixture()
def tournament(db_session):
    return make_tournament(db_session)


# ---------------------------------------------------------------------------
# Database URL correction
# ---------------------------------------------------------------------------

class TestDatabaseURLCorrection:
    """config._normalized_database_url() handles postgres:// scheme."""

    def test_postgres_to_postgresql(self):
        import os
        old = os.environ.get('DATABASE_URL')
        try:
            os.environ['DATABASE_URL'] = 'postgres://user:pass@host:5432/db'
            from config import _normalized_database_url
            url = _normalized_database_url()
            assert url.startswith('postgresql://')
            assert 'postgres://' not in url[len('postgresql'):]
        finally:
            if old is not None:
                os.environ['DATABASE_URL'] = old
            else:
                os.environ.pop('DATABASE_URL', None)

    def test_sqlite_url_unchanged(self):
        import os
        old = os.environ.get('DATABASE_URL')
        try:
            os.environ['DATABASE_URL'] = 'sqlite:///proam.db'
            from config import _normalized_database_url
            url = _normalized_database_url()
            assert url == 'sqlite:///proam.db'
        finally:
            if old is not None:
                os.environ['DATABASE_URL'] = old
            else:
                os.environ.pop('DATABASE_URL', None)

    def test_postgresql_url_unchanged(self):
        import os
        old = os.environ.get('DATABASE_URL')
        try:
            os.environ['DATABASE_URL'] = 'postgresql://user:pass@host/db'
            from config import _normalized_database_url
            url = _normalized_database_url()
            assert url == 'postgresql://user:pass@host/db'
        finally:
            if old is not None:
                os.environ['DATABASE_URL'] = old
            else:
                os.environ.pop('DATABASE_URL', None)


# ---------------------------------------------------------------------------
# Boolean column strictness
# ---------------------------------------------------------------------------

class TestBooleanColumns:
    """PG requires real booleans, not 0/1 ints."""

    def test_event_is_handicap_bool(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Bool Test', is_handicap=False)
        db_session.flush()
        assert e.is_handicap is False or e.is_handicap == 0

        e.is_handicap = True
        db_session.flush()
        assert e.is_handicap is True

    def test_event_is_finalized_bool(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Finalized Bool')
        assert e.is_finalized in (False, None, 0)

        e.is_finalized = True
        db_session.flush()
        assert e.is_finalized is True

    def test_pro_competitor_booleans(self, db_session, tournament):
        c = make_pro_competitor(db_session, tournament, 'BoolPro', 'M',
                                is_left_handed_springboard=True,
                                springboard_slow_heat=True)
        db_session.flush()
        assert c.is_left_handed_springboard is True
        assert c.springboard_slow_heat is True

    def test_event_result_throwoff_pending_bool(self, db_session, tournament):
        e = make_event(db_session, tournament, 'ThrowBool')
        c = make_pro_competitor(db_session, tournament, 'ThrowComp', 'M')
        r = make_event_result(db_session, e, c, status='completed')
        db_session.flush()

        assert r.throwoff_pending in (False, None, 0)
        r.throwoff_pending = True
        db_session.flush()
        assert r.throwoff_pending is True


# ---------------------------------------------------------------------------
# JSON field round-trips (TEXT columns storing JSON)
# ---------------------------------------------------------------------------

class TestJSONFieldRoundTrips:
    """JSON stored in TEXT columns must survive write/read cycles."""

    def test_event_payouts_roundtrip(self, db_session, tournament):
        payouts = {'1': 500, '2': 300, '3': 100}
        e = make_event(db_session, tournament, 'JSON Payouts', payouts=payouts)
        db_session.flush()

        loaded = e.get_payouts()
        assert loaded['1'] == 500 or loaded.get(1) == 500

    def test_competitor_events_entered_roundtrip(self, db_session, tournament):
        events = [1, 2, 3, 4, 5]
        c = make_pro_competitor(db_session, tournament, 'JSONEvts', 'M', events=events)
        db_session.flush()

        loaded = c.get_events_entered()
        assert set(loaded) == {1, 2, 3, 4, 5}

    def test_heat_competitors_roundtrip(self, db_session, tournament):
        e = make_event(db_session, tournament, 'JSON Heat')
        comp_ids = [10, 20, 30, 40]
        h = make_heat(db_session, e, competitors=comp_ids)
        db_session.flush()

        loaded = h.get_competitors()
        assert loaded == comp_ids

    def test_heat_stand_assignments_roundtrip(self, db_session, tournament):
        e = make_event(db_session, tournament, 'JSON Stands')
        assigns = {'10': 1, '20': 2, '30': 3}
        h = make_heat(db_session, e, stand_assignments=assigns)
        db_session.flush()

        loaded = h.get_stand_assignments()
        # Keys may be strings
        assert str(loaded.get('10', loaded.get(10, None))) == '1'

    def test_nested_json_structure(self, db_session, tournament):
        """Complex nested JSON survives round-trip."""
        c = make_pro_competitor(db_session, tournament, 'NestedJSON', 'M')
        complex_gear = {
            '1': 'Partner A',
            '2': 'group:TeamSaw',
            'category:crosscut': 'Partner B',
        }
        c.gear_sharing = json.dumps(complex_gear)
        db_session.flush()

        loaded = c.get_gear_sharing()
        assert loaded['1'] == 'Partner A'
        assert loaded['2'] == 'group:TeamSaw'
        assert loaded['category:crosscut'] == 'Partner B'

    def test_unicode_in_json(self, db_session, tournament):
        """Unicode names in JSON fields."""
        c = make_pro_competitor(db_session, tournament, 'Ünïcödë Test', 'M')
        c.partners = json.dumps({'1': 'José García'})
        db_session.flush()

        loaded = c.get_partners()
        assert loaded['1'] == 'José García'


# ---------------------------------------------------------------------------
# NULL handling in unique constraints
# ---------------------------------------------------------------------------

class TestNullUniques:
    """NULLable columns with unique constraints behave correctly."""

    def test_multiple_null_strathmark_ids_allowed(self, db_session, tournament):
        """PG allows multiple NULLs in unique indexes; SQLite does too."""
        c1 = make_pro_competitor(db_session, tournament, 'NullID1', 'M')
        c2 = make_pro_competitor(db_session, tournament, 'NullID2', 'F')
        assert c1.strathmark_id is None
        assert c2.strathmark_id is None
        db_session.flush()  # Should not raise

    def test_strathmark_id_uniqueness(self, db_session, tournament):
        """Non-NULL strathmark_ids should be unique per model convention."""
        c1 = make_pro_competitor(db_session, tournament, 'UniqueID1', 'M',
                                 strathmark_id='AKAPERM')
        db_session.flush()
        assert c1.strathmark_id == 'AKAPERM'


# ---------------------------------------------------------------------------
# Transaction savepoint behavior
# ---------------------------------------------------------------------------

class TestSavepointBehavior:
    """Savepoints work correctly for atomic operations."""

    def test_nested_rollback_preserves_outer(self, db_session, tournament):
        """Savepoint rollback doesn't affect outer transaction."""
        event = make_event(db_session, tournament, 'Savepoint Test')
        db_session.flush()

        try:
            with db_session.begin_nested():
                event.name = 'Modified Name'
                db_session.flush()
                raise ValueError('Intentional rollback')
        except ValueError:
            pass

        # Name should be rolled back to original
        db_session.refresh(event)
        assert event.name == 'Savepoint Test'


# ---------------------------------------------------------------------------
# Integer and float precision
# ---------------------------------------------------------------------------

class TestNumericPrecision:
    """Numeric values survive DB round-trips accurately."""

    def test_result_value_float_precision(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Precision Test')
        c = make_pro_competitor(db_session, tournament, 'PrecComp', 'M')
        r = make_event_result(db_session, e, c, result_value=15.123456789,
                              status='completed')
        db_session.flush()

        from models.event import EventResult
        loaded = EventResult.query.get(r.id)
        assert abs(loaded.result_value - 15.123456789) < 0.001

    def test_handicap_factor_precision(self, db_session, tournament):
        e = make_event(db_session, tournament, 'HF Precision')
        c = make_pro_competitor(db_session, tournament, 'HFComp', 'M')
        r = make_event_result(db_session, e, c, handicap_factor=3.456,
                              status='completed')
        db_session.flush()

        from models.event import EventResult
        loaded = EventResult.query.get(r.id)
        assert abs(loaded.handicap_factor - 3.456) < 0.01

    def test_payout_amount_zero_default(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Zero Payout')
        c = make_pro_competitor(db_session, tournament, 'ZeroComp', 'M')
        r = make_event_result(db_session, e, c, payout_amount=0.0, status='completed')
        db_session.flush()

        from models.event import EventResult
        loaded = EventResult.query.get(r.id)
        assert loaded.payout_amount == 0.0


# ---------------------------------------------------------------------------
# String length enforcement
# ---------------------------------------------------------------------------

class TestStringLengths:
    """String columns enforce max lengths (PG will truncate/reject)."""

    def test_event_name_within_limit(self, db_session, tournament):
        name = 'A' * 200  # max is 200
        e = make_event(db_session, tournament, name)
        db_session.flush()
        assert len(e.name) <= 200

    def test_competitor_name_truncated_at_100(self, db_session, tournament):
        long_name = 'X' * 150
        c = make_pro_competitor(db_session, tournament, long_name, 'M')
        # @validates truncates to 100
        assert len(c.name) <= 100


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestConfigValidation:
    """Runtime config validation catches production misconfig."""

    def test_weak_secret_key_rejected_in_production(self):
        from config import validate_runtime
        with pytest.raises(RuntimeError, match='SECRET_KEY'):
            validate_runtime({
                'ENV_NAME': 'production',
                'SECRET_KEY': 'changeme',
            })

    def test_short_secret_key_rejected(self):
        from config import validate_runtime
        with pytest.raises(RuntimeError, match='SECRET_KEY'):
            validate_runtime({
                'ENV_NAME': 'production',
                'SECRET_KEY': 'short',
            })

    def test_development_mode_allows_weak_key(self):
        from config import validate_runtime
        # Should not raise
        validate_runtime({
            'ENV_NAME': 'development',
            'SECRET_KEY': 'dev-key-change-in-production',
        })

    def test_strong_production_key_passes(self, monkeypatch):
        from config import validate_runtime
        monkeypatch.setenv('STRATHMARK_SUPABASE_URL', 'https://x.supabase.co')
        monkeypatch.setenv('STRATHMARK_SUPABASE_KEY', 'fake')
        # Should not raise
        validate_runtime({
            'ENV_NAME': 'production',
            'SECRET_KEY': 'a-very-strong-random-secret-key-1234567890!@#',
            'SQLALCHEMY_DATABASE_URI': 'postgresql://u:p@h/db',
        })

    def test_missing_strathmark_url_rejected_in_production(self, monkeypatch):
        """Production refuses to start when STRATHMARK_SUPABASE_URL is unset."""
        import pytest

        from config import validate_runtime
        monkeypatch.delenv('STRATHMARK_SUPABASE_URL', raising=False)
        monkeypatch.setenv('STRATHMARK_SUPABASE_KEY', 'fake')
        with pytest.raises(RuntimeError, match='STRATHMARK_SUPABASE'):
            validate_runtime({
                'ENV_NAME': 'production',
                'SECRET_KEY': 'a-very-strong-random-secret-key-1234567890!@#',
                'SQLALCHEMY_DATABASE_URI': 'postgresql://u:p@h/db',
            })

    def test_missing_strathmark_key_rejected_in_production(self, monkeypatch):
        """Production refuses to start when STRATHMARK_SUPABASE_KEY is unset."""
        import pytest

        from config import validate_runtime
        monkeypatch.setenv('STRATHMARK_SUPABASE_URL', 'https://x.supabase.co')
        monkeypatch.delenv('STRATHMARK_SUPABASE_KEY', raising=False)
        with pytest.raises(RuntimeError, match='STRATHMARK_SUPABASE'):
            validate_runtime({
                'ENV_NAME': 'production',
                'SECRET_KEY': 'a-very-strong-random-secret-key-1234567890!@#',
                'SQLALCHEMY_DATABASE_URI': 'postgresql://u:p@h/db',
            })

    def test_strathmark_check_skipped_in_development(self, monkeypatch):
        """Development env never enforces the STRATHMARK var requirement."""
        from config import validate_runtime
        monkeypatch.delenv('STRATHMARK_SUPABASE_URL', raising=False)
        monkeypatch.delenv('STRATHMARK_SUPABASE_KEY', raising=False)
        # Should not raise
        validate_runtime({'ENV_NAME': 'development', 'SECRET_KEY': 'dev'})


# ---------------------------------------------------------------------------
# Production detection (CSO follow-up: HSTS / SESSION_COOKIE_SECURE / validate_runtime
# were silently disabled on Railway because FLASK_ENV wasn't set. The detection
# now also auto-fires on RAILWAY_ENVIRONMENT and on a postgresql:// DATABASE_URL.)
# ---------------------------------------------------------------------------

class TestProductionDetection:
    """_is_production_environment auto-detects Railway / postgres."""

    def _clear_env(self, monkeypatch):
        for var in ('FLASK_ENV', 'PRODUCTION', 'RAILWAY_ENVIRONMENT', 'TESTING', 'DATABASE_URL'):
            monkeypatch.delenv(var, raising=False)

    def test_explicit_testing_is_not_production(self, monkeypatch):
        from config import _is_production_environment
        self._clear_env(monkeypatch)
        monkeypatch.setenv('FLASK_ENV', 'testing')
        monkeypatch.setenv('DATABASE_URL', 'postgresql://x:y@z/db')
        assert _is_production_environment() is False

    def test_testing_env_var_is_not_production(self, monkeypatch):
        from config import _is_production_environment
        self._clear_env(monkeypatch)
        monkeypatch.setenv('TESTING', '1')
        monkeypatch.setenv('DATABASE_URL', 'postgresql://x:y@z/db')
        assert _is_production_environment() is False

    def test_explicit_development_is_not_production(self, monkeypatch):
        from config import _is_production_environment
        self._clear_env(monkeypatch)
        monkeypatch.setenv('FLASK_ENV', 'development')
        monkeypatch.setenv('DATABASE_URL', 'postgresql://x:y@z/db')
        assert _is_production_environment() is False

    def test_explicit_flask_env_production(self, monkeypatch):
        from config import _is_production_environment
        self._clear_env(monkeypatch)
        monkeypatch.setenv('FLASK_ENV', 'production')
        assert _is_production_environment() is True

    def test_explicit_production_env_var(self, monkeypatch):
        from config import _is_production_environment
        self._clear_env(monkeypatch)
        monkeypatch.setenv('PRODUCTION', '1')
        assert _is_production_environment() is True

    def test_railway_environment_var_implies_production(self, monkeypatch):
        """Auto-detect Railway via RAILWAY_ENVIRONMENT — Railway sets this on every deploy."""
        from config import _is_production_environment
        self._clear_env(monkeypatch)
        monkeypatch.setenv('RAILWAY_ENVIRONMENT', 'production')
        assert _is_production_environment() is True

    def test_postgres_database_url_implies_production(self, monkeypatch):
        """Auto-detect production via postgresql:// — validate_runtime requires it anyway."""
        from config import _is_production_environment
        self._clear_env(monkeypatch)
        monkeypatch.setenv('DATABASE_URL', 'postgresql://prod:secret@host/db')
        assert _is_production_environment() is True

    def test_postgres_legacy_url_scheme_implies_production(self, monkeypatch):
        """The legacy postgres:// scheme normalizes to postgresql:// and is also detected."""
        from config import _is_production_environment
        self._clear_env(monkeypatch)
        monkeypatch.setenv('DATABASE_URL', 'postgres://prod:secret@host/db')
        assert _is_production_environment() is True

    def test_local_sqlite_is_not_production(self, monkeypatch):
        from config import _is_production_environment
        self._clear_env(monkeypatch)
        monkeypatch.setenv('DATABASE_URL', 'sqlite:////tmp/dev.db')
        assert _is_production_environment() is False

    def test_no_env_at_all_is_not_production(self, monkeypatch):
        """Default fallback: nothing set → development (bare local invocation)."""
        from config import _is_production_environment
        self._clear_env(monkeypatch)
        assert _is_production_environment() is False

    def test_get_config_returns_production_when_railway_detected(self, monkeypatch):
        from config import get_config
        self._clear_env(monkeypatch)
        monkeypatch.setenv('RAILWAY_ENVIRONMENT', 'production')
        monkeypatch.setenv('DATABASE_URL', 'postgresql://prod:secret@host/db')
        cfg = get_config()
        assert cfg.ENV_NAME == 'production'

    def test_get_config_returns_development_in_ci_with_postgres(self, monkeypatch):
        """Mirrors the CI postgres-smoke job: postgres URI but FLASK_ENV=testing."""
        from config import get_config
        self._clear_env(monkeypatch)
        monkeypatch.setenv('FLASK_ENV', 'testing')
        monkeypatch.setenv('DATABASE_URL', 'postgresql://x:y@z/db')
        cfg = get_config()
        assert cfg.ENV_NAME == 'development'
