"""
Unit tests for services/validation.py

Tests run with pytest and do NOT require a live database — all models are
mocked with SimpleNamespace objects so the pure validation logic can be
exercised in isolation.  Only the validate_all() classmethods need a DB;
those are skipped here.

Run:  pytest tests/test_validation.py -v
"""
import pytest
from types import SimpleNamespace

from services.validation import (
    ValidationError,
    ValidationResult,
    TeamValidator,
    CollegeCompetitorValidator,
    ProCompetitorValidator,
)


# ---------------------------------------------------------------------------
# ValidationError
# ---------------------------------------------------------------------------

class TestValidationError:
    def test_to_dict_returns_all_fields(self):
        err = ValidationError(
            code='SOME_CODE',
            message='Something went wrong',
            field='name',
            entity_id=42,
        )
        d = err.to_dict()
        assert d['code'] == 'SOME_CODE'
        assert d['message'] == 'Something went wrong'
        assert d['field'] == 'name'
        assert d['entity_id'] == 42

    def test_to_dict_defaults_none(self):
        err = ValidationError(code='X', message='msg')
        d = err.to_dict()
        assert d['field'] is None
        assert d['entity_id'] is None


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------

class TestValidationResult:
    def test_is_valid_true_when_no_errors(self):
        vr = ValidationResult()
        assert vr.is_valid is True

    def test_is_valid_false_when_errors_exist(self):
        vr = ValidationResult()
        vr.add_error('CODE', 'msg')
        assert vr.is_valid is False

    def test_has_warnings_false_initially(self):
        vr = ValidationResult()
        assert vr.has_warnings is False

    def test_has_warnings_true_after_add_warning(self):
        vr = ValidationResult()
        vr.add_warning('WARN', 'a warning')
        assert vr.has_warnings is True

    def test_add_error_accumulates(self):
        vr = ValidationResult()
        vr.add_error('E1', 'm1')
        vr.add_error('E2', 'm2')
        assert len(vr.errors) == 2

    def test_add_warning_accumulates(self):
        vr = ValidationResult()
        vr.add_warning('W1', 'w1')
        vr.add_warning('W2', 'w2')
        assert len(vr.warnings) == 2

    def test_merge_combines_results(self):
        vr1 = ValidationResult()
        vr1.add_error('E1', 'e1')
        vr1.add_warning('W1', 'w1')

        vr2 = ValidationResult()
        vr2.add_error('E2', 'e2')
        vr2.add_warning('W2', 'w2')

        vr1.merge(vr2)
        assert len(vr1.errors) == 2
        assert len(vr1.warnings) == 2

    def test_to_dict_structure(self):
        vr = ValidationResult()
        vr.add_error('E', 'e msg', field='f', entity_id=1)
        vr.add_warning('W', 'w msg')
        d = vr.to_dict()
        assert d['is_valid'] is False
        assert d['has_warnings'] is True
        assert d['error_count'] == 1
        assert d['warning_count'] == 1
        assert len(d['errors']) == 1
        assert len(d['warnings']) == 1
        assert d['errors'][0]['code'] == 'E'
        assert d['warnings'][0]['code'] == 'W'

    def test_to_dict_valid_empty(self):
        vr = ValidationResult()
        d = vr.to_dict()
        assert d['is_valid'] is True
        assert d['has_warnings'] is False
        assert d['error_count'] == 0
        assert d['warning_count'] == 0
        assert d['errors'] == []
        assert d['warnings'] == []


# ---------------------------------------------------------------------------
# Helpers — lightweight mock team/competitor objects
# ---------------------------------------------------------------------------

def _team(team_code='UM-A', id=1, member_count=6, male_count=3, female_count=3):
    return SimpleNamespace(
        team_code=team_code,
        id=id,
        member_count=member_count,
        male_count=male_count,
        female_count=female_count,
    )


def _college_comp(name='Alice', gender='F', team_id=1, events=None, id=1):
    ev = events if events is not None else ['Underhand Speed']
    return SimpleNamespace(
        name=name,
        gender=gender,
        team_id=team_id,
        get_events_entered=lambda: ev,
        id=id,
    )


def _pro_comp(name='Bob', gender='M', is_ala_member=True, events=None,
              fees_balance=0.0, id=1):
    ev = events if events is not None else ['Springboard']
    return SimpleNamespace(
        name=name,
        gender=gender,
        is_ala_member=is_ala_member,
        get_events_entered=lambda: ev,
        fees_balance=fees_balance,
        id=id,
    )


# ---------------------------------------------------------------------------
# TeamValidator
# ---------------------------------------------------------------------------

