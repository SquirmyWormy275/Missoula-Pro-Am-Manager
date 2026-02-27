"""Audit log model for critical state changes."""
from datetime import datetime
from database import db


class AuditLog(db.Model):
    """Immutable audit entries for security-sensitive actions."""

    __tablename__ = 'audit_logs'
    __table_args__ = (
        db.Index('ix_audit_logs_created_at', 'created_at'),
        db.Index('ix_audit_logs_actor', 'actor_user_id'),
        db.Index('ix_audit_logs_action', 'action'),
    )

    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(80), nullable=False)
    entity_type = db.Column(db.String(80), nullable=False)
    entity_id = db.Column(db.Integer, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    details_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

