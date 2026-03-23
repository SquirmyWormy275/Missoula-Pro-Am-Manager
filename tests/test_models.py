"""
Unit tests for pure model methods (no external database required).

We spin up a minimal Flask application with an in-memory SQLite database so
that SQLAlchemy's ORM is fully initialised and model instances can be created
and their pure Python methods exercised without hitting any persisted state.

Run:  pytest tests/test_models.py -v
"""
import pytest
from flask import Flask
from database import db as _db


# ---------------------------------------------------------------------------
# App / DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Minimal Flask app with an in-memory SQLite database."""
    test_app = Flask(__name__)
    test_app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY='test-secret',
        WTF_CSRF_ENABLED=False,
    )
    _db.init_app(test_app)

    with test_app.app_context():
        # Import all models so their tables are registered
        import models  # noqa: F401 — side-effect: registers all mappers
        _db.create_all()
        yield test_app
        # _db.drop_all() � skipped; in-memory SQLite is discarded on exit


@pytest.fixture()
def db_session(app):
    """Provide a clean DB session that rolls back after each test."""
    with app.app_context():
        yield _db.session
        _db.session.rollback()


# ---------------------------------------------------------------------------
# WoodConfig.display_size()
# ---------------------------------------------------------------------------

class TestWoodConfigDisplaySize:
    """Tests for WoodConfig.display_size() — uses in-memory DB."""

    def _make(self, db_session, size_value, size_unit='in'):
        from models.wood_config import WoodConfig
        wc = WoodConfig(
            tournament_id=999,
            config_key='test_key',
            size_value=size_value,
            size_unit=size_unit,
        )
        # No need to flush/commit — we just need the instance attributes
        return wc

    def test_whole_number_inches(self, db_session):
        wc = self._make(db_session, 12.0, 'in')
        assert wc.display_size() == '12"'

    def test_fractional_inches(self, db_session):
        wc = self._make(db_session, 12.5, 'in')
        assert wc.display_size() == '12.5"'

    def test_millimeters(self, db_session):
        wc = self._make(db_session, 300.0, 'mm')
        assert wc.display_size() == '300 mm'

    def test_none_value_returns_dash(self, db_session):
        wc = self._make(db_session, None)
        assert wc.display_size() == '—'

    def test_small_whole_number_inches(self, db_session):
        wc = self._make(db_session, 6.0, 'in')
        assert wc.display_size() == '6"'


# ---------------------------------------------------------------------------
# PayoutTemplate methods
# ---------------------------------------------------------------------------

class TestPayoutTemplate:
    """Tests for PayoutTemplate.get_payouts(), set_payouts(), total_purse()."""

    def _make(self, db_session, payouts_json='{}'):
        from models.payout_template import PayoutTemplate
        pt = PayoutTemplate(name='Test Template', payouts=payouts_json)
        return pt

    def test_set_and_get_payouts_roundtrip(self, db_session):
        pt = self._make(db_session)
        pt.set_payouts({1: 500, 2: 300})
        result = pt.get_payouts()
        # JSON keys are always strings after serialise/deserialise
        assert float(result['1']) == 500.0
        assert float(result['2']) == 300.0

    def test_total_purse(self, db_session):
        pt = self._make(db_session)
        pt.set_payouts({1: 500, 2: 300})
        assert pt.total_purse() == 800.0

    def test_get_payouts_empty_template(self, db_session):
        pt = self._make(db_session, '{}')
        assert pt.get_payouts() == {}

    def test_total_purse_empty_template(self, db_session):
        pt = self._make(db_session, '{}')
        assert pt.total_purse() == 0.0

    def test_payouts_with_string_keys(self, db_session):
        pt = self._make(db_session)
        pt.set_payouts({'1': 250.0})
        result = pt.get_payouts()
        assert float(result['1']) == 250.0
        assert pt.total_purse() == 250.0


# ---------------------------------------------------------------------------
# SchoolCaptain.check_pin() / set_pin() / has_pin
# ---------------------------------------------------------------------------

class TestSchoolCaptain:
    """Tests for SchoolCaptain PIN management — uses in-memory DB."""

    def _make(self, db_session):
        from models.school_captain import SchoolCaptain
        sc = SchoolCaptain(tournament_id=999, school_name='Test School')
        return sc

    def test_has_pin_false_when_no_hash(self, db_session):
        sc = self._make(db_session)
        assert sc.has_pin is False

    def test_has_pin_true_after_set_pin(self, db_session):
        sc = self._make(db_session)
        sc.set_pin('1234')
        assert sc.has_pin is True

    def test_check_pin_correct_returns_true(self, db_session):
        sc = self._make(db_session)
        sc.set_pin('1234')
        assert sc.check_pin('1234') is True

    def test_check_pin_wrong_returns_false(self, db_session):
        sc = self._make(db_session)
        sc.set_pin('1234')
        assert sc.check_pin('wrong') is False

    def test_check_pin_without_set_returns_false(self, db_session):
        sc = self._make(db_session)
        assert sc.check_pin('any') is False
