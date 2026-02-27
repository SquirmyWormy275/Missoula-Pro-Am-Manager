"""
Flask application entry point for the Missoula Pro Am Tournament Manager.
"""
import os
from flask import Flask, Response, request, abort
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine
from database import db, init_db
import config
import strings as text
from services.background_jobs import configure as configure_jobs
from services.logging_setup import configure_error_monitoring, configure_logging

HAS_FLASK_LOGIN = True
try:
    from flask_login import LoginManager, current_user
except ModuleNotFoundError:
    HAS_FLASK_LOGIN = False

    class LoginManager:  # type: ignore
        def __init__(self):
            self.login_view = None
            self.login_message = ''
            self.login_message_category = 'warning'

        def init_app(self, app):
            return None

        def unauthorized(self):
            abort(401)

        def user_loader(self, func):
            return func

    class _AnonymousCurrentUser:
        is_authenticated = False

    current_user = _AnonymousCurrentUser()

csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to continue.'
login_manager.login_message_category = 'warning'

MANAGEMENT_BLUEPRINTS = {'main', 'registration', 'scheduling', 'scoring', 'reporting', 'proam_relay', 'partnered_axe', 'validation', 'import_pro'}
BLUEPRINT_PERMISSIONS = {
    'main': 'is_judge',
    'registration': 'can_register',
    'scheduling': 'can_schedule',
    'scoring': 'can_score',
    'reporting': 'can_report',
    'proam_relay': 'can_score',
    'partnered_axe': 'can_score',
    'validation': 'can_report',
    'import_pro': 'can_register',
    'auth': 'can_manage_users',
}

PUBLIC_MAIN_ENDPOINTS = {
    'main.index',
    'main.set_language',
}


def _can_access_arapaho_mode(endpoint: str) -> bool:
    """Only judge/admin context can use Arapaho mode."""
    if endpoint.startswith('portal.') or endpoint == 'main.index':
        return False
    if not HAS_FLASK_LOGIN:
        return False
    if not getattr(current_user, 'is_authenticated', False):
        return False
    return bool(getattr(current_user, 'is_judge', False) or getattr(current_user, 'is_admin', False))

def create_app():
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.config.from_object(config.get_config())
    config.validate_runtime(app.config)
    configure_logging(bool(app.config.get('STRUCTURED_LOGGING', True)))
    configure_error_monitoring(app.config.get('SENTRY_DSN', ''))
    configure_jobs(int(app.config.get('JOB_MAX_WORKERS', 2)))

    # Ensure upload folder exists
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # Initialize database
    init_db(app)

    # Initialize CSRF protection
    csrf.init_app(app)
    if HAS_FLASK_LOGIN:
        login_manager.init_app(app)

        from models.user import User

        @login_manager.user_loader
        def load_user(user_id: str):
            if not user_id:
                return None
            return User.query.get(int(user_id))

    # Inject text constants into all templates
    @app.context_processor
    def inject_strings():
        endpoint = request.endpoint or ''
        arapaho_allowed = _can_access_arapaho_mode(endpoint)
        return {
            'NAV': text.section('NAV'),
            'COMPETITION': text.section('COMPETITION'),
            'LANGUAGES': text.SUPPORTED_LANGUAGES if arapaho_allowed else {'en': text.SUPPORTED_LANGUAGES['en']},
            'CURRENT_LANG': text.get_language(),
            'ARAPAHO_ALLOWED': arapaho_allowed,
            'ui': text.ui,
        }

    # Register blueprints
    from routes.main import main_bp
    from routes.registration import registration_bp
    from routes.scheduling import scheduling_bp
    from routes.scoring import scoring_bp
    from routes.reporting import reporting_bp
    from routes.proam_relay import bp as proam_relay_bp
    from routes.partnered_axe import bp as partnered_axe_bp
    from routes.validation import bp as validation_bp
    from routes.import_routes import import_pro_bp
    if HAS_FLASK_LOGIN:
        from routes.auth import auth_bp
        from routes.portal import portal_bp
        from routes.api import api_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(registration_bp, url_prefix='/registration')
    app.register_blueprint(scheduling_bp, url_prefix='/scheduling')
    app.register_blueprint(scoring_bp, url_prefix='/scoring')
    app.register_blueprint(reporting_bp, url_prefix='/reporting')
    app.register_blueprint(proam_relay_bp)
    app.register_blueprint(partnered_axe_bp)
    app.register_blueprint(validation_bp)
    app.register_blueprint(import_pro_bp, url_prefix='/import')
    if HAS_FLASK_LOGIN:
        app.register_blueprint(auth_bp, url_prefix='/auth')
        app.register_blueprint(portal_bp, url_prefix='/portal')
        app.register_blueprint(api_bp, url_prefix='/api')

    @sa_event.listens_for(Engine, 'connect')
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
            cursor = dbapi_connection.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.close()

    @app.before_request
    def require_judge_for_management_routes():
        """Protect management routes while keeping login and portals available."""
        endpoint = request.endpoint or ''
        if endpoint.startswith('static'):
            return None
        if endpoint in PUBLIC_MAIN_ENDPOINTS:
            return None
        if HAS_FLASK_LOGIN:
            if endpoint.startswith('auth.'):
                return None
            if endpoint.startswith('portal.'):
                return None
            if endpoint.startswith('api.public_'):
                return None

        blueprint_name = endpoint.split('.', 1)[0]
        if blueprint_name not in MANAGEMENT_BLUEPRINTS:
            return None

        if not HAS_FLASK_LOGIN:
            return None

        if not current_user.is_authenticated:
            return login_manager.unauthorized()

        permission_attr = BLUEPRINT_PERMISSIONS.get(blueprint_name, 'is_judge')
        if not getattr(current_user, permission_attr, False):
            abort(403)
        return None

    @app.before_request
    def enforce_language_access():
        """Force English outside Judge/Admin contexts."""
        endpoint = request.endpoint or ''
        if text.get_language() == 'arp' and not _can_access_arapaho_mode(endpoint):
            text.set_language('en')
        return None

    @app.after_request
    def apply_language_translation(response: Response):
        """Apply full-page translation for HTML responses."""
        if text.get_language() != 'arp':
            return response
        content_type = response.content_type or ''
        if response.direct_passthrough or 'text/html' not in content_type:
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response

        body = response.get_data(as_text=True)
        translated = text.translate_html(body)
        if translated != body:
            response.set_data(translated)
        return response

    return app


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app = create_app()
    app.run(host='0.0.0.0', port=port, debug=False)
