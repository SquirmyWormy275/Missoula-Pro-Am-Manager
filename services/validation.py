"""
Validation service for the Missoula Pro Am Tournament Manager.

Provides comprehensive validation for:
- Team composition (member counts, gender requirements)
- Event entries (max entries, eligibility)
- Competitor data (required fields)
- Heat assignments (stand constraints, gear sharing)
"""
from typing import List, Dict, Optional, Tuple
from models import Tournament, Event, Heat, HeatAssignment
from models.team import Team
from models.competitor import CollegeCompetitor, ProCompetitor
import config


class ValidationError:
    """Represents a single validation error."""

    def __init__(self, code: str, message: str, field: str = None, entity_id: int = None):
        self.code = code
        self.message = message
        self.field = field
        self.entity_id = entity_id

    def to_dict(self) -> dict:
        return {
            'code': self.code,
            'message': self.message,
            'field': self.field,
            'entity_id': self.entity_id
        }


class ValidationResult:
    """Collection of validation results."""

    def __init__(self):
        self.errors: List[ValidationError] = []
        self.warnings: List[ValidationError] = []

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def add_error(self, code: str, message: str, field: str = None, entity_id: int = None):
        self.errors.append(ValidationError(code, message, field, entity_id))

    def add_warning(self, code: str, message: str, field: str = None, entity_id: int = None):
        self.warnings.append(ValidationError(code, message, field, entity_id))

    def merge(self, other: 'ValidationResult'):
        """Merge another ValidationResult into this one."""
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    def to_dict(self) -> dict:
        return {
            'is_valid': self.is_valid,
            'has_warnings': self.has_warnings,
            'error_count': len(self.errors),
            'warning_count': len(self.warnings),
            'errors': [e.to_dict() for e in self.errors],
            'warnings': [w.to_dict() for w in self.warnings]
        }


class TeamValidator:
    """Validates college team composition."""

    MIN_MEMBERS = 4
    MAX_MEMBERS = 8
    MIN_MALE = 2
    MIN_FEMALE = 2

    @classmethod
    def validate(cls, team: Team) -> ValidationResult:
        """Validate a single team."""
        result = ValidationResult()

        # Check member count
        if team.member_count < cls.MIN_MEMBERS:
            result.add_error(
                'TEAM_TOO_SMALL',
                f'Team {team.team_code} has only {team.member_count} members (minimum {cls.MIN_MEMBERS})',
                entity_id=team.id
            )

        if team.member_count > cls.MAX_MEMBERS:
            result.add_error(
                'TEAM_TOO_LARGE',
                f'Team {team.team_code} has {team.member_count} members (maximum {cls.MAX_MEMBERS})',
                entity_id=team.id
            )

        # Check gender requirements
        if team.male_count < cls.MIN_MALE:
            result.add_error(
                'INSUFFICIENT_MALES',
                f'Team {team.team_code} has only {team.male_count} male members (minimum {cls.MIN_MALE})',
                entity_id=team.id
            )

        if team.female_count < cls.MIN_FEMALE:
            result.add_error(
                'INSUFFICIENT_FEMALES',
                f'Team {team.team_code} has only {team.female_count} female members (minimum {cls.MIN_FEMALE})',
                entity_id=team.id
            )

        # Warnings
        if team.member_count == cls.MIN_MEMBERS:
            result.add_warning(
                'TEAM_AT_MINIMUM',
                f'Team {team.team_code} has minimum members - no substitutes available',
                entity_id=team.id
            )

        return result

    @classmethod
    def validate_all(cls, tournament_id: int) -> ValidationResult:
        """Validate all teams in a tournament."""
        result = ValidationResult()
        teams = Team.query.filter_by(tournament_id=tournament_id).all()

        for team in teams:
            result.merge(cls.validate(team))

        return result


