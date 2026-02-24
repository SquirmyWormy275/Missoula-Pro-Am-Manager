"""
Database setup and initialization for the Missoula Pro Am Tournament Manager.
"""
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()


def init_db(app):
    """Initialize the database with the Flask app."""
    db.init_app(app)
    migrate.init_app(app, db)
    # Import all models to register them with SQLAlchemy
    from models import (Tournament, Team, CollegeCompetitor, ProCompetitor,
                        Event, EventResult, Heat, HeatAssignment, Flight)
