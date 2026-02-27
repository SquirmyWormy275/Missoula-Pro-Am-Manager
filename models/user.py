"""
User model for role-based authentication.
"""
from datetime import datetime
try:
    from flask_login import UserMixin
except ModuleNotFoundError:
    class UserMixin:  # type: ignore
        @property
        def is_authenticated(self):
            return True

        @property
        def is_anonymous(self):
            return False

        def get_id(self):
            return str(getattr(self, 'id', ''))
from werkzeug.security import check_password_hash, generate_password_hash
from database import db


class User(UserMixin, db.Model):
    """Application user with role-based access control."""

    __tablename__ = 'users'

    ROLE_ADMIN = 'admin'
    ROLE_JUDGE = 'judge'
    ROLE_SCORER = 'scorer'
    ROLE_REGISTRAR = 'registrar'
    ROLE_COMPETITOR = 'competitor'
    ROLE_SPECTATOR = 'spectator'
    ROLE_VIEWER = 'viewer'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_SPECTATOR)

    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=True)
    competitor_type = db.Column(db.String(20), nullable=True)  # 'college' or 'pro'
    competitor_id = db.Column(db.Integer, nullable=True)

    display_name = db.Column(db.String(200), nullable=True)
    is_active_user = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f'<User {self.username} ({self.role})>'

    @property
    def is_active(self):
        return bool(self.is_active_user)

    @property
    def is_judge(self):
        return self.role in {self.ROLE_ADMIN, self.ROLE_JUDGE}

    @property
    def is_admin(self):
        return self.role == self.ROLE_ADMIN

    @property
    def is_competitor(self):
        return self.role == self.ROLE_COMPETITOR

    @property
    def is_spectator(self):
        return self.role in {self.ROLE_SPECTATOR, self.ROLE_VIEWER}

    @property
    def can_manage_users(self):
        return self.is_admin

    @property
    def can_register(self):
        return self.role in {self.ROLE_ADMIN, self.ROLE_JUDGE, self.ROLE_REGISTRAR}

    @property
    def can_schedule(self):
        return self.role in {self.ROLE_ADMIN, self.ROLE_JUDGE, self.ROLE_SCORER}

    @property
    def can_score(self):
        return self.role in {self.ROLE_ADMIN, self.ROLE_JUDGE, self.ROLE_SCORER}

    @property
    def can_report(self):
        return self.role in {
            self.ROLE_ADMIN,
            self.ROLE_JUDGE,
            self.ROLE_SCORER,
            self.ROLE_REGISTRAR,
            self.ROLE_VIEWER,
            self.ROLE_SPECTATOR,
        }

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)
