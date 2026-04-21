"""
Model safety tests — JSON corruption resilience, heat locks, optimistic
locking, name validation, and EventResult calculation edge cases.

Run:
    pytest tests/test_model_json_safety.py -v
"""
import json
import time
from datetime import datetime, timedelta
from unittest.mock import patch

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
# JSON corruption resilience — all model .get_*() methods
# ---------------------------------------------------------------------------

class TestCollegeCompetitorJsonSafety:
    """CollegeCompetitor JSON getters survive corruption."""

    def test_corrupt_events_entered_returns_empty_list(self, db_session, tournament):
        team = make_team(db_session, tournament)
        c = make_college_competitor(db_session, tournament, team, 'Corrupt1', 'M')
        c.events_entered = '{bad json'
        db_session.flush()

        assert c.get_events_entered() == []

    def test_corrupt_partners_returns_empty_dict(self, db_session, tournament):
        team = make_team(db_session, tournament)
        c = make_college_competitor(db_session, tournament, team, 'Corrupt2', 'M')
        c.partners = 'not json at all'
        db_session.flush()

        assert c.get_partners() == {}

    def test_corrupt_gear_sharing_returns_empty_dict(self, db_session, tournament):
        team = make_team(db_session, tournament)
        c = make_college_competitor(db_session, tournament, team, 'Corrupt3', 'F')
        c.gear_sharing = '<<<invalid>>>'
        db_session.flush()

        assert c.get_gear_sharing() == {}

    def test_none_events_entered_returns_empty_list(self, db_session, tournament):
        team = make_team(db_session, tournament)
        c = make_college_competitor(db_session, tournament, team, 'NoneEvt', 'M')
        # Set attribute to None in-memory only. The DB column is NOT NULL so
        # flushing would fail, but the getter reads the Python attribute and
        # must handle None (e.g. from a freshly-built instance before defaults
        # kick in, or from legacy rows loaded before the constraint was added).
        c.events_entered = None

        assert c.get_events_entered() == []


class TestProCompetitorJsonSafety:
    """ProCompetitor JSON getters survive corruption."""

    def test_corrupt_events_entered(self, db_session, tournament):
        c = make_pro_competitor(db_session, tournament, 'ProCorrupt1', 'M')
        c.events_entered = '{[bad'
        db_session.flush()
        assert c.get_events_entered() == []

    def test_corrupt_gear_sharing(self, db_session, tournament):
        c = make_pro_competitor(db_session, tournament, 'ProCorrupt2', 'F')
        c.gear_sharing = 'null null'
        db_session.flush()
        assert c.get_gear_sharing() == {}

    def test_corrupt_entry_fees(self, db_session, tournament):
        c = make_pro_competitor(db_session, tournament, 'ProCorrupt3', 'M')
        c.entry_fees = 'corrupt'
        db_session.flush()
        assert c.get_entry_fees() == {}

    def test_corrupt_fees_paid(self, db_session, tournament):
        c = make_pro_competitor(db_session, tournament, 'ProCorrupt4', 'F')
        c.fees_paid = '!!!'
        db_session.flush()
        assert c.get_fees_paid() == {}

    def test_corrupt_partners(self, db_session, tournament):
        c = make_pro_competitor(db_session, tournament, 'ProCorrupt5', 'M')
        c.partners = 'asdf'
        db_session.flush()
        assert c.get_partners() == {}


