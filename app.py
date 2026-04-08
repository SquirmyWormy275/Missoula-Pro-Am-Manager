"""
Flask application entry point for the Missoula Pro Am Tournament Manager.
"""
import os
import re
import secrets as _secrets
import time

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    render_template,
    request,
    send_from_directory,
    session,
)
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine

import config
import strings as text
from database import db, init_db
from services.background_jobs import configure as configure_jobs
from services.logging_setup import (
    configure_error_monitoring,
    configure_logging,
    request_id_middleware,
)

# SECURITY FIX (CSO #7): pre-compiled regexes for after_request CSP nonce injection.
# Each inline <script> and <style> tag in the response body gets the per-request
# nonce stamped onto it so a strict CSP without 'unsafe-inline' still allows them.
_SCRIPT_OPEN_RE = re.compile(r'<script\b([^>]*)>', re.IGNORECASE)
_STYLE_OPEN_RE = re.compile(r'<style\b([^>]*)>', re.IGNORECASE)


def _inject_csp_nonce(body: str, nonce: str) -> str:
    """Stamp nonce="<nonce>" onto every inline <script> and <style> open tag.

    Skips tags that already carry a nonce= attribute. External scripts (with
    src=) get a nonce too — harmless and keeps things uniform.
    """
    if not nonce or not body:
        return body

    def _script(match):
        attrs = match.group(1) or ''
        if 'nonce=' in attrs.lower():
            return match.group(0)
        return f'<script nonce="{nonce}"{attrs}>'

    def _style(match):
        attrs = match.group(1) or ''
        if 'nonce=' in attrs.lower():
            return match.group(0)
        return f'<style nonce="{nonce}"{attrs}>'

    body = _SCRIPT_OPEN_RE.sub(_script, body)
    body = _STYLE_OPEN_RE.sub(_style, body)
    return body

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
    'main.health_diag',
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

def _print_startup_error_banner(error: BaseException) -> None:
    """Print an impossible-to-miss boxed error banner to stderr.

    Designed for Railway / gunicorn deploy logs where a tight 1-line traceback
    is easy to scroll past. The banner makes the error visible at a glance and
    tells the operator exactly which env var to set to fix it.

    Always writes to stderr (not the structured logger) so it appears even if
    logging configuration itself crashed during boot.
    """
    import sys as _sys
    import traceback as _tb

    msg = str(error) or error.__class__.__name__
    width = max(80, min(120, max(len(line) for line in msg.split('\n')) + 4))
    bar = '#' * width
    title = ' STARTUP FAILED — Missoula Pro-Am Manager '
    title_bar = '#' + title.center(width - 2, '#') + '#'

    lines: list[str] = []
    lines.append('')
    lines.append(bar)
    lines.append(title_bar)
    lines.append(bar)
    lines.append('')
    for raw in msg.split('\n'):
        # Wrap manually so the banner stays inside `width` chars.
        while len(raw) > width - 4:
            lines.append('  ' + raw[:width - 4])
            raw = raw[width - 4:]
        lines.append('  ' + raw)
    lines.append('')
    lines.append('  TROUBLESHOOTING:')
    lines.append('    1. SECRET_KEY too short or weak?')
    lines.append('       Set SECRET_KEY env var (>=16 chars). Generate with:')
    lines.append('         python -c "import secrets; print(secrets.token_hex(32))"')
    lines.append('    2. DATABASE_URL not postgres in production?')
    lines.append('       Attach a PostgreSQL service in the Railway dashboard.')
    lines.append('       Railway sets DATABASE_URL automatically when you do.')
    lines.append('    3. Other env-var issue?')
    lines.append('       Curl /health/diag on the deployed app for full state.')
    lines.append('')
    lines.append('  Full traceback below:')
    lines.append(bar)
    print('\n'.join(lines), file=_sys.stderr, flush=True)
    _tb.print_exc(file=_sys.stderr)
    print(bar, file=_sys.stderr, flush=True)
    print('', file=_sys.stderr, flush=True)


def create_app():
    """Create and configure the Flask application."""
    try:
        return _create_app_inner()
    except BaseException as exc:
        # Catch BaseException (not just Exception) so SystemExit / KeyboardInterrupt
        # also surface a clear banner instead of vanishing into gunicorn's worker
        # restart loop. The banner is the LAST thing operators see before the
        # container crashes — make it count.
        _print_startup_error_banner(exc)
        raise


