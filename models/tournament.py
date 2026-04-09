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
        """Return top male college competitors by individual points.

        Phase 5 (V2.8.0): tiebreak chain per the AWFC rule —
            1. individual_points DESC
            2. count of 1st-place finishes DESC
            3. count of 2nd-place finishes DESC
            4. ... through 6th
            5. name ASC (deterministic stable sort; ties beyond placement
               counts are flagged via get_bull_of_woods_with_tiebreak_data)

        Returns the same [CollegeCompetitor] shape as before so existing
        callers continue to work.
        """
        return self._bull_belle_query('M', limit)

    def get_belle_of_woods(self, limit=5):
        """Return top female college competitors with the same tiebreak chain
        as get_bull_of_woods.  See that method's docstring for details."""
        return self._bull_belle_query('F', limit)

    def _bull_belle_query(self, gender: str, limit: int):
        """Shared implementation for Bull/Belle ordering with placement-count tiebreak.

        Builds a single SQL query that joins college_competitors to event_results
        (filtered to finalized college events only), then orders by:
            individual_points DESC,
            COUNT(*) FILTER (final_position=1) DESC,
            COUNT(*) FILTER (final_position=2) DESC,
            ... through 6,
            name ASC

        Uses ``COUNT(*) FILTER (WHERE ...)`` which is PG-native and supported
        by SQLite >= 3.30 (released 2019; both prod PG and dev SQLite are well
        above this).  Falls back to a portable CASE-based pivot for absolute
        safety on older SQLite by computing each count via SUM(CASE WHEN ...).
        """
        from sqlalchemy import case, func

        from .competitor import CollegeCompetitor
        from .event import EventResult

        # SUM(CASE) is the maximally-portable way to do conditional counts.
        # Equivalent to COUNT(*) FILTER (WHERE final_position=N) on PG / modern
        # SQLite, but works on every SQLAlchemy backend without dialect probes.
        def _count_at(position: int):
            return func.coalesce(
                func.sum(case((EventResult.final_position == position, 1), else_=0)),
                0,
            )

        # Subquery: for each competitor, the placement counts.
        placements = (
            db.session.query(
                EventResult.competitor_id.label('competitor_id'),
                _count_at(1).label('p1'),
                _count_at(2).label('p2'),
                _count_at(3).label('p3'),
                _count_at(4).label('p4'),
                _count_at(5).label('p5'),
                _count_at(6).label('p6'),
            )
            .filter(EventResult.competitor_type == 'college')
            .filter(EventResult.status == 'completed')
            .group_by(EventResult.competitor_id)
            .subquery()
        )

        # Main query: left-join the placements subquery so competitors with
        # zero results still appear (their counts will be NULL → coalesced 0).
        query = (
            db.session.query(CollegeCompetitor)
            .outerjoin(placements, placements.c.competitor_id == CollegeCompetitor.id)
            .filter(CollegeCompetitor.tournament_id == self.id)
            .filter(CollegeCompetitor.status == 'active')
            .filter(CollegeCompetitor.gender == gender)
            .order_by(
                CollegeCompetitor.individual_points.desc(),
                func.coalesce(placements.c.p1, 0).desc(),
                func.coalesce(placements.c.p2, 0).desc(),
                func.coalesce(placements.c.p3, 0).desc(),
                func.coalesce(placements.c.p4, 0).desc(),
                func.coalesce(placements.c.p5, 0).desc(),
                func.coalesce(placements.c.p6, 0).desc(),
                CollegeCompetitor.name,
            )
        )
        if limit:
            query = query.limit(limit)
        return query.all()

    def get_bull_belle_with_tiebreak_data(self, gender: str, limit: int = 5):
        """Like _bull_belle_query but also returns the placement counts and
        a tied_with_next flag for the UI to render a "TIE — manual resolution
        required" indicator.

        Returns: list of dicts with keys
          - competitor: the CollegeCompetitor
          - placements: dict {1: count, 2: count, ..., 6: count}
          - tied_with_next: True if this row's (points, p1..p6) tuple equals
            the next row's tuple — i.e., the placement-count chain failed to
            break the tie and a coin flip is required.
        """
        from sqlalchemy import case, func

        from .competitor import CollegeCompetitor
        from .event import EventResult

        def _count_at(position: int):
            return func.coalesce(
                func.sum(case((EventResult.final_position == position, 1), else_=0)),
                0,
            )

        placements = (
            db.session.query(
                EventResult.competitor_id.label('competitor_id'),
                _count_at(1).label('p1'),
                _count_at(2).label('p2'),
                _count_at(3).label('p3'),
                _count_at(4).label('p4'),
                _count_at(5).label('p5'),
                _count_at(6).label('p6'),
            )
            .filter(EventResult.competitor_type == 'college')
            .filter(EventResult.status == 'completed')
            .group_by(EventResult.competitor_id)
            .subquery()
        )

        rows = (
            db.session.query(
                CollegeCompetitor,
                func.coalesce(placements.c.p1, 0).label('p1'),
                func.coalesce(placements.c.p2, 0).label('p2'),
                func.coalesce(placements.c.p3, 0).label('p3'),
                func.coalesce(placements.c.p4, 0).label('p4'),
                func.coalesce(placements.c.p5, 0).label('p5'),
                func.coalesce(placements.c.p6, 0).label('p6'),
            )
            .outerjoin(placements, placements.c.competitor_id == CollegeCompetitor.id)
            .filter(CollegeCompetitor.tournament_id == self.id)
            .filter(CollegeCompetitor.status == 'active')
            .filter(CollegeCompetitor.gender == gender)
            .order_by(
                CollegeCompetitor.individual_points.desc(),
                func.coalesce(placements.c.p1, 0).desc(),
                func.coalesce(placements.c.p2, 0).desc(),
                func.coalesce(placements.c.p3, 0).desc(),
                func.coalesce(placements.c.p4, 0).desc(),
                func.coalesce(placements.c.p5, 0).desc(),
                func.coalesce(placements.c.p6, 0).desc(),
                CollegeCompetitor.name,
            )
            .limit(limit)
            .all()
        )

        # Build the result list with a tied_with_next flag.  Two consecutive
        # rows are "tied through the chain" iff their entire tuple
        # (individual_points, p1, p2, p3, p4, p5, p6) is equal — at that point
        # the only thing distinguishing them is the alphabetical name fallback,
        # which is a stand-in for the manual coin flip per AWFC.
        out = []
        for i, row in enumerate(rows):
            comp = row[0]
            placements_dict = {n: int(row[n]) for n in (1, 2, 3, 4, 5, 6)}
            tied_with_next = False
            if i + 1 < len(rows):
                next_row = rows[i + 1]
                next_comp = next_row[0]
                same_points = comp.individual_points == next_comp.individual_points
                same_chain = all(int(row[n]) == int(next_row[n]) for n in (1, 2, 3, 4, 5, 6))
                tied_with_next = same_points and same_chain
            out.append({
                'competitor': comp,
                'placements': placements_dict,
                'tied_with_next': tied_with_next,
            })
        return out
