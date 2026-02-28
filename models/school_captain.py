"""
SchoolCaptain model — one PIN-protected profile per school per tournament.
Grants the captain access to all teams from their school.
"""
from database import db
from werkzeug.security import check_password_hash, generate_password_hash


class SchoolCaptain(db.Model):
    """One captain account per school per tournament; covers all school teams."""

    __tablename__ = 'school_captains'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    school_name = db.Column(db.String(200), nullable=False)  # Matches Team.school_name exactly
    pin_hash = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    __table_args__ = (
        db.UniqueConstraint('tournament_id', 'school_name', name='unique_school_captain_per_tournament'),
    )

    def __repr__(self):
        return f'<SchoolCaptain {self.school_name} @ tournament {self.tournament_id}>'

    @property
    def has_pin(self) -> bool:
        return bool(self.pin_hash)

    def set_pin(self, pin: str):
        self.pin_hash = generate_password_hash(pin)

    def check_pin(self, pin: str) -> bool:
        if not self.pin_hash:
            return False
        return check_password_hash(self.pin_hash, pin)
