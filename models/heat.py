"""
Heat and Flight models for scheduling competition runs.
"""
from database import db
import json


class HeatAssignment(db.Model):
    """Represents a competitor's assignment to a specific heat."""

    __tablename__ = 'heat_assignments'

    id = db.Column(db.Integer, primary_key=True)
    heat_id = db.Column(db.Integer, db.ForeignKey('heats.id'), nullable=False)
    competitor_id = db.Column(db.Integer, nullable=False)
    competitor_type = db.Column(db.String(20), nullable=False)  # 'pro' or 'college'
    stand_number = db.Column(db.Integer, nullable=True)

    def __repr__(self):
        return f'<HeatAssignment heat={self.heat_id} competitor={self.competitor_id}>'


class Heat(db.Model):
    """Represents a heat within an event (group of competitors running together)."""

    __tablename__ = 'heats'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'heat_number', 'run_number', name='uq_event_heat_run'),
        db.Index('ix_heats_event_status', 'event_id', 'status'),
        db.Index('ix_heats_flight_id', 'flight_id'),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), nullable=False)

    # Heat identification
    heat_number = db.Column(db.Integer, nullable=False)
    run_number = db.Column(db.Integer, default=1)  # For dual-run events (1 or 2)

    # Competitors and stand assignments - stored as JSON
    competitors = db.Column(db.Text, default='[]')  # List of competitor IDs
    stand_assignments = db.Column(db.Text, default='{}')  # Dict: competitor_id -> stand_number

    # Status
    status = db.Column(db.String(20), default='pending')  # pending, in_progress, completed
    version_id = db.Column(db.Integer, nullable=False, default=1)

    __mapper_args__ = {
        'version_id_col': version_id,
    }

    # Optional flight assignment (pro only)
    flight_id = db.Column(db.Integer, db.ForeignKey('flights.id'), nullable=True)

    def __repr__(self):
        run_str = f" Run {self.run_number}" if self.run_number > 1 else ""
        return f'<Heat {self.heat_number}{run_str}>'

    def get_competitors(self):
        """Return list of competitor IDs in this heat."""
        return json.loads(self.competitors or '[]')

    def set_competitors(self, competitor_ids):
        """Set the list of competitor IDs."""
        self.competitors = json.dumps(competitor_ids)

    def add_competitor(self, competitor_id):
        """Add a competitor to this heat."""
        comps = self.get_competitors()
        if competitor_id not in comps:
            comps.append(competitor_id)
            self.competitors = json.dumps(comps)

    def remove_competitor(self, competitor_id):
        """Remove a competitor from this heat."""
        comps = self.get_competitors()
        if competitor_id in comps:
            comps.remove(competitor_id)
            self.competitors = json.dumps(comps)

    def get_stand_assignments(self):
        """Return dict of competitor_id -> stand_number."""
        return json.loads(self.stand_assignments or '{}')

    def set_stand_assignment(self, competitor_id, stand_number):
        """Assign a competitor to a specific stand."""
        assignments = self.get_stand_assignments()
        assignments[str(competitor_id)] = stand_number
        self.stand_assignments = json.dumps(assignments)

    def get_stand_for_competitor(self, competitor_id):
        """Get the stand number assigned to a competitor."""
        assignments = self.get_stand_assignments()
        return assignments.get(str(competitor_id))

    @property
    def competitor_count(self):
        """Return number of competitors in this heat."""
        return len(self.get_competitors())


class Flight(db.Model):
    """Represents a flight in pro competition (group of heats from different events)."""

    __tablename__ = 'flights'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)

    # Flight identification
    flight_number = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100), nullable=True)  # Optional custom name

    # Status
    status = db.Column(db.String(20), default='pending')  # pending, in_progress, completed

    # Notes
    notes = db.Column(db.Text, nullable=True)  # For special instructions

    # Relationships
    heats = db.relationship('Heat', backref='flight', lazy='dynamic')

    def __repr__(self):
        return f'<Flight {self.flight_number}>'

    def get_heats_ordered(self):
        """Return heats in this flight, ordered by their sequence."""
        return self.heats.order_by(Heat.id).all()

    def add_heat(self, heat):
        """Add a heat to this flight."""
        heat.flight_id = self.id

    @property
    def heat_count(self):
        """Return number of heats in this flight."""
        return self.heats.count()

    @property
    def event_variety(self):
        """Return count of unique events represented in this flight."""
        heats = self.heats.all()
        event_ids = set(h.event_id for h in heats)
        return len(event_ids)
