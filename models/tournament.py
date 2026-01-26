"""
Tournament model for managing overall tournament state.
"""
from datetime import datetime
from database import db


class Tournament(db.Model):
    """Represents an overall tournament (e.g., Missoula Pro Am 2026)."""

    __tablename__ = 'tournaments'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    year = db.Column(db.Integer, nullable=False)

    # Dates
    college_date = db.Column(db.Date, nullable=True)  # Friday
    pro_date = db.Column(db.Date, nullable=True)      # Saturday
    friday_feature_date = db.Column(db.Date, nullable=True)  # Friday night

    # Status tracking
    status = db.Column(db.String(50), default='setup')  # setup, college_active, pro_active, completed

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    teams = db.relationship('Team', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')
    college_competitors = db.relationship('CollegeCompetitor', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')
    pro_competitors = db.relationship('ProCompetitor', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')
    events = db.relationship('Event', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Tournament {self.name} {self.year}>'

    @property
    def college_team_count(self):
        """Return count of college teams."""
        return self.teams.count()

    @property
    def college_competitor_count(self):
        """Return count of college competitors."""
        return self.college_competitors.count()

    @property
    def pro_competitor_count(self):
        """Return count of pro competitors."""
        return self.pro_competitors.count()

    def get_team_standings(self):
        """Return teams sorted by total points (descending)."""
        teams = self.teams.all()
        return sorted(teams, key=lambda t: t.total_points, reverse=True)

    def get_bull_of_woods(self, limit=5):
        """Return top male college competitors by individual points."""
        males = self.college_competitors.filter_by(gender='M', status='active').all()
        return sorted(males, key=lambda c: c.individual_points, reverse=True)[:limit]

    def get_belle_of_woods(self, limit=5):
        """Return top female college competitors by individual points."""
        females = self.college_competitors.filter_by(gender='F', status='active').all()
        return sorted(females, key=lambda c: c.individual_points, reverse=True)[:limit]
