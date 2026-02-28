"""
Competitor models for both college and professional competitors.
"""
from database import db
import json
from werkzeug.security import check_password_hash, generate_password_hash


class CollegeCompetitor(db.Model):
    """Represents a college competitor."""

    __tablename__ = 'college_competitors'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)

    # Personal info
    name = db.Column(db.String(200), nullable=False)
    gender = db.Column(db.String(1), nullable=False)  # 'M' or 'F'

    # Scoring
    individual_points = db.Column(db.Integer, default=0)

    # Event tracking - stored as JSON
    events_entered = db.Column(db.Text, default='[]')  # List of event IDs
    partners = db.Column(db.Text, default='{}')  # Dict: event_id -> partner_name
    gear_sharing = db.Column(db.Text, default='{}')  # Dict: event_id -> partner sharing gear
    portal_pin_hash = db.Column(db.String(255), nullable=True)

    # Headshot and SMS (#14, #2)
    headshot_filename = db.Column(db.Text, nullable=True)
    phone_opted_in = db.Column(db.Boolean, default=False)

    # Status
    status = db.Column(db.String(20), default='active')  # active, scratched

    _PRO_AM_LOTTERY_META_KEY = '__pro_am_lottery_opt_in__'

    def __repr__(self):
        return f'<CollegeCompetitor {self.name} ({self.team.team_code if self.team else "no team"})>'

    def get_events_entered(self):
        """Return list of event IDs this competitor is entered in."""
        return json.loads(self.events_entered or '[]')

    def set_events_entered(self, events):
        """Set the list of event IDs."""
        self.events_entered = json.dumps(events)

    def get_partners(self):
        """Return dict of event_id -> partner_name."""
        return json.loads(self.partners or '{}')

    def set_partner(self, event_id, partner_name):
        """Set partner for a specific event."""
        partners = self.get_partners()
        partners[str(event_id)] = partner_name
        self.partners = json.dumps(partners)

    @property
    def pro_am_lottery_opt_in(self) -> bool:
        """Return whether this college competitor opted into the Pro-Am relay lottery."""
        partners = self.get_partners()
        value = str(partners.get(self._PRO_AM_LOTTERY_META_KEY, '')).strip().lower()
        return value in {'true', '1', 'yes', 'y', 'x'}

    @pro_am_lottery_opt_in.setter
    def pro_am_lottery_opt_in(self, opted_in: bool):
        """Persist Pro-Am relay lottery preference in the partners metadata payload."""
        partners = self.get_partners()
        if opted_in:
            partners[self._PRO_AM_LOTTERY_META_KEY] = 'true'
        else:
            partners.pop(self._PRO_AM_LOTTERY_META_KEY, None)
        self.partners = json.dumps(partners)

    def get_gear_sharing(self):
        """Return dict of event_id -> partner sharing gear."""
        return json.loads(self.gear_sharing or '{}')

    def set_gear_sharing(self, event_id, partner_name):
        """Set gear sharing partner for a specific event."""
        sharing = self.get_gear_sharing()
        sharing[str(event_id)] = partner_name
        self.gear_sharing = json.dumps(sharing)

    def add_points(self, points):
        """Add points to individual total and update team total."""
        self.individual_points += points
        if self.team:
            self.team.recalculate_points()

    @property
    def closed_event_count(self):
        """Return count of CLOSED events entered (max 6 allowed)."""
        import config
        closed_names = {e['name'] for e in config.COLLEGE_CLOSED_EVENTS}
        return sum(1 for e in self.get_events_entered() if e in closed_names)

    @property
    def has_portal_pin(self) -> bool:
        return bool(self.portal_pin_hash)

    def set_portal_pin(self, pin: str):
        self.portal_pin_hash = generate_password_hash(pin)

    def check_portal_pin(self, pin: str) -> bool:
        if not self.portal_pin_hash:
            return False
        return check_password_hash(self.portal_pin_hash, pin)