class CollegeCompetitorValidator:
    """Validates college competitor data and event entries."""

    MAX_CLOSED_EVENTS = 6
    REQUIRED_FIELDS = ['name', 'gender']

    @classmethod
    def validate(cls, competitor: CollegeCompetitor) -> ValidationResult:
        """Validate a single college competitor."""
        result = ValidationResult()

        # Check required fields
        for field in cls.REQUIRED_FIELDS:
            if not getattr(competitor, field, None):
                result.add_error(
                    'MISSING_FIELD',
                    f'Competitor is missing required field: {field}',
                    field=field,
                    entity_id=competitor.id
                )

        # Check team assignment
        if not competitor.team_id:
            result.add_error(
                'NO_TEAM',
                f'Competitor {competitor.name} is not assigned to a team',
                entity_id=competitor.id
            )

        # Check event entries
        events = competitor.get_events_entered()
        closed_event_count = 0

        for event_name in events:
            if event_name in [e['name'] for e in config.COLLEGE_CLOSED_EVENTS]:
                closed_event_count += 1

        if closed_event_count > cls.MAX_CLOSED_EVENTS:
            result.add_error(
                'TOO_MANY_CLOSED_EVENTS',
                f'Competitor {competitor.name} entered {closed_event_count} closed events (maximum {cls.MAX_CLOSED_EVENTS})',
                entity_id=competitor.id
            )

        # Warning if no events entered
        if len(events) == 0:
            result.add_warning(
                'NO_EVENTS',
                f'Competitor {competitor.name} has no events entered',
                entity_id=competitor.id
            )

        return result

    @classmethod
    def validate_all(cls, tournament_id: int) -> ValidationResult:
        """Validate all college competitors in a tournament."""
        result = ValidationResult()
        competitors = CollegeCompetitor.query.filter_by(tournament_id=tournament_id).all()

        for competitor in competitors:
            result.merge(cls.validate(competitor))

        return result


class ProCompetitorValidator:
    """Validates professional competitor data."""

    REQUIRED_FIELDS = ['name', 'gender']

    @classmethod
    def validate(cls, competitor: ProCompetitor) -> ValidationResult:
        """Validate a single pro competitor."""
        result = ValidationResult()

        # Check required fields
        for field in cls.REQUIRED_FIELDS:
            if not getattr(competitor, field, None):
                result.add_error(
                    'MISSING_FIELD',
                    f'Competitor is missing required field: {field}',
                    field=field,
                    entity_id=competitor.id
                )

        # Check ALA membership
        if not competitor.is_ala_member:
            result.add_warning(
                'NOT_ALA_MEMBER',
                f'Competitor {competitor.name} is not an ALA member',
                entity_id=competitor.id
            )

        # Check event entries
        events = competitor.get_events_entered()
        if len(events) == 0:
            result.add_warning(
                'NO_EVENTS',
                f'Competitor {competitor.name} has no events entered',
                entity_id=competitor.id
            )

        # Check fees
        if competitor.fees_balance > 0:
            result.add_warning(
                'UNPAID_FEES',
                f'Competitor {competitor.name} has unpaid fees: ${competitor.fees_balance:.2f}',
                entity_id=competitor.id
            )

        return result

    @classmethod
    def validate_all(cls, tournament_id: int) -> ValidationResult:
        """Validate all pro competitors in a tournament."""
        result = ValidationResult()
        competitors = ProCompetitor.query.filter_by(tournament_id=tournament_id).all()

        for competitor in competitors:
            result.merge(cls.validate(competitor))

        return result