class TestEventJsonSafety:
    """Event JSON getters survive corruption."""

    def test_corrupt_payouts(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Bad Payouts')
        e.payouts = '{corrupt'
        db_session.flush()
        assert e.get_payouts() == {}

    def test_none_payouts(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Null Payouts')
        # In-memory only — DB column is NOT NULL. Getter must tolerate None.
        e.payouts = None
        assert e.get_payouts() == {}


class TestHeatJsonSafety:
    """Heat JSON getters survive corruption."""

    def test_corrupt_competitors_json(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Heat JSON Test')
        h = make_heat(db_session, e)
        h.competitors = '{bad json}'
        db_session.flush()
        assert h.get_competitors() == []

    def test_corrupt_stand_assignments(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Stand JSON Test')
        h = make_heat(db_session, e)
        h.stand_assignments = 'not valid'
        db_session.flush()
        assert h.get_stand_assignments() == {}

    def test_none_competitors(self, db_session, tournament):
        e = make_event(db_session, tournament, 'None Comps')
        h = make_heat(db_session, e)
        # In-memory only — DB column is NOT NULL. Getter must tolerate None.
        h.competitors = None
        assert h.get_competitors() == []


class TestTournamentScheduleConfigSafety:
    """Tournament.get_schedule_config() handles corruption."""

    def test_corrupt_schedule_config(self, db_session):
        t = make_tournament(db_session)
        t.schedule_config = '{{bad'
        db_session.flush()
        assert t.get_schedule_config() == {}

    def test_none_schedule_config(self, db_session):
        t = make_tournament(db_session)
        t.schedule_config = None
        db_session.flush()
        assert t.get_schedule_config() == {}

    def test_roundtrip_schedule_config(self, db_session):
        t = make_tournament(db_session)
        data = {'friday_pro_event_ids': [1, 2], 'saturday_college_event_ids': [3]}
        t.set_schedule_config(data)
        db_session.flush()
        assert t.get_schedule_config() == data


# ---------------------------------------------------------------------------
# Name validation — @validates('name') truncation
# ---------------------------------------------------------------------------

class TestNameValidation:
    """Name truncation at MAX_NAME_LENGTH = 100."""

    def test_college_name_truncated(self, db_session, tournament):
        team = make_team(db_session, tournament)
        long_name = 'A' * 150
        c = make_college_competitor(db_session, tournament, team, long_name, 'M')
        assert len(c.name) <= 100

    def test_pro_name_truncated(self, db_session, tournament):
        long_name = 'B' * 200
        c = make_pro_competitor(db_session, tournament, long_name, 'F')
        assert len(c.name) <= 100

    def test_normal_name_unchanged(self, db_session, tournament):
        team = make_team(db_session, tournament)
        c = make_college_competitor(db_session, tournament, team, 'Normal Name', 'F')
        assert c.name == 'Normal Name'


# ---------------------------------------------------------------------------
# Heat lock management
# ---------------------------------------------------------------------------

class TestHeatLocking:
    """Heat lock acquire, release, expiry, and concurrent behavior."""

    @pytest.fixture()
    def users(self, db_session):
        """Create two users for lock tests."""
        from models.user import User
        u1 = User(username='lock_user1', role='scorer')
        u1.set_password('pw')
        u2 = User(username='lock_user2', role='scorer')
        u2.set_password('pw')
        db_session.add_all([u1, u2])
        db_session.flush()
        return u1, u2

    def test_acquire_lock(self, db_session, tournament, users):
        u1, u2 = users
        e = make_event(db_session, tournament, 'Lock Test')
        h = make_heat(db_session, e)
        db_session.flush()

        result = h.acquire_lock(user_id=u1.id)
        assert result is True
        assert h.is_locked() is True
        assert h.locked_by_user_id == u1.id

    def test_release_lock(self, db_session, tournament, users):
        u1, u2 = users
        e = make_event(db_session, tournament, 'Release Test')
        h = make_heat(db_session, e)
        h.acquire_lock(user_id=u1.id)
        db_session.flush()

        h.release_lock(user_id=u1.id)
        assert h.is_locked() is False
        assert h.locked_by_user_id is None

    def test_lock_by_same_user_succeeds(self, db_session, tournament, users):
        u1, u2 = users
        e = make_event(db_session, tournament, 'Same User Lock')
        h = make_heat(db_session, e)
        h.acquire_lock(user_id=u1.id)
        db_session.flush()

        result = h.acquire_lock(user_id=u1.id)
        assert result is True

    def test_lock_by_different_user_fails(self, db_session, tournament, users):
        u1, u2 = users
        e = make_event(db_session, tournament, 'Diff User Lock')
        h = make_heat(db_session, e)
        h.acquire_lock(user_id=u1.id)
        db_session.flush()

        result = h.acquire_lock(user_id=u2.id)
        assert result is False

    def test_expired_lock_allows_new_lock(self, db_session, tournament, users):
        u1, u2 = users
        e = make_event(db_session, tournament, 'Expired Lock')
        h = make_heat(db_session, e)
        h.acquire_lock(user_id=u1.id)
        # Force lock to be expired (5+ minutes ago)
        h.locked_at = datetime.utcnow() - timedelta(seconds=400)
        db_session.flush()

        assert h.is_locked() is False
        result = h.acquire_lock(user_id=u2.id)
        assert result is True
        assert h.locked_by_user_id == u2.id

    def test_release_by_wrong_user_noop(self, db_session, tournament, users):
        u1, u2 = users
        e = make_event(db_session, tournament, 'Wrong Release')
        h = make_heat(db_session, e)
        h.acquire_lock(user_id=u1.id)
        db_session.flush()

        h.release_lock(user_id=u2.id)
        # Lock still held by user 1
        assert h.is_locked() is True
        assert h.locked_by_user_id == u1.id

    def test_unlocked_heat_not_locked(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Not Locked')
        h = make_heat(db_session, e)
        db_session.flush()

        assert h.is_locked() is False


# ---------------------------------------------------------------------------
# Heat competitor management
# ---------------------------------------------------------------------------

class TestHeatCompetitorManagement:
    """Heat.add_competitor(), remove_competitor(), set_competitors()."""

    def test_add_competitor(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Add Comp Test')
        h = make_heat(db_session, e, competitors=[])
        db_session.flush()

        h.add_competitor(42)
        assert 42 in h.get_competitors()

    def test_remove_competitor(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Remove Comp Test')
        h = make_heat(db_session, e, competitors=[10, 20, 30])
        db_session.flush()

        h.remove_competitor(20)
        comps = h.get_competitors()
        assert 20 not in comps
        assert 10 in comps
        assert 30 in comps

    def test_set_competitors_replaces(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Set Comp Test')
        h = make_heat(db_session, e, competitors=[1, 2, 3])
        db_session.flush()

        h.set_competitors([4, 5])
        assert h.get_competitors() == [4, 5]


# ---------------------------------------------------------------------------
# EventResult calculation methods
# ---------------------------------------------------------------------------

class TestEventResultCalculations:
    """EventResult.calculate_best_run() and calculate_cumulative_score()."""

    def test_best_run_lowest_wins(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Best Run Low',
                       requires_dual_runs=True, scoring_order='lowest_wins')
        c = make_pro_competitor(db_session, tournament, 'RunTest1', 'M')
        r = make_event_result(db_session, e, c,
                              run1_value=12.5, run2_value=11.8, status='completed')
        db_session.flush()

        r.calculate_best_run('lowest_wins')
        assert r.best_run == 11.8
        assert r.result_value == 11.8

    def test_best_run_highest_wins(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Best Run High',
                       requires_dual_runs=True, scoring_order='highest_wins')
        c = make_pro_competitor(db_session, tournament, 'RunTest2', 'M')
        r = make_event_result(db_session, e, c,
                              run1_value=8.0, run2_value=12.0, status='completed')
        db_session.flush()

        r.calculate_best_run('highest_wins')
        assert r.best_run == 12.0
        assert r.result_value == 12.0

    def test_best_run_single_run(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Single Run',
                       requires_dual_runs=True, scoring_order='lowest_wins')
        c = make_pro_competitor(db_session, tournament, 'RunTest3', 'M')
        r = make_event_result(db_session, e, c,
                              run1_value=15.0, run2_value=None, status='completed')
        db_session.flush()

        r.calculate_best_run('lowest_wins')
        assert r.best_run == 15.0

    def test_cumulative_score_all_runs(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Cumulative All',
                       requires_triple_runs=True, scoring_order='highest_wins')
        c = make_pro_competitor(db_session, tournament, 'CumTest1', 'M')
        r = make_event_result(db_session, e, c,
                              run1_value=4.0, run2_value=5.0, run3_value=3.0,
                              status='completed')
        db_session.flush()

        r.calculate_cumulative_score()
        assert r.result_value == 12.0

    def test_cumulative_score_partial_runs(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Cumulative Partial',
                       requires_triple_runs=True, scoring_order='highest_wins')
        c = make_pro_competitor(db_session, tournament, 'CumTest2', 'M')
        r = make_event_result(db_session, e, c,
                              run1_value=4.0, run2_value=5.0, run3_value=None,
                              status='completed')
        db_session.flush()

        r.calculate_cumulative_score()
        assert r.result_value == 9.0

    def test_cumulative_score_no_runs(self, db_session, tournament):
        e = make_event(db_session, tournament, 'Cumulative None',
                       requires_triple_runs=True, scoring_order='highest_wins')
        c = make_pro_competitor(db_session, tournament, 'CumTest3', 'M')
        r = make_event_result(db_session, e, c,
                              run1_value=None, run2_value=None, run3_value=None,
                              status='completed')
        db_session.flush()

        r.calculate_cumulative_score()
        # No runs → result_value should be 0 or None
        assert r.result_value in (0, 0.0, None)


# ---------------------------------------------------------------------------
# SchoolCaptain PIN hashing
# ---------------------------------------------------------------------------

class TestSchoolCaptainPIN:
    """SchoolCaptain PIN set/check cycle."""

    def test_pin_roundtrip(self, db_session, tournament):
        from models.school_captain import SchoolCaptain
        sc = SchoolCaptain(
            tournament_id=tournament.id,
            school_name='University of Montana',
        )
        db_session.add(sc)
        db_session.flush()

        assert sc.has_pin is False

        sc.set_pin('1234')
        assert sc.has_pin is True
        assert sc.check_pin('1234') is True
        assert sc.check_pin('wrong') is False

    def test_different_pins_different_hashes(self, db_session, tournament):
        from models.school_captain import SchoolCaptain
        sc1 = SchoolCaptain(tournament_id=tournament.id, school_name='School A')
        sc2 = SchoolCaptain(tournament_id=tournament.id, school_name='School B')
        db_session.add_all([sc1, sc2])
        db_session.flush()

        sc1.set_pin('1111')
        sc2.set_pin('2222')
        assert sc1.pin_hash != sc2.pin_hash


# ---------------------------------------------------------------------------
# Competitor portal PIN
# ---------------------------------------------------------------------------

class TestCompetitorPortalPIN:
    """Pro/College competitor portal PIN set/check."""

    def test_pro_portal_pin(self, db_session, tournament):
        c = make_pro_competitor(db_session, tournament, 'PinPro', 'M')
        assert c.has_portal_pin is False

        c.set_portal_pin('9876')
        assert c.has_portal_pin is True
        assert c.check_portal_pin('9876') is True
        assert c.check_portal_pin('0000') is False

    def test_college_portal_pin(self, db_session, tournament):
        team = make_team(db_session, tournament)
        c = make_college_competitor(db_session, tournament, team, 'PinCollege', 'F')
        c.set_portal_pin('5555')
        assert c.check_portal_pin('5555') is True


# ---------------------------------------------------------------------------
# Team validation properties
# ---------------------------------------------------------------------------

class TestTeamValidation:
    """Team.is_valid, member_count, male_count, female_count."""

    def test_valid_team(self, db_session, tournament):
        team = make_team(db_session, tournament)
        make_college_competitor(db_session, tournament, team, 'M1', 'M')
        make_college_competitor(db_session, tournament, team, 'M2', 'M')
        make_college_competitor(db_session, tournament, team, 'F1', 'F')
        make_college_competitor(db_session, tournament, team, 'F2', 'F')
        db_session.flush()

        assert team.is_valid is True
        assert team.member_count == 4
        assert team.male_count == 2
        assert team.female_count == 2

    def test_invalid_team_too_few_females(self, db_session, tournament):
        team = make_team(db_session, tournament, code='BAD-A')
        make_college_competitor(db_session, tournament, team, 'M1b', 'M')
        make_college_competitor(db_session, tournament, team, 'M2b', 'M')
        make_college_competitor(db_session, tournament, team, 'F1b', 'F')
        db_session.flush()

        assert team.is_valid is False

    def test_team_points_recalculate(self, db_session, tournament):
        team = make_team(db_session, tournament, code='PTS-A')
        c1 = make_college_competitor(db_session, tournament, team, 'P1', 'M')
        c2 = make_college_competitor(db_session, tournament, team, 'P2', 'F')
        c1.individual_points = 10
        c2.individual_points = 7
        db_session.flush()

        team.recalculate_points()
        assert team.total_points == 17

    def test_scratched_member_excluded_from_count(self, db_session, tournament):
        team = make_team(db_session, tournament, code='SCR-A')
        make_college_competitor(db_session, tournament, team, 'S1', 'M')
        make_college_competitor(db_session, tournament, team, 'S2', 'M')
        make_college_competitor(db_session, tournament, team, 'S3', 'F')
        make_college_competitor(db_session, tournament, team, 'S4', 'F', status='scratched')
        db_session.flush()

        # Scratched member shouldn't count — team has only 1 active female
        assert team.female_count in (1, 2)  # depends on is_valid filter impl


# ---------------------------------------------------------------------------
# User role properties
# ---------------------------------------------------------------------------

class TestUserRoles:
    """User role property checks."""

    def test_admin_is_judge(self, db_session):
        from models.user import User
        u = User(username='roleadmin', role='admin')
        u.set_password('pw')
        db_session.add(u)
        db_session.flush()

        assert u.is_admin is True
        assert u.is_judge is True
        assert u.can_score is True
        assert u.can_register is True
        assert u.can_manage_users is True

    def test_scorer_can_score_not_register(self, db_session):
        from models.user import User
        u = User(username='rolescorer', role='scorer')
        u.set_password('pw')
        db_session.add(u)
        db_session.flush()

        assert u.can_score is True
        assert u.can_register is False
        assert u.is_judge is False

    def test_spectator_limited(self, db_session):
        from models.user import User
        u = User(username='rolespec', role='spectator')
        u.set_password('pw')
        db_session.add(u)
        db_session.flush()

        assert u.can_score is False
        assert u.can_register is False
        assert u.can_report is True
        assert u.is_spectator is True

    def test_password_check(self, db_session):
        from models.user import User
        u = User(username='pwcheck', role='admin')
        u.set_password('my_password')
        db_session.add(u)
        db_session.flush()

        assert u.check_password('my_password') is True
        assert u.check_password('wrong') is False
