"""
Tournament model for managing overall tournament state.
"""
from datetime import datetime

from config import TournamentStatus  # noqa: F401 — re-exported for convenience
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

    # Status tracking — use TournamentStatus constants (from config) not bare strings.
    status = db.Column(db.String(50), nullable=False, default=TournamentStatus.SETUP)

    # Shirt logistics — True when the show provides shirts; controls shirt-size collection on pro entry
    providing_shirts = db.Column(db.Boolean, nullable=False, default=False)

    # Schedule config — persists friday_pro_event_ids / saturday_college_event_ids across sessions
    schedule_config = db.Column(db.Text, nullable=True)

    # Timestamps
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    teams = db.relationship('Team', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')
    college_competitors = db.relationship('CollegeCompetitor', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')
    pro_competitors = db.relationship('ProCompetitor', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')
    events = db.relationship('Event', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')
    wood_configs = db.relationship('WoodConfig', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')

    def get_schedule_config(self) -> dict:
        """Return parsed schedule config dict (friday/saturday event selections)."""
        import json as _json
        try:
            return _json.loads(self.schedule_config or '{}')
        except Exception:
            return {}

    def set_schedule_config(self, data: dict):
        """Persist schedule config dict to the DB column."""
        import json as _json
        self.schedule_config = _json.dumps(data)

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
        from .team import Team
        return (
            Team.query
            .filter_by(tournament_id=self.id, status='active')
            .order_by(Team.total_points.desc(), Team.team_code)
            .all()
        )

    def get_bull_of_woods(self, limit=5):
        """Return top male college competitors by individual points."""
        from .competitor import CollegeCompetitor
        return (
            CollegeCompetitor.query
            .filter_by(tournament_id=self.id, gender='M', status='active')
            .order_by(CollegeCompetitor.individual_points.desc(), CollegeCompetitor.name)
            .limit(limit)
            .all()
        )

    def get_belle_of_woods(self, limit=5):
        """Return top female college competitors by individual points."""
        from .competitor import CollegeCompetitor
        return (
            CollegeCompetitor.query
            .filter_by(tournament_id=self.id, gender='F', status='active')
            .order_by(CollegeCompetitor.individual_points.desc(), CollegeCompetitor.name)
            .limit(limit)
            .all()
        )
