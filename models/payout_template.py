"""
PayoutTemplate model — reusable payout structures for pro events.
"""
import json
from datetime import datetime, timezone

import sqlalchemy as sa

from database import db


class PayoutTemplate(db.Model):
    """A named payout structure that can be applied to multiple events."""

    __tablename__ = 'payout_templates'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    # JSON dict: {"1": 500.0, "2": 300.0, ...}
    payouts = db.Column(
        db.Text, nullable=False, default='{}', server_default=sa.text("'{}'")
    )
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f'<PayoutTemplate {self.name!r}>'

    def get_payouts(self) -> dict:
        return json.loads(self.payouts or '{}')

    def set_payouts(self, payout_dict: dict) -> None:
        self.payouts = json.dumps(payout_dict)

    def total_purse(self) -> float:
        return sum(float(v) for v in self.get_payouts().values())
