"""PrintEmailLog model for Print Hub email-delivery audit trail.

One row per send attempt. Populated by services/email_delivery.py.
"""

import json
from datetime import datetime

from database import db

EMAIL_LOG_STATUSES = ("queued", "sent", "failed")


class PrintEmailLog(db.Model):
    """Records each Print Hub email send attempt (queued / sent / failed)."""

    __tablename__ = "print_email_logs"

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(
        db.Integer,
        db.ForeignKey("tournaments.id", ondelete="CASCADE"),
        nullable=False,
    )
    doc_key = db.Column(db.String(64), nullable=False)
    entity_id = db.Column(db.Integer, nullable=True)

    # JSON list of email addresses (lowercased, validated before insert)
    recipients_json = db.Column(
        db.Text, nullable=False, default="[]", server_default="[]"
    )
    subject = db.Column(db.String(300), nullable=False)

    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    sent_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    status = db.Column(
        db.String(16), nullable=False, default="queued", server_default="queued"
    )
    error = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.Index("ix_print_email_logs_tournament_sent", "tournament_id", "sent_at"),
        db.CheckConstraint(
            "status IN ('queued', 'sent', 'failed')",
            name="ck_print_email_logs_status_valid",
        ),
    )

    def get_recipients(self) -> list:
        """Return the recipient list. Returns [] on corrupt JSON."""
        try:
            value = json.loads(self.recipients_json or "[]")
            return value if isinstance(value, list) else []
        except (ValueError, TypeError):
            return []

    def set_recipients(self, recipients: list):
        """Store the recipient list as JSON."""
        self.recipients_json = json.dumps(list(recipients or []))

    def __repr__(self):
        return f"<PrintEmailLog {self.doc_key} t={self.tournament_id} {self.status}>"
