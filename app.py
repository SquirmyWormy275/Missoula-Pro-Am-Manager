"""
Flask application entry point for the Missoula Pro Am Tournament Manager.
"""
import os
from flask import Flask
from database import db, init_db
import config
import strings as text

def create_app():
    """Create and configure the Flask application."""
    app = Flask(__name__)

    # Configuration
    app.config['SECRET_KEY'] = config.SECRET_KEY
    app.config['SQLALCHEMY_DATABASE_URI'] = config.SQLALCHEMY_DATABASE_URI
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = config.SQLALCHEMY_TRACK_MODIFICATIONS
    app.config['UPLOAD_FOLDER'] = config.UPLOAD_FOLDER
    app.config['MAX_CONTENT_LENGTH'] = config.MAX_CONTENT_LENGTH

    # Ensure upload folder exists
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # Initialize database
    init_db(app)

    # Inject text constants into all templates
    @app.context_processor
    def inject_strings():
        return {'NAV': text.NAV, 'COMPETITION': text.COMPETITION}

    # Register blueprints
    from routes.main import main_bp
    from routes.registration import registration_bp
    from routes.scheduling import scheduling_bp
    from routes.scoring import scoring_bp
    from routes.reporting import reporting_bp
    from routes.proam_relay import bp as proam_relay_bp
    from routes.partnered_axe import bp as partnered_axe_bp
    from routes.validation import bp as validation_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(registration_bp, url_prefix='/registration')
    app.register_blueprint(scheduling_bp, url_prefix='/scheduling')
    app.register_blueprint(scoring_bp, url_prefix='/scoring')
    app.register_blueprint(reporting_bp, url_prefix='/reporting')
    app.register_blueprint(proam_relay_bp)
    app.register_blueprint(partnered_axe_bp)
    app.register_blueprint(validation_bp)

    return app


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app = create_app()
    app.run(host='0.0.0.0', port=port, debug=False)
