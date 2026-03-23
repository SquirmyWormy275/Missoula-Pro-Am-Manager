"""
Comprehensive model tests covering Tournament, Team, CollegeCompetitor,
ProCompetitor, Event, EventResult, Heat, and User.

Complements tests/test_models.py which covers WoodConfig, PayoutTemplate,
and SchoolCaptain.

Run:  pytest tests/test_models_full.py -v
"""
import json
from datetime import datetime, timedelta, timezone

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
        SECRET_KEY='test-secret-full',
        WTF_CSRF_ENABLED=False,
    )
    _db.init_app(test_app)

    with test_app.app_context():
        import models  # noqa: F401 â€” registers all mappers
        _db.create_all()
        yield test_app
        _db.session.remove()
        # _db.drop_all() — skipped; in-memory SQLite is discarded on exit


@pytest.fixture(autouse=True)
def db_session(app):
    """Provide a clean DB session that rolls back after each test."""
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tournament(name='Test Tournament', year=2026):
    from models.tournament import Tournament
    t = Tournament(name=name, year=year, status='setup')
    _db.session.add(t)
    _db.session.flush()
    return t


def _make_team(tournament, team_code='UM-A', school_name='University of Montana',
               school_abbreviation='UM'):
    from models.team import Team
    t = Team(
        tournament_id=tournament.id,
        team_code=team_code,
        school_name=school_name,
        school_abbreviation=school_abbreviation,
    )
    _db.session.add(t)
    _db.session.flush()
    return t


def _make_college_competitor(tournament, team, name='John Doe', gender='M'):
    from models.competitor import CollegeCompetitor
    c = CollegeCompetitor(
        tournament_id=tournament.id,
        team_id=team.id,
        name=name,
        gender=gender,
    )
    _db.session.add(c)
    _db.session.flush()
    return c


def _make_pro_competitor(tournament, name='Jane Pro', gender='F'):
    from models.competitor import ProCompetitor
    p = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
    )
    _db.session.add(p)
    _db.session.flush()
    return p


def _make_event(tournament, name='Underhand Speed', event_type='pro',
                gender='M', scoring_type='time', scoring_order='lowest_wins'):
    from models.event import Event
    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender=gender,
        scoring_type=scoring_type,
        scoring_order=scoring_order,
    )
    _db.session.add(e)
    _db.session.flush()
    return e


def _make_event_result(event, competitor_id=1, competitor_type='pro',
                       competitor_name='Test Comp'):
    from models.event import EventResult
    r = EventResult(
        event_id=event.id,
        competitor_id=competitor_id,
        competitor_type=competitor_type,
        competitor_name=competitor_name,
    )
    _db.session.add(r)
    _db.session.flush()
    return r


def _make_heat(event, heat_number=1, run_number=1):
    from models.heat import Heat
    h = Heat(
        event_id=event.id,
        heat_number=heat_number,
        run_number=run_number,
    )
    _db.session.add(h)
    _db.session.flush()
    return h


def _make_user(username='testuser', role='admin'):
    from models.user import User
    u = User(username=username, role=role)
    u.set_password('password123')
    _db.session.add(u)
    _db.session.flush()
    return u


# ===========================================================================
# Tournament Tests
# ===========================================================================

class TestTournamentScheduleConfig:
    """get_schedule_config / set_schedule_config round-trip."""

    def test_default_is_empty_dict(self, db_session):
        t = _make_tournament()
        assert t.get_schedule_config() == {}

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        data = {'friday_pro_event_ids': [1, 2], 'saturday_college_event_ids': [3]}
        t.set_schedule_config(data)
        result = t.get_schedule_config()
        assert result == data

    def test_corrupt_json_returns_empty_dict(self, db_session):
        t = _make_tournament()
        t.schedule_config = '{not valid json'
        assert t.get_schedule_config() == {}

    def test_none_returns_empty_dict(self, db_session):
        t = _make_tournament()
        t.schedule_config = None
        assert t.get_schedule_config() == {}


