"""Durable record of async background job execution."""
from datetime import datetime

from database import db


class BackgroundJob(db.Model):
    """Persisted job status for operator visibility across process restarts."""

    __tablename__ = 'background_jobs'
    __table_args__ = (
        db.Index('ix_background_jobs_status', 'status'),
        db.Index('ix_background_jobs_submitted_at', 'submitted_at'),
        db.Index('ix_background_jobs_tournament_id', 'tournament_id'),
    )

    id = db.Column(db.String(32), primary_key=True)
    label = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    tournament_id = db.Column(db.Integer, nullable=True)
    submitted_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    result_json = db.Column(db.Text, nullable=True)
    error_text = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)
