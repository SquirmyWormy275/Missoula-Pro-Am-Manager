"""
Database setup and initialization for the Missoula Pro Am Tournament Manager.
"""
import logging
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()

logger = logging.getLogger(__name__)


def init_db(app):
    """Initialize the database with the Flask app."""
    db.init_app(app)
    migrate.init_app(app, db)
    # Import all models to register them with SQLAlchemy
    from models import (Tournament, Team, CollegeCompetitor, ProCompetitor,
                        Event, EventResult, Heat, HeatAssignment, Flight, User, AuditLog,
                        SchoolCaptain, WoodConfig, ProEventRank, PayoutTemplate)

    # Auto-run migrations on startup so the DB schema is always current.
    # Skip when invoked via `flask db` CLI to avoid double-upgrade conflicts.
    # Skip during testing — tests manage their own migrations.
    import sys
    is_flask_db_cli = any(arg == 'db' for arg in sys.argv)
    is_testing = 'pytest' in sys.modules or app.config.get('TESTING')
    if not is_flask_db_cli and not is_testing:
        with app.app_context():
            try:
                from flask_migrate import upgrade
                from alembic.util.exc import CommandError
                upgrade()
                logger.info("Database migrations applied successfully.")
            except CommandError as e:
                # Alembic-specific errors: bad revision chain, missing migration
                # file, etc.  These are real problems that need attention.
                logger.error(
                    "Migration chain error — database may be out of sync: %s", e
                )
            except Exception as e:
                # OperationalError (locked DB, corrupt file), ProgrammingError
                # (bad SQL in a migration), etc.
                err_str = str(e).lower()
                if 'no such table' in err_str or 'does not exist' in err_str:
                    # First run — DB file exists but has no tables yet. Normal.
                    logger.info("First-run migration (fresh database): %s", e)
                else:
                    logger.error(
                        "Auto-migration failed — schema may be stale: %s", e
                    )
