"""
Heat and Flight models for scheduling competition runs.
"""
from database import db
from datetime import datetime, timezone
import json

HEAT_LOCK_TTL_SECONDS = 300  # 5-minute lock expiry


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

    # Edit lock — prevents two judges on different devices from simultaneously entering the same heat.
    # Acquired when the entry form is opened; auto-expires after HEAT_LOCK_TTL_SECONDS.
    locked_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)

    # Optional flight assignment (pro only)
    flight_id = db.Column(db.Integer, db.ForeignKey('flights.id'), nullable=True)
    flight_position = db.Column(db.Integer, nullable=True)  # 1-based order within a flight

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

    def sync_assignments(self, competitor_type: str) -> None:
        """Rebuild HeatAssignment rows to match the authoritative competitors JSON.

        Must be called after self.id is assigned (i.e., after db.session.flush()).
        competitor_type should be 'pro' or 'college' (matches event.event_type).
        """
        HeatAssignment.query.filter_by(heat_id=self.id).delete()
        assignments = self.get_stand_assignments()
        for comp_id in self.get_competitors():
            db.session.add(HeatAssignment(
                heat_id=self.id,
                competitor_id=comp_id,
                competitor_type=competitor_type,
                stand_number=assignments.get(str(comp_id)),
            ))

    @property
    def competitor_count(self):
        """Return number of competitors in this heat."""
        return len(self.get_competitors())

    def is_locked(self) -> bool:
        """True if the heat is currently locked by another judge (non-expired)."""
        if not self.locked_by_user_id or not self.locked_at:
            return False
        now = datetime.now(timezone.utc)
        locked_at = self.locked_at
        if locked_at.tzinfo is None:
            locked_at = locked_at.replace(tzinfo=timezone.utc)
        return (now - locked_at).total_seconds() < HEAT_LOCK_TTL_SECONDS

    def acquire_lock(self, user_id: int) -> bool:
        """Attempt to acquire the edit lock. Returns True if successful."""
        if self.is_locked() and self.locked_by_user_id != user_id:
            return False
        self.locked_by_user_id = user_id
        self.locked_at = datetime.now(timezone.utc)
        return True

    def release_lock(self, user_id: int) -> None:
        """Release the lock if held by user_id."""
        if self.locked_by_user_id == user_id:
            self.locked_by_user_id = None
            self.locked_at = None


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
        return self.heats.order_by(
            db.case((Heat.flight_position.is_(None), 1), else_=0),
            Heat.flight_position,
            Heat.id,
        ).all()

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
