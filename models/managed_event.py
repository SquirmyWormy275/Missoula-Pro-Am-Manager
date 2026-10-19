"""Additive host mappings; module attempts are not event summary rows."""

from database import db


class ManagedEventLink(db.Model):
    __tablename__ = 'managed_event_links'
    __table_args__ = (
        db.CheckConstraint("mode = 'managed'", name='ck_managed_event_mode'),
        db.CheckConstraint('accepted_revision >= 0', name='ck_managed_accepted_revision'),
    )
    event_id = db.Column(db.Integer, db.ForeignKey('events.id'), primary_key=True, nullable=False)
    module_event_id = db.Column(db.String(224), nullable=False, unique=True)
    show_id = db.Column(db.String(224), nullable=False)
    mode = db.Column(db.String(16), nullable=False, default='managed', server_default='managed')
    mapping_json = db.Column(db.Text, nullable=False)
    accepted_revision = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    summary_revision = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    state_json = db.Column(db.Text, nullable=True)
    event = db.relationship('Event', backref=db.backref('managed_operation', uselist=False))


class ManagedCommand(db.Model):
    __tablename__ = 'managed_commands'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'actor_id', 'request_id', name='uq_managed_command_identity'),
        db.CheckConstraint("status IN ('pending','accepted','conflicting')", name='ck_managed_command_status'),
    )
    id = db.Column(db.Integer, primary_key=True, nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey('managed_event_links.event_id'), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    request_id = db.Column(db.String(224), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    payload_digest = db.Column(db.CHAR(64), nullable=False)
    status = db.Column(db.String(16), nullable=False, default='pending', server_default='pending')
    response_json = db.Column(db.Text, nullable=True)


class ManagedFieldLink(db.Model):
    __tablename__ = 'managed_field_links'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'module_heat_id', name='uq_managed_field_identity'),
    )
    id = db.Column(db.Integer, primary_key=True, nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey('managed_event_links.event_id'), nullable=False)
    module_heat_id = db.Column(db.String(224), nullable=False)
    heat_id = db.Column(db.Integer, db.ForeignKey('heats.id'), nullable=False, unique=True)
    field_digest = db.Column(db.CHAR(64), nullable=False)
    issue_id = db.Column(db.CHAR(64), nullable=True)


class ManagedAttemptRevision(db.Model):
    __tablename__ = 'managed_attempt_revisions'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'source_result_id', 'source_revision', name='uq_managed_attempt_revision'),
        db.CheckConstraint('source_revision >= 1', name='ck_managed_attempt_revision'),
    )
    id = db.Column(db.Integer, primary_key=True, nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey('managed_event_links.event_id'), nullable=False)
    source_result_id = db.Column(db.String(512), nullable=False)
    source_revision = db.Column(db.Integer, nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    payload_digest = db.Column(db.CHAR(64), nullable=False)


class ManagedAcceptance(db.Model):
    __tablename__ = 'managed_acceptances'
    __table_args__ = (
        db.UniqueConstraint('event_id', 'module_revision', name='uq_managed_acceptance_revision'),
    )
    id = db.Column(db.Integer, primary_key=True, nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey('managed_event_links.event_id'), nullable=False)
    module_revision = db.Column(db.Integer, nullable=False)
    payload_digest = db.Column(db.CHAR(64), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)


class ManagedCareerDelivery(db.Model):
    __tablename__ = 'managed_career_deliveries'
    __table_args__ = (
        db.CheckConstraint("status IN ('pending','accepted','conflicting')", name='ck_managed_career_delivery_status'),
    )
    delivery_id = db.Column(db.CHAR(64), primary_key=True, nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey('managed_event_links.event_id'), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    payload_digest = db.Column(db.CHAR(64), nullable=False)
    status = db.Column(db.String(16), nullable=False, default='pending', server_default='pending')
    receipt_digest = db.Column(db.CHAR(64), nullable=True)
    receipt_json = db.Column(db.Text, nullable=True)
    last_error_code = db.Column(db.String(80), nullable=True)