def _create_app_inner():
    """Inner factory — wrapped by create_app() with the startup error banner."""
    app = Flask(__name__)
    app.config.from_object(config.get_config())
    config.validate_runtime(app.config)

    # SAFEGUARD: Block tests from ever touching the production database.
    # If TESTING is True, the DB URI must NOT point to the real proam.db.
    if app.config.get('TESTING'):
        db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        if 'instance' in db_uri and 'proam.db' in db_uri:
            raise RuntimeError(
                'FATAL: Test is about to use the PRODUCTION database '
                f'({db_uri}). Tests MUST use a temporary database. '
                'Use create_test_app() from tests/db_test_utils.py.'
            )
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
                from models import Event as _Event
                from models import Heat
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
    from routes.demo_data import demo_bp
    from routes.import_routes import import_pro_bp
    from routes.main import main_bp
    from routes.partnered_axe import bp as partnered_axe_bp
    from routes.proam_relay import bp as proam_relay_bp
    from routes.registration import registration_bp
    from routes.reporting import reporting_bp
    from routes.scheduling import scheduling_bp
    from routes.scoring import scoring_bp
    from routes.strathmark import strathmark_bp
    from routes.validation import bp as validation_bp
    from routes.woodboss import woodboss_bp, woodboss_public_bp
    if HAS_FLASK_LOGIN:
        from routes.api import api_bp
        from routes.auth import auth_bp
        from routes.portal import portal_bp

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
    def _generate_csp_nonce():
        """Generate a per-request CSP nonce. Used by both the CSP header and
        the after_request inline-tag injector."""
        g.csp_nonce = _secrets.token_urlsafe(16)

    @app.before_request
    def enforce_language_access():
        """Force English if a restricted language is active outside Judge/Admin context."""
        endpoint = request.endpoint or ''
        if text.get_language() in text.RESTRICTED_LANGUAGES and not _can_access_arapaho_mode(endpoint):
            text.set_language('en')
        return None

    @app.after_request
    def apply_html_post_processing(response: Response):
        """Final HTML body transforms: language translation + CSP nonce injection.

        Both transforms need to mutate the response body, so they share a single
        round-trip through response.get_data() / set_data() to keep cost down.

        SECURITY FIX (CSO #7): the CSP nonce injector stamps every inline
        <script> and <style> open tag with the per-request nonce so a strict
        CSP without 'unsafe-inline' still permits them. Inline event handlers
        (onclick=, onsubmit=, ...) are NOT covered by nonce — they were
        converted to data-attribute delegation in csp_handlers.js.
        """
        content_type = response.content_type or ''
        if response.direct_passthrough or 'text/html' not in content_type:
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response

        body = response.get_data(as_text=True)
        changed = False

        # 1. Optional Arapaho translation
        if text.get_language() == 'arp':
            translated = text.translate_html(body)
            if translated != body:
                body = translated
                changed = True

        # 2. CSP nonce injection (always, when a nonce was generated)
        nonce = getattr(g, 'csp_nonce', None)
        if nonce:
            new_body = _inject_csp_nonce(body, nonce)
            if new_body != body:
                body = new_body
                changed = True

        if changed:
            response.set_data(body)
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
        # SECURITY FIX (CSO #7): nonce-based CSP for script-src.
        #
        # 'unsafe-inline' is dropped from script-src — inline <script> blocks
        # are stamped with the per-request nonce by apply_html_post_processing,
        # and inline event handlers (onclick=, onsubmit=, ...) were converted
        # to data-attribute delegation in static/js/csp_handlers.js.
        #
        # style-src KEEPS 'unsafe-inline' (no nonce) because CSP3 browsers
        # ignore 'unsafe-inline' once a nonce is present, which would break
        # every style="..." attribute in the templates (there are hundreds —
        # Bootstrap relies on inline styles for display:none toggles, transient
        # widths, etc.). Style-based XSS is rare and low-impact compared to
        # script execution; this trade-off is intentional.
        nonce = getattr(g, 'csp_nonce', '')
        script_nonce = f"'nonce-{nonce}' " if nonce else ''
        response.headers.setdefault(
            'Content-Security-Policy',
            "default-src 'self'; "
            f"script-src 'self' {script_nonce}https://cdn.jsdelivr.net; "
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