class HeatValidator:
    """Validates heat assignments for constraint violations."""

    @classmethod
    def validate_gear_sharing(cls, heat: Heat) -> ValidationResult:
        """Check for gear sharing conflicts within a heat."""
        result = ValidationResult()

        assignments = HeatAssignment.query.filter_by(heat_id=heat.id).all()

        # Get all competitor IDs in this heat
        competitor_ids = {a.competitor_id for a in assignments}

        # Check each competitor's gear sharing
        for assignment in assignments:
            if assignment.competitor_type == 'pro':
                competitor = ProCompetitor.query.get(assignment.competitor_id)
                if competitor:
                    gear_sharing = competitor.get_gear_sharing()
                    event_id = str(heat.event_id)

                    if event_id in gear_sharing:
                        partner_name = gear_sharing[event_id]
                        # Find partner by name in this heat
                        for other in assignments:
                            if other.competitor_id != assignment.competitor_id:
                                other_comp = ProCompetitor.query.get(other.competitor_id)
                                if other_comp and other_comp.name == partner_name:
                                    result.add_error(
                                        'GEAR_SHARING_CONFLICT',
                                        f'{competitor.name} shares gear with {partner_name} but both are in Heat {heat.heat_number}',
                                        entity_id=heat.id
                                    )

        return result

    @classmethod
    def validate_stand_constraints(cls, heat: Heat, event: Event) -> ValidationResult:
        """Validate stand constraints for a heat."""
        result = ValidationResult()

        assignments = HeatAssignment.query.filter_by(heat_id=heat.id).all()
        stand_type = event.stand_type

        if not stand_type or stand_type not in config.STAND_CONFIGS:
            return result

        stand_config = config.STAND_CONFIGS[stand_type]
        max_per_heat = stand_config.get('total', 8)

        if len(assignments) > max_per_heat:
            result.add_error(
                'HEAT_OVERCAPACITY',
                f'Heat {heat.heat_number} has {len(assignments)} competitors but stand type {stand_type} only has {max_per_heat} positions',
                entity_id=heat.id
            )

        return result

    @classmethod
    def validate_all(cls, event: Event) -> ValidationResult:
        """Validate all heats for an event."""
        result = ValidationResult()
        heats = Heat.query.filter_by(event_id=event.id).all()

        for heat in heats:
            result.merge(cls.validate_gear_sharing(heat))
            result.merge(cls.validate_stand_constraints(heat, event))

        return result


class TournamentValidator:
    """High-level tournament validation."""

    @classmethod
    def validate_college(cls, tournament_id: int) -> ValidationResult:
        """Validate all college competition data."""
        result = ValidationResult()

        # Validate teams
        result.merge(TeamValidator.validate_all(tournament_id))

        # Validate competitors
        result.merge(CollegeCompetitorValidator.validate_all(tournament_id))

        return result

    @classmethod
    def validate_pro(cls, tournament_id: int) -> ValidationResult:
        """Validate all pro competition data."""
        result = ValidationResult()

        # Validate competitors
        result.merge(ProCompetitorValidator.validate_all(tournament_id))

        # Validate events with heats
        events = Event.query.filter_by(tournament_id=tournament_id, event_type='pro').all()
        for event in events:
            result.merge(HeatValidator.validate_all(event))

        return result

    @classmethod
    def validate_full(cls, tournament_id: int) -> Dict[str, ValidationResult]:
        """Validate entire tournament."""
        return {
            'college': cls.validate_college(tournament_id),
            'pro': cls.validate_pro(tournament_id)
        }


def validate_tournament(tournament_id: int) -> Dict[str, dict]:
    """
    Convenience function to validate an entire tournament.

    Returns dict with college and pro validation results.
    """
    results = TournamentValidator.validate_full(tournament_id)
    return {
        'college': results['college'].to_dict(),
        'pro': results['pro'].to_dict()
    }


def validate_team(team: Team) -> dict:
    """Validate a single team and return dict result."""
    return TeamValidator.validate(team).to_dict()


def validate_competitor(competitor, competitor_type: str = 'college') -> dict:
    """Validate a single competitor and return dict result."""
    if competitor_type == 'college':
        return CollegeCompetitorValidator.validate(competitor).to_dict()
    else:
        return ProCompetitorValidator.validate(competitor).to_dict()
