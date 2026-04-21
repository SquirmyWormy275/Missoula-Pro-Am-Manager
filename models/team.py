"""
Team model for college competition teams.
"""
import sqlalchemy as sa

from database import db


class Team(db.Model):
    """Represents a college team (e.g., UM-A, CSU-B)."""

    __tablename__ = 'teams'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)

    # Team identification
    team_code = db.Column(db.String(20), nullable=False)  # e.g., "UM-A", "CSU-B"
    school_name = db.Column(db.String(200), nullable=False)  # e.g., "University of Montana"
    school_abbreviation = db.Column(db.String(20), nullable=False)  # e.g., "UM", "CSU"

    # Scoring.
    # Changed Integer → Numeric(8, 2) in V2.8.0 (Phase 1B of scoring fix) so that
    # team totals can hold the fractional individual_points produced by split-tie
    # placements.  Numeric(8, 2) supports values up to 999999.99 — far above any
    # plausible team total even after many split ties across all members.
    total_points = db.Column(
        db.Numeric(8, 2),
        nullable=False,
        default=0,
        server_default=sa.text("'0.00'"),
    )

    # Status
    status = db.Column(db.String(20), nullable=False, default='active')  # active, scratched, invalid

    # Validation error tracking (JSON list of structured error dicts; None = no errors recorded)
    validation_errors = db.Column(db.Text, nullable=True)

    # Admin override — when True, validation errors are recorded for display but do NOT
    # flip the team to 'invalid' status. Used for edge-case small rosters (e.g., 5 men + 0
    # women) where a judge has manually decided the team may still compete. Re-validation
    # and Excel re-imports preserve this flag; only an explicit "remove override" action
    # clears it. If re-validation finds zero errors the flag auto-clears (vestigial).
    is_override = db.Column(
        db.Boolean, nullable=False, default=False, server_default=sa.text('false')
    )

    # Relationships
    members = db.relationship('CollegeCompetitor', backref='team', lazy='dynamic')

    __table_args__ = (
        db.UniqueConstraint('tournament_id', 'team_code', name='unique_team_code_per_tournament'),
        db.CheckConstraint("status IN ('active', 'scratched', 'invalid')", name='ck_teams_status_valid'),
        db.CheckConstraint('total_points >= 0', name='ck_teams_total_points_nonnegative'),
    )

    def __repr__(self):
        return f'<Team {self.team_code}>'

    @property
    def member_count(self):
        """Return total number of team members."""
        return self.members.filter_by(status='active').count()

    @property
    def male_count(self):
        """Return count of male team members."""
        return self.members.filter_by(gender='M', status='active').count()

    @property
    def female_count(self):
        """Return count of female team members."""
        return self.members.filter_by(gender='F', status='active').count()

    @property
    def is_valid(self):
        """Check if team meets minimum requirements (min 2 per gender, max 8 total)."""
        return (
            self.male_count >= 2 and
            self.female_count >= 2 and
            self.member_count <= 8
        )

    def recalculate_points(self):
        """Recalculate total team points from all active members."""
        active_members = self.members.filter_by(status='active').all()
        self.total_points = sum(m.individual_points for m in active_members)
        return self.total_points

    def get_members_sorted(self):
        """Return members sorted by individual points (descending)."""
        members = self.members.filter_by(status='active').all()
        return sorted(members, key=lambda m: m.individual_points, reverse=True)

    def get_validation_errors(self):
        """Return list of structured validation error dicts (empty list if none)."""
        import json
        try:
            return json.loads(self.validation_errors or '[]')
        except Exception:
            return []

    def set_validation_errors(self, errors: list):
        """Store structured errors and update team status.

        Normal path: errors → status='invalid', no errors → status='active'.

        Override path: if is_override is True, errors are still written for UI
        display but status stays 'active' — a judge has manually accepted the
        roster despite the violations. If the team becomes genuinely clean
        (errors=[]) the override flag auto-clears since it's vestigial.
        """
        import json
        self.validation_errors = json.dumps(errors)
        if not errors:
            self.status = 'active'
            self.is_override = False
            return
        if self.is_override:
            self.status = 'active'
            return
        self.status = 'invalid'
