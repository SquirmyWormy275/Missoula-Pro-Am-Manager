"""
Event and EventResult models for tournament events.
"""
from database import db
import json


class Event(db.Model):
    """Represents a competition event (e.g., Men's Underhand Speed)."""

    __tablename__ = 'events'
    __table_args__ = (
        db.Index('ix_events_tournament_type_status', 'tournament_id', 'event_type', 'status'),
    )

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)

    # Event identification
    name = db.Column(db.String(200), nullable=False)
    event_type = db.Column(db.String(20), nullable=False)  # 'college' or 'pro'
    gender = db.Column(db.String(10), nullable=True)  # 'M', 'F', or None for mixed

    # Scoring configuration
    scoring_type = db.Column(db.String(20), nullable=False)  # 'time', 'score', 'distance', 'hits', 'bracket'
    scoring_order = db.Column(db.String(20), default='lowest_wins')  # 'lowest_wins' or 'highest_wins'

    # Event classification (college only)
    is_open = db.Column(db.Boolean, default=False)  # True = OPEN event, False = CLOSED

    # Event characteristics
    is_partnered = db.Column(db.Boolean, default=False)
    partner_gender_requirement = db.Column(db.String(10), nullable=True)  # 'same', 'mixed', 'any'
    requires_dual_runs = db.Column(db.Boolean, default=False)  # True for Chokerman, Obstacle, Climb

    # Stand configuration
    stand_type = db.Column(db.String(50), nullable=True)
    max_stands = db.Column(db.Integer, nullable=True)

    # Pro event specifics
    has_prelims = db.Column(db.Boolean, default=False)  # True for Partnered Axe Throw

    # Payout configuration (pro only) - stored as JSON
    payouts = db.Column(db.Text, default='{}')  # Dict: position -> amount

    # Status
    status = db.Column(db.String(20), default='pending')  # pending, in_progress, completed

    # Relationships
    heats = db.relationship('Heat', backref='event', lazy='dynamic', cascade='all, delete-orphan')
    results = db.relationship('EventResult', backref='event', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        gender_str = f" ({self.gender})" if self.gender else ""
        return f'<Event {self.name}{gender_str}>'

    @property
    def display_name(self):
        """Return event name with gender prefix if applicable."""
        if self.gender == 'M':
            return f"Men's {self.name}"
        elif self.gender == 'F':
            return f"Women's {self.name}"
        return self.name

    def get_payouts(self):
        """Return dict of position -> payout amount."""
        return json.loads(self.payouts or '{}')

    def set_payouts(self, payout_dict):
        """Set the payout structure."""
        self.payouts = json.dumps(payout_dict)

    def get_payout_for_position(self, position):
        """Get payout amount for a specific position."""
        payouts = self.get_payouts()
        return payouts.get(str(position), 0)

    def get_competitors(self):
        """Return list of competitors entered in this event."""
        # Get from results
        return [r.competitor_name for r in self.results.all()]

    def get_results_sorted(self):
        """Return results sorted by final position."""
        return self.results.order_by(EventResult.final_position).all()


class EventResult(db.Model):
    """Represents a competitor's result in an event."""

    __tablename__ = 'event_results'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'competitor_id', 'competitor_type', name='uq_event_result_competitor'),
        db.Index('ix_event_results_event_status', 'event_id', 'status'),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)

    # Competitor info (flexible - works for both college and pro)
    competitor_id = db.Column(db.Integer, nullable=False)
    competitor_type = db.Column(db.String(20), nullable=False)  # 'college' or 'pro'
    competitor_name = db.Column(db.String(200), nullable=False)

    # For partnered events
    partner_name = db.Column(db.String(200), nullable=True)

    # Results - flexible storage
    result_value = db.Column(db.Float, nullable=True)  # Time in seconds, score, distance, or hits
    result_unit = db.Column(db.String(20), nullable=True)  # 'seconds', 'points', 'feet', 'hits'

    # For dual-run events
    run1_value = db.Column(db.Float, nullable=True)
    run2_value = db.Column(db.Float, nullable=True)
    best_run = db.Column(db.Float, nullable=True)  # The better of the two runs

    # Placement
    final_position = db.Column(db.Integer, nullable=True)  # 1st, 2nd, 3rd, etc.

    # Points awarded (college only)
    points_awarded = db.Column(db.Integer, default=0)

    # Payout (pro only)
    payout_amount = db.Column(db.Float, default=0.0)

    # Score discrepancy flag (#8)
    is_flagged = db.Column(db.Boolean, default=False)

    # Status
    status = db.Column(db.String(20), default='pending')  # pending, completed, scratched, dnf
    version_id = db.Column(db.Integer, nullable=False, default=1)

    __mapper_args__ = {
        'version_id_col': version_id,
    }

    def __repr__(self):
        return f'<EventResult {self.competitor_name} - {self.result_value}>'

    def calculate_best_run(self):
        """For dual-run events, calculate the best (lowest for time) of two runs."""
        if self.run1_value is not None and self.run2_value is not None:
            # For time-based events, lower is better
            self.best_run = min(self.run1_value, self.run2_value)
            self.result_value = self.best_run
        elif self.run1_value is not None:
            self.best_run = self.run1_value
            self.result_value = self.run1_value
        elif self.run2_value is not None:
            self.best_run = self.run2_value
            self.result_value = self.run2_value
        return self.best_run
