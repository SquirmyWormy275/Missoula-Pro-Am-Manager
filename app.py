"""
Flask application entry point for the Missoula Pro Am Tournament Manager.
"""
import os
import time
from flask import Flask, Response, jsonify, request, abort, render_template, send_from_directory, session
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine
from database import db, init_db
import config
import strings as text
from services.background_jobs import configure as configure_jobs
from services.logging_setup import configure_error_monitoring, configure_logging, request_id_middleware

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

MANAGEMENT_BLUEPRINTS = {'main', 'registration', 'scheduling', 'scoring', 'reporting', 'proam_relay', 'partnered_axe', 'validation', 'import_pro', 'woodboss', 'demo', 'strathmark'}
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
    'woodboss': 'is_judge',
    'demo': 'is_judge',
    'strathmark': 'is_judge',
    'auth': 'can_manage_users',
}

PUBLIC_MAIN_ENDPOINTS = {
    'main.index',
    'main.set_language',
    'main.health',
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
    request_id_middleware(app)
    configure_jobs(int(app.config.get('JOB_MAX_WORKERS', 2)))

    # Session cookie hardening
    app.config.setdefault('SESSION_COOKIE_HTTPONLY', True)
    app.config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')
    if app.config.get('ENV_NAME') == 'production':
        app.config.setdefault('SESSION_COOKIE_SECURE', True)

    # Ensure upload folder exists
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # Initialize database
    init_db(app)

    # Initialize CSRF protection
    csrf.init_app(app)
    if HAS_FLASK_LOGIN:
        login_manager.session_protection = 'basic'  # type: ignore[assignment]
        login_manager.init_app(app)

        from models.user import User

        @login_manager.user_loader
        def load_user(user_id: str):
            if not user_id:
                return None
            try:
                return db.session.get(User, int(user_id))
            except (ValueError, TypeError):
                return None

    # Inject text constants into all templates
    @app.context_processor
    def inject_strings():
        endpoint = request.endpoint or ''
        arapaho_allowed = _can_access_arapaho_mode(endpoint)
        lock_until = session.get('arapaho_language_lock_until')
        remaining = 0
        if isinstance(lock_until, (int, float)):
            remaining = max(0, int(lock_until - time.time()))

        # Unscored heat count for sidebar badge — only when inside a tournament route
        unscored_heats = 0
        try:
            tid = request.view_args.get('tournament_id') if request.view_args else None
            if tid:
                from models import Heat
                from models import Event as _Event
                unscored_heats = Heat.query.join(_Event, Heat.event_id == _Event.id) \
                    .filter(_Event.tournament_id == tid, Heat.status == 'pending').count()
        except Exception:
            pass

        # Public languages always available; restricted languages (Arapaho) only for judge/admin.
        available_languages = dict(text.PUBLIC_LANGUAGES)
        if arapaho_allowed:
            available_languages.update(text.RESTRICTED_LANGUAGES)

        return {
            'NAV': text.section('NAV'),
            'COMPETITION': text.section('COMPETITION'),
            'LANGUAGES': available_languages,
            'CURRENT_LANG': text.get_language(),
            'ARAPAHO_ALLOWED': arapaho_allowed,
            'ARAPAHO_LOCK_REMAINING': remaining,
            'ui': text.ui,
            'unscored_heats': unscored_heats,
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
    from routes.woodboss import woodboss_bp, woodboss_public_bp
    from routes.strathmark import strathmark_bp
    from routes.demo_data import demo_bp
    if HAS_FLASK_LOGIN:
        from routes.auth import auth_bp
        from routes.portal import portal_bp
        from routes.api import api_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(registration_bp, url_prefix='/registration')
    app.register_blueprint(scheduling_bp, url_prefix='/scheduling')
    app.register_blueprint(scoring_bp, url_prefix='/scoring')
    # Exempt the offline replay endpoint from CSRF — it uses a one-time replay token instead.
    csrf.exempt('scoring.replay_offline_score')
    app.register_blueprint(reporting_bp, url_prefix='/reporting')
    app.register_blueprint(proam_relay_bp)
    app.register_blueprint(partnered_axe_bp)
    app.register_blueprint(validation_bp)
    app.register_blueprint(import_pro_bp, url_prefix='/import')
    app.register_blueprint(woodboss_bp, url_prefix='/woodboss')
    app.register_blueprint(woodboss_public_bp, url_prefix='/woodboss')
    app.register_blueprint(strathmark_bp, url_prefix='/strathmark')
    app.register_blueprint(demo_bp, url_prefix='/demo')
    if HAS_FLASK_LOGIN:
        app.register_blueprint(auth_bp, url_prefix='/auth')
        app.register_blueprint(portal_bp, url_prefix='/portal')
        app.register_blueprint(api_bp, url_prefix='/api')
        # Also register api_bp at /api/v1/ for forwards-compatible clients (#19)
        app.register_blueprint(api_bp, url_prefix='/api/v1', name='api_v1')
        # Attach rate limiters to the app (no-op if flask-limiter not installed)
        from routes.api import _init_limiter, _init_write_limiter
        _init_limiter(app)
        _init_write_limiter(app)

    @sa_event.listens_for(Engine, 'connect')
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
            cursor = dbapi_connection.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.close()

    # Service worker must be served from root scope, not /static/
    @app.route('/sw.js')
    def service_worker():
        static_folder = app.static_folder or 'static'
        return send_from_directory(static_folder, 'sw.js',
                                   mimetype='application/javascript')

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
        """Force English if a restricted language is active outside Judge/Admin context."""
        endpoint = request.endpoint or ''
        if text.get_language() in text.RESTRICTED_LANGUAGES and not _can_access_arapaho_mode(endpoint):
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

    # --- Security headers ---
    @app.after_request
    def set_security_headers(response: Response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        if app.config.get('ENV_NAME') == 'production':
            response.headers.setdefault(
                'Strict-Transport-Security', 'max-age=31536000; includeSubDomains'
            )
        response.headers.setdefault(
            'Content-Security-Policy',
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    # --- CORS for public API endpoints ---
    @app.after_request
    def set_cors_headers(response: Response):
        if request.path.startswith('/api/public/'):
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    # --- Custom error handlers ---
    @app.errorhandler(404)
    def not_found_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Not found', 'status': 404}), 404
        return render_template('errors/404.html'), 404

    @app.errorhandler(403)
    def forbidden_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Forbidden', 'status': 403}), 403
        return render_template('errors/403.html'), 403

    @app.errorhandler(500)
    def internal_error(error):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Internal server error', 'status': 500}), 500
        return render_template('errors/500.html'), 500

    return app


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app = create_app()
    app.run(host='0.0.0.0', port=port, debug=False)