class TestTeamValidator:
    def test_valid_minimum_team_has_warning(self):
        # 4 members: 2M/2F — valid but at minimum, expects TEAM_AT_MINIMUM warning
        team = _team(member_count=4, male_count=2, female_count=2)
        result = TeamValidator.validate(team)
        assert result.is_valid is True
        assert result.has_warnings is True
        codes = [w.code for w in result.warnings]
        assert 'TEAM_AT_MINIMUM' in codes

    def test_valid_comfortable_team_no_warnings(self):
        team = _team(member_count=6, male_count=3, female_count=3)
        result = TeamValidator.validate(team)
        assert result.is_valid is True
        assert result.has_warnings is False

    def test_too_few_members_error(self):
        team = _team(member_count=2, male_count=1, female_count=1)
        result = TeamValidator.validate(team)
        assert result.is_valid is False
        codes = [e.code for e in result.errors]
        assert 'TEAM_TOO_SMALL' in codes

    def test_too_many_members_error(self):
        team = _team(member_count=9, male_count=5, female_count=4)
        result = TeamValidator.validate(team)
        assert result.is_valid is False
        codes = [e.code for e in result.errors]
        assert 'TEAM_TOO_LARGE' in codes

    def test_insufficient_males_error(self):
        team = _team(member_count=5, male_count=1, female_count=4)
        result = TeamValidator.validate(team)
        assert result.is_valid is False
        codes = [e.code for e in result.errors]
        assert 'INSUFFICIENT_MALES' in codes

    def test_insufficient_females_error(self):
        team = _team(member_count=5, male_count=4, female_count=1)
        result = TeamValidator.validate(team)
        assert result.is_valid is False
        codes = [e.code for e in result.errors]
        assert 'INSUFFICIENT_FEMALES' in codes

    def test_multiple_issues_produce_multiple_errors(self):
        # 1 member: triggers TOO_SMALL, INSUFFICIENT_MALES, INSUFFICIENT_FEMALES
        team = _team(member_count=1, male_count=0, female_count=1)
        result = TeamValidator.validate(team)
        assert result.is_valid is False
        assert len(result.errors) >= 2


# ---------------------------------------------------------------------------
# CollegeCompetitorValidator
# ---------------------------------------------------------------------------

class TestCollegeCompetitorValidator:
    def test_valid_competitor(self):
        comp = _college_comp(name='Alice', gender='F', team_id=1,
                             events=['Underhand Speed', 'Single Buck'])
        result = CollegeCompetitorValidator.validate(comp)
        assert result.is_valid is True

    def test_missing_name_error(self):
        comp = _college_comp(name='', gender='F', team_id=1, events=['Underhand Speed'])
        result = CollegeCompetitorValidator.validate(comp)
        assert result.is_valid is False
        codes = [e.code for e in result.errors]
        assert 'MISSING_FIELD' in codes

    def test_no_team_error(self):
        comp = _college_comp(name='Alice', gender='F', team_id=None,
                             events=['Underhand Speed'])
        result = CollegeCompetitorValidator.validate(comp)
        assert result.is_valid is False
        codes = [e.code for e in result.errors]
        assert 'NO_TEAM' in codes

    def test_no_events_is_warning_not_error(self):
        comp = _college_comp(name='Alice', gender='F', team_id=1, events=[])
        result = CollegeCompetitorValidator.validate(comp)
        # NO_EVENTS is a warning, not an error
        assert result.is_valid is True
        assert result.has_warnings is True
        codes = [w.code for w in result.warnings]
        assert 'NO_EVENTS' in codes


# ---------------------------------------------------------------------------
# ProCompetitorValidator
# ---------------------------------------------------------------------------

class TestProCompetitorValidator:
    def test_valid_ala_member_no_fees_has_events(self):
        comp = _pro_comp(name='Bob', gender='M', is_ala_member=True,
                         events=['Springboard'], fees_balance=0.0)
        result = ProCompetitorValidator.validate(comp)
        assert result.is_valid is True
        assert result.has_warnings is False

    def test_non_ala_member_warning(self):
        comp = _pro_comp(name='Bob', gender='M', is_ala_member=False,
                         events=['Springboard'], fees_balance=0.0)
        result = ProCompetitorValidator.validate(comp)
        # Should be valid but with a warning
        assert result.is_valid is True
        codes = [w.code for w in result.warnings]
        assert 'NOT_ALA_MEMBER' in codes

    def test_unpaid_fees_warning(self):
        comp = _pro_comp(name='Bob', gender='M', is_ala_member=True,
                         events=['Springboard'], fees_balance=150.0)
        result = ProCompetitorValidator.validate(comp)
        assert result.is_valid is True
        codes = [w.code for w in result.warnings]
        assert 'UNPAID_FEES' in codes

    def test_no_events_warning(self):
        comp = _pro_comp(name='Bob', gender='M', is_ala_member=True,
                         events=[], fees_balance=0.0)
        result = ProCompetitorValidator.validate(comp)
        assert result.is_valid is True
        codes = [w.code for w in result.warnings]
        assert 'NO_EVENTS' in codes

    def test_missing_name_error(self):
        comp = _pro_comp(name='', gender='M', is_ala_member=True,
                         events=['Springboard'], fees_balance=0.0)
        result = ProCompetitorValidator.validate(comp)
        assert result.is_valid is False
        codes = [e.code for e in result.errors]
        assert 'MISSING_FIELD' in codes

    def test_multiple_warnings_can_coexist(self):
        comp = _pro_comp(name='Bob', gender='M', is_ala_member=False,
                         events=[], fees_balance=50.0)
        result = ProCompetitorValidator.validate(comp)
        # Valid with 3 warnings: NOT_ALA_MEMBER, NO_EVENTS, UNPAID_FEES
        assert result.is_valid is True
        warn_codes = [w.code for w in result.warnings]
        assert 'NOT_ALA_MEMBER' in warn_codes
        assert 'NO_EVENTS' in warn_codes
        assert 'UNPAID_FEES' in warn_codes