class TestTournamentCountProperties:
    """Tournament count properties that depend on relationships."""

    def test_college_team_count(self, db_session):
        t = _make_tournament()
        assert t.college_team_count == 0
        _make_team(t)
        assert t.college_team_count == 1

    def test_college_competitor_count(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        assert t.college_competitor_count == 0
        _make_college_competitor(t, team, name='Alice', gender='F')
        assert t.college_competitor_count == 1

    def test_pro_competitor_count(self, db_session):
        t = _make_tournament()
        assert t.pro_competitor_count == 0
        _make_pro_competitor(t)
        assert t.pro_competitor_count == 1


# ===========================================================================
# Team Tests
# ===========================================================================

class TestTeamProperties:
    """Team member_count, male_count, female_count, is_valid."""

    def test_empty_team_counts(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        assert team.member_count == 0
        assert team.male_count == 0
        assert team.female_count == 0

    def test_counts_with_members(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        _make_college_competitor(t, team, 'Male1', 'M')
        _make_college_competitor(t, team, 'Male2', 'M')
        _make_college_competitor(t, team, 'Female1', 'F')
        _make_college_competitor(t, team, 'Female2', 'F')
        assert team.member_count == 4
        assert team.male_count == 2
        assert team.female_count == 2

    def test_counts_ignore_scratched(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team, 'Scratched', 'M')
        c.status = 'scratched'
        _make_college_competitor(t, team, 'Active', 'M')
        assert team.member_count == 1
        assert team.male_count == 1

    def test_is_valid_true(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        _make_college_competitor(t, team, 'M1', 'M')
        _make_college_competitor(t, team, 'M2', 'M')
        _make_college_competitor(t, team, 'F1', 'F')
        _make_college_competitor(t, team, 'F2', 'F')
        assert team.is_valid is True

    def test_is_valid_false_not_enough_females(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        _make_college_competitor(t, team, 'M1', 'M')
        _make_college_competitor(t, team, 'M2', 'M')
        _make_college_competitor(t, team, 'F1', 'F')
        assert team.is_valid is False

    def test_is_valid_false_too_many_members(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        for i in range(5):
            _make_college_competitor(t, team, f'M{i}', 'M')
        for i in range(4):
            _make_college_competitor(t, team, f'F{i}', 'F')
        assert team.member_count == 9
        assert team.is_valid is False


class TestTeamRecalculatePoints:
    """recalculate_points sums member individual_points."""

    def test_recalculate_empty_team(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        result = team.recalculate_points()
        assert result == 0
        assert team.total_points == 0

    def test_recalculate_sums_members(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c1 = _make_college_competitor(t, team, 'A', 'M')
        c2 = _make_college_competitor(t, team, 'B', 'F')
        c1.individual_points = 10
        c2.individual_points = 7
        result = team.recalculate_points()
        assert result == 17
        assert team.total_points == 17

    def test_recalculate_ignores_scratched(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c1 = _make_college_competitor(t, team, 'Active', 'M')
        c2 = _make_college_competitor(t, team, 'Scratched', 'F')
        c1.individual_points = 10
        c2.individual_points = 7
        c2.status = 'scratched'
        result = team.recalculate_points()
        assert result == 10


class TestTeamValidationErrors:
    """get_validation_errors / set_validation_errors round-trip."""

    def test_default_is_empty_list(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        assert team.get_validation_errors() == []

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        errors = [{'type': 'gender_imbalance', 'message': 'Need more females'}]
        team.set_validation_errors(errors)
        assert team.get_validation_errors() == errors
        assert team.status == 'invalid'

    def test_set_empty_list_restores_active(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        team.set_validation_errors([{'type': 'test'}])
        assert team.status == 'invalid'
        team.set_validation_errors([])
        assert team.status == 'active'

    def test_corrupt_json_returns_empty_list(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        team.validation_errors = 'not json!!!'
        assert team.get_validation_errors() == []


# ===========================================================================
# CollegeCompetitor Tests
# ===========================================================================

class TestCollegeCompetitorNameTruncation:
    """validate_name truncates at MAX_NAME_LENGTH (100 chars)."""

    def test_short_name_preserved(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team, 'Short Name', 'M')
        assert c.name == 'Short Name'

    def test_long_name_truncated(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        long_name = 'A' * 150
        c = _make_college_competitor(t, team, long_name, 'M')
        assert len(c.name) == 100


class TestCollegeCompetitorEventsEntered:
    """get_events_entered / set_events_entered round-trip."""

    def test_default_is_empty_list(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        assert c.get_events_entered() == []

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        c.set_events_entered([1, 2, 3])
        assert c.get_events_entered() == [1, 2, 3]

    def test_corrupt_json_returns_empty_list(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        c.events_entered = '{bad json'
        assert c.get_events_entered() == []


class TestCollegeCompetitorPartners:
    """get_partners / set_partner round-trip."""

    def test_default_is_empty_dict(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        assert c.get_partners() == {}

    def test_set_partner_and_get(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        c.set_partner(5, 'Alice')
        c.set_partner(10, 'Bob')
        partners = c.get_partners()
        assert partners['5'] == 'Alice'
        assert partners['10'] == 'Bob'


class TestCollegeCompetitorGearSharing:
    """get_gear_sharing / set_gear_sharing round-trip."""

    def test_default_is_empty_dict(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        assert c.get_gear_sharing() == {}

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        c.set_gear_sharing(7, 'PartnerGuy')
        result = c.get_gear_sharing()
        assert result['7'] == 'PartnerGuy'

    def test_corrupt_json_returns_empty_dict(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        c.gear_sharing = 'broken!'
        assert c.get_gear_sharing() == {}


class TestCollegeCompetitorAddPoints:
    """add_points updates individual and team totals."""

    def test_add_points_updates_individual(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        assert c.individual_points == 0
        c.add_points(10)
        assert c.individual_points == 10

    def test_add_points_updates_team(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c1 = _make_college_competitor(t, team, 'A', 'M')
        c2 = _make_college_competitor(t, team, 'B', 'F')
        c1.add_points(10)
        c2.add_points(7)
        assert team.total_points == 17

    def test_add_points_cumulative(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        c.add_points(5)
        c.add_points(3)
        assert c.individual_points == 8


class TestCollegeCompetitorClosedEventCount:
    """closed_event_count counts closed college events from events_entered list."""

    def test_no_events_returns_zero(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        assert c.closed_event_count == 0

    def test_counts_closed_events_only(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        # Create one closed and one open college event
        e_closed = _make_event(t, name='Single Buck', event_type='college',
                               gender='M', scoring_type='time')
        e_closed.is_open = False
        e_open = _make_event(t, name='Axe Throw', event_type='college',
                             gender='M', scoring_type='score')
        e_open.is_open = True
        _db.session.flush()
        c = _make_college_competitor(t, team)
        c.set_events_entered([e_closed.id, e_open.id])
        assert c.closed_event_count == 1


class TestCollegeCompetitorPortalPin:
    """Portal PIN: set_portal_pin, check_portal_pin, has_portal_pin."""

    def test_has_portal_pin_false_initially(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        assert c.has_portal_pin is False

    def test_set_and_check_pin(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        c.set_portal_pin('4321')
        assert c.has_portal_pin is True
        assert c.check_portal_pin('4321') is True
        assert c.check_portal_pin('wrong') is False

    def test_check_pin_without_set_returns_false(self, db_session):
        t = _make_tournament()
        team = _make_team(t)
        c = _make_college_competitor(t, team)
        assert c.check_portal_pin('1234') is False


# ===========================================================================
# ProCompetitor Tests
# ===========================================================================

class TestProCompetitorNameTruncation:
    """validate_name truncates at MAX_NAME_LENGTH (100 chars)."""

    def test_short_name_preserved(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t, 'Normal Name', 'M')
        assert p.name == 'Normal Name'

    def test_long_name_truncated(self, db_session):
        t = _make_tournament()
        long_name = 'B' * 200
        p = _make_pro_competitor(t, long_name, 'M')
        assert len(p.name) == 100


class TestProCompetitorEventsEntered:
    """get_events_entered / set_events_entered round-trip."""

    def test_default_is_empty_list(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        assert p.get_events_entered() == []

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.set_events_entered([10, 20, 30])
        assert p.get_events_entered() == [10, 20, 30]

    def test_corrupt_json_returns_empty_list(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.events_entered = 'garbage'
        assert p.get_events_entered() == []


class TestProCompetitorEntryFees:
    """get_entry_fees / set_entry_fee round-trip."""

    def test_default_is_empty_dict(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        assert p.get_entry_fees() == {}

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.set_entry_fee(1, 25.0)
        p.set_entry_fee(2, 50.0)
        fees = p.get_entry_fees()
        assert fees['1'] == 25.0
        assert fees['2'] == 50.0

    def test_corrupt_json_returns_empty_dict(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.entry_fees = '!!!'
        assert p.get_entry_fees() == {}


class TestProCompetitorFeesPaid:
    """get_fees_paid / set_fee_paid round-trip."""

    def test_default_is_empty_dict(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        assert p.get_fees_paid() == {}

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.set_fee_paid(1, True)
        p.set_fee_paid(2, False)
        paid = p.get_fees_paid()
        assert paid['1'] is True
        assert paid['2'] is False

    def test_corrupt_json_returns_empty_dict(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.fees_paid = 'nope'
        assert p.get_fees_paid() == {}


class TestProCompetitorGearSharing:
    """get_gear_sharing / set_gear_sharing round-trip."""

    def test_default_is_empty_dict(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        assert p.get_gear_sharing() == {}

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.set_gear_sharing(5, 'My Partner')
        result = p.get_gear_sharing()
        assert result['5'] == 'My Partner'

    def test_corrupt_json_returns_empty_dict(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.gear_sharing = '{'
        assert p.get_gear_sharing() == {}


class TestProCompetitorAddEarnings:
    """add_earnings updates total_earnings."""

    def test_add_earnings(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        assert p.total_earnings == 0.0
        p.add_earnings(500.0)
        assert p.total_earnings == 500.0
        p.add_earnings(250.0)
        assert p.total_earnings == 750.0


class TestProCompetitorFeeProperties:
    """total_fees_owed, total_fees_paid, fees_balance."""

    def test_total_fees_owed(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.set_entry_fee(1, 25.0)
        p.set_entry_fee(2, 50.0)
        assert p.total_fees_owed == 75.0

    def test_total_fees_paid_partial(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.set_entry_fee(1, 25.0)
        p.set_entry_fee(2, 50.0)
        p.set_fee_paid(1, True)
        p.set_fee_paid(2, False)
        assert p.total_fees_paid == 25.0

    def test_fees_balance(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.set_entry_fee(1, 25.0)
        p.set_entry_fee(2, 50.0)
        p.set_fee_paid(1, True)
        assert p.fees_balance == 50.0

    def test_fees_balance_all_paid(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.set_entry_fee(1, 25.0)
        p.set_fee_paid(1, True)
        assert p.fees_balance == 0.0

    def test_fees_balance_nothing_owed(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        assert p.fees_balance == 0.0


class TestProCompetitorPortalPin:
    """Portal PIN methods for ProCompetitor."""

    def test_has_portal_pin_false_initially(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        assert p.has_portal_pin is False

    def test_set_and_check_pin(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        p.set_portal_pin('9999')
        assert p.has_portal_pin is True
        assert p.check_portal_pin('9999') is True
        assert p.check_portal_pin('0000') is False

    def test_check_pin_without_set_returns_false(self, db_session):
        t = _make_tournament()
        p = _make_pro_competitor(t)
        assert p.check_portal_pin('1234') is False


# ===========================================================================
# Event Tests
# ===========================================================================

class TestEventDisplayName:
    """display_name property: gender prefix logic."""

    def test_mens_event(self, db_session):
        t = _make_tournament()
        e = _make_event(t, name='Underhand Speed', gender='M')
        assert e.display_name == "Men's Underhand Speed"

    def test_womens_event(self, db_session):
        t = _make_tournament()
        e = _make_event(t, name='Standing Block Speed', gender='F')
        assert e.display_name == "Women's Standing Block Speed"

    def test_mixed_event_no_prefix(self, db_session):
        t = _make_tournament()
        e = _make_event(t, name='Jack and Jill', gender=None)
        assert e.display_name == 'Jack and Jill'


class TestEventIsHardHit:
    """is_hard_hit property checks config.HARD_HIT_EVENTS."""

    def test_hard_hit_true(self, db_session):
        t = _make_tournament()
        e = _make_event(t, name='Underhand Hard Hit')
        assert e.is_hard_hit is True

    def test_hard_hit_false(self, db_session):
        t = _make_tournament()
        e = _make_event(t, name='Underhand Speed')
        assert e.is_hard_hit is False


class TestEventPayouts:
    """get_payouts / set_payouts round-trip."""

    def test_default_is_empty_dict(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        assert e.get_payouts() == {}

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        e.set_payouts({'1': 500, '2': 300, '3': 100})
        result = e.get_payouts()
        assert result['1'] == 500
        assert result['2'] == 300
        assert result['3'] == 100

    def test_get_payout_for_position(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        e.set_payouts({'1': 500, '2': 300})
        assert e.get_payout_for_position(1) == 500
        assert e.get_payout_for_position(2) == 300
        assert e.get_payout_for_position(5) == 0

    def test_corrupt_json_returns_empty_dict(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        e.payouts = 'not json at all'
        assert e.get_payouts() == {}


# ===========================================================================
# EventResult Tests
# ===========================================================================

class TestEventResultCalculateBestRun:
    """calculate_best_run picks the right run based on scoring_order."""

    def test_lowest_wins_picks_lower(self, db_session):
        t = _make_tournament()
        e = _make_event(t, scoring_order='lowest_wins')
        r = _make_event_result(e)
        r.run1_value = 15.5
        r.run2_value = 12.3
        result = r.calculate_best_run('lowest_wins')
        assert result == 12.3
        assert r.best_run == 12.3
        assert r.result_value == 12.3

    def test_highest_wins_picks_higher(self, db_session):
        t = _make_tournament()
        e = _make_event(t, scoring_order='highest_wins')
        r = _make_event_result(e)
        r.run1_value = 15.5
        r.run2_value = 12.3
        result = r.calculate_best_run('highest_wins')
        assert result == 15.5
        assert r.best_run == 15.5
        assert r.result_value == 15.5

    def test_one_run_only(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        r = _make_event_result(e)
        r.run1_value = 20.0
        r.run2_value = None
        result = r.calculate_best_run('lowest_wins')
        assert result == 20.0

    def test_no_runs_returns_none(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        r = _make_event_result(e)
        r.run1_value = None
        r.run2_value = None
        result = r.calculate_best_run('lowest_wins')
        assert result is None

    def test_equal_runs(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        r = _make_event_result(e)
        r.run1_value = 10.0
        r.run2_value = 10.0
        result = r.calculate_best_run('lowest_wins')
        assert result == 10.0


class TestEventResultCalculateCumulativeScore:
    """calculate_cumulative_score sums run1+run2+run3."""

    def test_all_three_runs(self, db_session):
        t = _make_tournament()
        e = _make_event(t, scoring_type='score', scoring_order='highest_wins')
        r = _make_event_result(e)
        r.run1_value = 5.0
        r.run2_value = 3.0
        r.run3_value = 4.0
        result = r.calculate_cumulative_score()
        assert result == 12.0
        assert r.result_value == 12.0

    def test_partial_runs(self, db_session):
        t = _make_tournament()
        e = _make_event(t, scoring_type='score', scoring_order='highest_wins')
        r = _make_event_result(e)
        r.run1_value = 5.0
        r.run2_value = 3.0
        r.run3_value = None
        result = r.calculate_cumulative_score()
        assert result == 8.0

    def test_no_runs_returns_none(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        r = _make_event_result(e)
        r.run1_value = None
        r.run2_value = None
        r.run3_value = None
        result = r.calculate_cumulative_score()
        assert result is None

    def test_single_run(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        r = _make_event_result(e)
        r.run1_value = 7.0
        r.run2_value = None
        r.run3_value = None
        result = r.calculate_cumulative_score()
        assert result == 7.0


# ===========================================================================
# Heat Tests
# ===========================================================================

class TestHeatCompetitors:
    """get_competitors / set_competitors round-trip."""

    def test_default_is_empty_list(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        assert h.get_competitors() == []

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        h.set_competitors([10, 20, 30])
        assert h.get_competitors() == [10, 20, 30]

    def test_corrupt_json_returns_empty_list(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        h.competitors = '{bad'
        assert h.get_competitors() == []


class TestHeatAddRemoveCompetitor:
    """add_competitor / remove_competitor."""

    def test_add_competitor(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        h.add_competitor(42)
        assert 42 in h.get_competitors()

    def test_add_duplicate_is_noop(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        h.add_competitor(42)
        h.add_competitor(42)
        assert h.get_competitors().count(42) == 1

    def test_remove_competitor(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        h.set_competitors([1, 2, 3])
        h.remove_competitor(2)
        assert h.get_competitors() == [1, 3]

    def test_remove_nonexistent_is_noop(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        h.set_competitors([1, 2])
        h.remove_competitor(99)
        assert h.get_competitors() == [1, 2]


class TestHeatStandAssignments:
    """get_stand_assignments / set_stand_assignment round-trip."""

    def test_default_is_empty_dict(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        assert h.get_stand_assignments() == {}

    def test_set_and_get_roundtrip(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        h.set_stand_assignment(10, 1)
        h.set_stand_assignment(20, 2)
        assignments = h.get_stand_assignments()
        assert assignments['10'] == 1
        assert assignments['20'] == 2

    def test_get_stand_for_competitor(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        h.set_stand_assignment(10, 3)
        assert h.get_stand_for_competitor(10) == 3
        assert h.get_stand_for_competitor(99) is None

    def test_corrupt_json_returns_empty_dict(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        h.stand_assignments = '!!'
        assert h.get_stand_assignments() == {}


class TestHeatCompetitorCount:
    """competitor_count property."""

    def test_empty_heat(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        assert h.competitor_count == 0

    def test_with_competitors(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        h.set_competitors([1, 2, 3, 4, 5])
        assert h.competitor_count == 5


class TestHeatLocking:
    """Lock methods: acquire_lock, release_lock, is_locked."""

    def test_not_locked_by_default(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        assert h.is_locked() is False

    def test_acquire_lock_succeeds(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        u = _make_user('locker', 'judge')
        assert h.acquire_lock(u.id) is True
        assert h.is_locked() is True
        assert h.locked_by_user_id == u.id

    def test_acquire_lock_same_user_succeeds(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        u = _make_user('locker2', 'judge')
        h.acquire_lock(u.id)
        # Same user can re-acquire
        assert h.acquire_lock(u.id) is True

    def test_acquire_lock_different_user_fails(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        u1 = _make_user('user1', 'judge')
        u2 = _make_user('user2', 'scorer')
        h.acquire_lock(u1.id)
        assert h.acquire_lock(u2.id) is False

    def test_release_lock(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        u = _make_user('releaser', 'judge')
        h.acquire_lock(u.id)
        assert h.is_locked() is True
        h.release_lock(u.id)
        assert h.is_locked() is False
        assert h.locked_by_user_id is None

    def test_release_lock_wrong_user_is_noop(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        u1 = _make_user('holder', 'judge')
        u2 = _make_user('other', 'scorer')
        h.acquire_lock(u1.id)
        h.release_lock(u2.id)
        # Lock should still be held by u1
        assert h.is_locked() is True
        assert h.locked_by_user_id == u1.id

    def test_expired_lock_is_not_locked(self, db_session):
        t = _make_tournament()
        e = _make_event(t)
        h = _make_heat(e)
        u = _make_user('expirer', 'judge')
        h.locked_by_user_id = u.id
        # Set locked_at to 10 minutes ago (well past 5 min TTL)
        h.locked_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        assert h.is_locked() is False


# ===========================================================================
# User Tests
# ===========================================================================

class TestUserPassword:
    """set_password / check_password round-trip."""

    def test_set_and_check_correct(self, db_session):
        u = _make_user('pwtest', 'admin')
        assert u.check_password('password123') is True

    def test_check_wrong_password(self, db_session):
        u = _make_user('pwtest2', 'admin')
        assert u.check_password('wrong') is False

    def test_check_password_without_hash(self, db_session):
        from models.user import User
        u = User(username='nohash', role='viewer')
        u.password_hash = None
        assert u.check_password('anything') is False


class TestUserIsJudge:
    """is_judge property: True for admin OR judge role."""

    def test_admin_is_judge(self, db_session):
        u = _make_user('admin_j', 'admin')
        assert u.is_judge is True

    def test_judge_is_judge(self, db_session):
        u = _make_user('judge_j', 'judge')
        assert u.is_judge is True

    def test_scorer_is_not_judge(self, db_session):
        u = _make_user('scorer_j', 'scorer')
        assert u.is_judge is False

    def test_viewer_is_not_judge(self, db_session):
        u = _make_user('viewer_j', 'viewer')
        assert u.is_judge is False


class TestUserRoleProperties:
    """Role-based permission properties."""

    def test_is_admin(self, db_session):
        assert _make_user('a1', 'admin').is_admin is True
        assert _make_user('a2', 'judge').is_admin is False

    def test_is_competitor(self, db_session):
        assert _make_user('c1', 'competitor').is_competitor is True
        assert _make_user('c2', 'admin').is_competitor is False

    def test_is_spectator(self, db_session):
        assert _make_user('s1', 'spectator').is_spectator is True
        assert _make_user('s2', 'viewer').is_spectator is True
        assert _make_user('s3', 'admin').is_spectator is False

    def test_can_manage_users(self, db_session):
        assert _make_user('m1', 'admin').can_manage_users is True
        assert _make_user('m2', 'judge').can_manage_users is False

    def test_can_register(self, db_session):
        assert _make_user('r1', 'admin').can_register is True
        assert _make_user('r2', 'judge').can_register is True
        assert _make_user('r3', 'registrar').can_register is True
        assert _make_user('r4', 'scorer').can_register is False

    def test_can_schedule(self, db_session):
        assert _make_user('sch1', 'admin').can_schedule is True
        assert _make_user('sch2', 'judge').can_schedule is True
        assert _make_user('sch3', 'scorer').can_schedule is True
        assert _make_user('sch4', 'registrar').can_schedule is False

    def test_can_score(self, db_session):
        assert _make_user('sc1', 'admin').can_score is True
        assert _make_user('sc2', 'judge').can_score is True
        assert _make_user('sc3', 'scorer').can_score is True
        assert _make_user('sc4', 'spectator').can_score is False

    def test_can_report(self, db_session):
        assert _make_user('rp1', 'admin').can_report is True
        assert _make_user('rp2', 'viewer').can_report is True
        assert _make_user('rp3', 'spectator').can_report is True
        assert _make_user('rp4', 'competitor').can_report is False

    def test_is_active_default(self, db_session):
        u = _make_user('act1', 'admin')
        assert u.is_active is True

    def test_is_active_disabled(self, db_session):
        u = _make_user('act2', 'admin')
        u.is_active_user = False
        assert u.is_active is False