class ProCompetitor(db.Model):
    """Represents a professional competitor."""

    __tablename__ = 'pro_competitors'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)

    # Personal info
    name = db.Column(db.String(200), nullable=False)
    gender = db.Column(db.String(1), nullable=False)  # 'M' or 'F'

    # Contact info
    address = db.Column(db.Text, nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(200), nullable=True)
    shirt_size = db.Column(db.String(10), nullable=True)

    # Membership and lottery
    is_ala_member = db.Column(db.Boolean, default=False)  # American Lumberjack Association
    pro_am_lottery_opt_in = db.Column(db.Boolean, default=False)

    # Springboard specific
    is_left_handed_springboard = db.Column(db.Boolean, default=False)

    # Event and fee tracking - stored as JSON
    events_entered = db.Column(db.Text, default='[]')  # List of event IDs
    entry_fees = db.Column(db.Text, default='{}')  # Dict: event_id -> fee amount
    fees_paid = db.Column(db.Text, default='{}')  # Dict: event_id -> True/False
    gear_sharing = db.Column(db.Text, default='{}')  # Dict: event_id -> partner name
    partners = db.Column(db.Text, default='{}')  # Dict: event_id -> partner_name
    portal_pin_hash = db.Column(db.String(255), nullable=True)

    # Earnings
    total_earnings = db.Column(db.Float, default=0.0)
    payout_settled = db.Column(db.Boolean, default=False)  # #21 payout settlement checklist

    # Headshot and SMS (#14, #2)
    headshot_filename = db.Column(db.Text, nullable=True)
    phone_opted_in = db.Column(db.Boolean, default=False)

    # Status
    status = db.Column(db.String(20), default='active')  # active, scratched

    # Import tracking (populated by Google Forms xlsx importer)
    submission_timestamp = db.Column(db.DateTime, nullable=True)
    gear_sharing_details = db.Column(db.Text, nullable=True)
    waiver_accepted = db.Column(db.Boolean, default=False)
    waiver_signature = db.Column(db.String(200), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    total_fees = db.Column(db.Integer, default=0)
    import_timestamp = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f'<ProCompetitor {self.name}>'

    def get_events_entered(self):
        """Return list of event IDs this competitor is entered in."""
        return json.loads(self.events_entered or '[]')

    def set_events_entered(self, events):
        """Set the list of event IDs."""
        self.events_entered = json.dumps(events)

    def get_entry_fees(self):
        """Return dict of event_id -> fee amount."""
        return json.loads(self.entry_fees or '{}')

    def set_entry_fee(self, event_id, amount):
        """Set entry fee for a specific event."""
        fees = self.get_entry_fees()
        fees[str(event_id)] = amount
        self.entry_fees = json.dumps(fees)

    def get_fees_paid(self):
        """Return dict of event_id -> paid status."""
        return json.loads(self.fees_paid or '{}')

    def set_fee_paid(self, event_id, paid=True):
        """Set fee paid status for a specific event."""
        paid_status = self.get_fees_paid()
        paid_status[str(event_id)] = paid
        self.fees_paid = json.dumps(paid_status)

    def get_gear_sharing(self):
        """Return dict of event_id -> partner sharing gear."""
        return json.loads(self.gear_sharing or '{}')

    def set_gear_sharing(self, event_id, partner_name):
        """Set gear sharing partner for a specific event."""
        sharing = self.get_gear_sharing()
        sharing[str(event_id)] = partner_name
        self.gear_sharing = json.dumps(sharing)

    def get_partners(self):
        """Return dict of event_id -> partner_name."""
        return json.loads(self.partners or '{}')

    def set_partner(self, event_id, partner_name):
        """Set partner for a specific event."""
        partners = self.get_partners()
        partners[str(event_id)] = partner_name
        self.partners = json.dumps(partners)

    def add_earnings(self, amount):
        """Add earnings to total."""
        self.total_earnings += amount

    @property
    def total_fees_owed(self):
        """Calculate total entry fees owed."""
        return sum(self.get_entry_fees().values())

    @property
    def total_fees_paid(self):
        """Calculate total fees that have been paid."""
        fees = self.get_entry_fees()
        paid = self.get_fees_paid()
        return sum(fees.get(k, 0) for k, v in paid.items() if v)

    @property
    def fees_balance(self):
        """Calculate remaining balance owed."""
        return self.total_fees_owed - self.total_fees_paid

    @property
    def has_portal_pin(self) -> bool:
        return bool(self.portal_pin_hash)

    def set_portal_pin(self, pin: str):
        self.portal_pin_hash = generate_password_hash(pin)

    def check_portal_pin(self, pin: str) -> bool:
        if not self.portal_pin_hash:
            return False
        return check_password_hash(self.portal_pin_hash, pin)
