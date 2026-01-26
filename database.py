"""
Database setup and initialization for the Missoula Pro Am Tournament Manager.
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def init_db(app):
    """Initialize the database with the Flask app."""
    db.init_app(app)
    with app.app_context():
        # Import all models to register them with SQLAlchemy
        from models import (Tournament, Team, CollegeCompetitor, ProCompetitor,
                            Event, EventResult, Heat, HeatAssignment, Flight)
        db.create_all()
