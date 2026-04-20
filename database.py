"""
Database setup and initialization for the Missoula Pro Am Tournament Manager.
"""
import logging
import os

from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()

logger = logging.getLogger(__name__)


def init_db(app):
    """Initialize the database with the Flask app."""
    db.init_app(app)
    migrate.init_app(app, db)
    migration_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'migrations')
    # Import all models to register them with SQLAlchemy
    from models import (
        AuditLog,
        BackgroundJob,
        CollegeCompetitor,
        Event,
        EventResult,
        Flight,
        Heat,
        HeatAssignment,
        PayoutTemplate,
        ProCompetitor,
        ProEventRank,
        SchoolCaptain,
        Team,
        Tournament,
        User,
        WoodConfig,
    )

    # Migrations are handled by Railway's releaseCommand (`flask db upgrade`
    # in railway.toml) — NOT at app startup.  Running migrations here caused a
    # race condition: Railway's releaseCommand and the app boot competed for the
    # migration lock, sometimes leaving the schema in an inconsistent state.
    #
    # For local dev, run `flask db upgrade` manually before starting the app.
