"""Durable idempotency receipts for heat score submissions."""

from database import db
from services.time_utils import utc_now_naive


class ScoreSubmissionReceipt(db.Model):
    """Binds one accepted request to historical tournament scoring state."""

    __tablename__ = 'score_submission_receipts'
    __table_args__ = (
        db.Index(
            'ix_score_submission_receipts_binding',
            'tournament_id',
            'heat_id',
            'issuing_user_id',
        ),
    )

    request_id = db.Column(db.String(36), primary_key=True)
    tournament_id = db.Column(
        db.Integer,
        db.ForeignKey('tournaments.id', ondelete='CASCADE'),
        nullable=False,
    )
    # Heat rows can be regenerated after a score is undone. Keep the original
    # integer as historical identity so deleting that mutable row cannot make
    # an old offline request ID reusable.
    heat_id = db.Column(db.Integer, nullable=False)
    issuing_user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    )
    canonical_payload_sha256 = db.Column(db.String(64), nullable=False)
    accepted_outcome_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now_naive)

    def __repr__(self):
        return (
            f'<ScoreSubmissionReceipt {self.request_id} '
            f't={self.tournament_id} h={self.heat_id} u={self.issuing_user_id}>'
        )
