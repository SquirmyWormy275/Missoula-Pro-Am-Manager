"""
Team model for college competition teams.
"""
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

    # Scoring
    total_points = db.Column(db.Integer, default=0)

    # Status
    status = db.Column(db.String(20), default='active')  # active, scratched

    # Relationships
    members = db.relationship('CollegeCompetitor', backref='team', lazy='dynamic')

    __table_args__ = (
        db.UniqueConstraint('tournament_id', 'team_code', name='unique_team_code_per_tournament'),
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
