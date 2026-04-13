"""Configuration constants and runtime profiles for the app."""
import os


def _project_path(*parts: str) -> str:
    """Return an absolute path rooted at the project directory."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)


def _normalized_database_url() -> str:
    default_sqlite_path = _project_path('instance', 'proam.db')
    url = os.environ.get('DATABASE_URL', f"sqlite:///{default_sqlite_path}")
    if url.startswith('postgres://'):
        return url.replace('postgres://', 'postgresql://', 1)
    return url


def _require_secret_key() -> str:
    """Return SECRET_KEY from env, or a random key for local dev.

    In production (DATABASE_URL points to PostgreSQL), a missing SECRET_KEY is
    fatal — random keys invalidate all sessions on every deploy/restart, breaking
    CSRF tokens, login sessions, and offline score replay tokens.
    """
    key = os.environ.get('SECRET_KEY', '').strip()
    if key:
        return key
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('postgres') and not os.environ.get('TESTING'):
        raise RuntimeError(
            'SECRET_KEY environment variable is required in production. '
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    # Local dev with SQLite — random key is acceptable
    return os.urandom(32).hex()


class BaseConfig:
    SECRET_KEY = _require_secret_key()
    SQLALCHEMY_DATABASE_URI = _normalized_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True}
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', _project_path('uploads'))
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max upload
    STRUCTURED_LOGGING = os.environ.get('STRUCTURED_LOGGING', '1') == '1'
    SENTRY_DSN = os.environ.get('SENTRY_DSN', '').strip()
    JOB_MAX_WORKERS = int(os.environ.get('JOB_MAX_WORKERS', '2'))
    REPORT_CACHE_TTL_SECONDS = int(os.environ.get('REPORT_CACHE_TTL_SECONDS', '60'))
    PUBLIC_CACHE_TTL_SECONDS = int(os.environ.get('PUBLIC_CACHE_TTL_SECONDS', '5'))
    ENABLE_UPLOAD_MALWARE_SCAN = os.environ.get('ENABLE_UPLOAD_MALWARE_SCAN', '0') == '1'
    MALWARE_SCAN_COMMAND = os.environ.get('MALWARE_SCAN_COMMAND', '').strip()
    EVENT_ORDER_CONFIG_PATH = os.environ.get(
        'EVENT_ORDER_CONFIG_PATH',
        _project_path('instance', 'event_order.json'),
    )
    # S3 Cloud Backup (optional — all keys required to enable)
    BACKUP_S3_BUCKET = os.environ.get('BACKUP_S3_BUCKET', '').strip()
    BACKUP_S3_PREFIX = os.environ.get('BACKUP_S3_PREFIX', 'proam-backups').strip()
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID', '').strip()
    AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', '').strip()
    AWS_DEFAULT_REGION = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1').strip()
    # Local backup directory (used as fallback when S3 is not configured)
    LOCAL_BACKUP_DIR = os.environ.get(
        'LOCAL_BACKUP_DIR',
        _project_path('instance', 'backups'),
    ).strip()
    # Twilio SMS (optional — all keys required to enable)
    TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
    TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
    TWILIO_FROM_NUMBER = os.environ.get('TWILIO_FROM_NUMBER', '').strip()
    # How many flights ahead to notify competitors (default 3)
    SMS_NOTIFY_FLIGHTS_AHEAD = int(os.environ.get('SMS_NOTIFY_FLIGHTS_AHEAD', '3'))


class DevelopmentConfig(BaseConfig):
    ENV_NAME = 'development'


class ProductionConfig(BaseConfig):
    ENV_NAME = 'production'


def _is_production_environment() -> bool:
    """Detect whether we're running in a production environment.

    Order of precedence:
      1. Explicit FLASK_ENV=testing or TESTING=1 → NOT production (tests).
      2. Explicit FLASK_ENV=development → NOT production (local dev).
      3. Explicit FLASK_ENV=production or PRODUCTION=1 → production.
      4. Auto-detect: Railway sets RAILWAY_ENVIRONMENT for every deployment.
         If that variable is set, we are on Railway → production.
      5. Auto-detect: a postgresql:// DATABASE_URL is the canonical signal
         that we are NOT on a developer laptop. validate_runtime() already
         requires postgres in production, so this is a safe equivalence.
      6. Default → development.

    The auto-detect tiers exist because the original implementation only
    looked at FLASK_ENV / PRODUCTION, and Railway does not set either by
    default. The result was a silent demotion of every production deploy
    to DevelopmentConfig — losing HSTS, SESSION_COOKIE_SECURE, and the
    entire validate_runtime() guard rail (which itself is gated on
    ENV_NAME == 'production'). See CSO follow-up.
    """
    env = os.environ.get('FLASK_ENV', '').strip().lower()
    if env == 'testing' or os.environ.get('TESTING', '').strip():
        return False
    if env == 'development':
        return False
    if env == 'production' or os.environ.get('PRODUCTION', '').strip() == '1':
        return True
    if os.environ.get('RAILWAY_ENVIRONMENT', '').strip():
        return True
    if _normalized_database_url().startswith('postgresql://'):
        return True
    return False


def get_config():
    cfg = ProductionConfig if _is_production_environment() else DevelopmentConfig
    # Always re-resolve DATABASE_URL at app creation time.
    # BaseConfig.SQLALCHEMY_DATABASE_URI is cached at class-definition time,
    # which can become stale if DATABASE_URL env var changed (e.g. in tests).
    cfg.SQLALCHEMY_DATABASE_URI = _normalized_database_url()
    return cfg


def validate_runtime(app_config: dict) -> None:
    """Fail fast for production misconfiguration that would lose data or
    weaken security. Soft-warn for production misconfiguration that the app
    can survive (e.g. STRATHMARK env vars — the integration is non-blocking
    by design and silently no-ops when not configured).

    Two tiers of checks:

      HARD FAIL (raises RuntimeError, crashes deploy):
        - SECRET_KEY missing or weak — sessions, CSRF, and replay tokens
          would all be unsigned and forgeable.
        - DATABASE_URL not postgresql:// — production runs on Railway PG.
          A SQLite fallback would write to an ephemeral container disk and
          vanish on every redeploy.

      SOFT WARN (logger.error + push to startup health record, deploy continues):
        - STRATHMARK_SUPABASE_URL / STRATHMARK_SUPABASE_KEY missing — the
          integration silently no-ops; result push and mark assignment will
          not run, but the app stays functional. The show director can fix
          the env var post-deploy without an outage.

    The hard/soft split exists because the previous implementation hard-failed
    on STRATHMARK config, which turned every Railway deploy without those env
    vars into a complete outage. The integration is non-blocking by design
    (`services/strathmark_sync.py` catches all exceptions), so a config gap
    should warn the operator, not kill the deploy.

    Soft warnings are also written to ``app_config['_PRODUCTION_WARNINGS']``
    so the /health/diag endpoint can surface them without the operator
    needing access to Railway log scrollback.
    """
    import logging
    logger = logging.getLogger(__name__)

    env_name = app_config.get('ENV_NAME', 'development')
    if env_name != 'production':
        return

    # ----- HARD FAIL CHECKS -------------------------------------------------
    secret = app_config.get('SECRET_KEY') or ''
    weak_values = {'changeme', 'secret', 'default'}
    if len(secret) < 16 or secret.lower() in weak_values:
        raise RuntimeError(
            'Invalid SECRET_KEY for production. Set a strong random secret '
            'via SECRET_KEY env var. Generate with: '
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )

    db_uri = app_config.get('SQLALCHEMY_DATABASE_URI', '') or ''
    if not db_uri.startswith('postgresql://'):
        raise RuntimeError(
            'Production requires PostgreSQL. DATABASE_URL is missing or '
            'points to SQLite. On Railway, attach a PostgreSQL service to '
            'this project — Railway sets DATABASE_URL automatically.'
        )

    # ----- SOFT WARN CHECKS -------------------------------------------------
    warnings: list[str] = []

    if not os.environ.get('STRATHMARK_SUPABASE_URL') or not os.environ.get('STRATHMARK_SUPABASE_KEY'):
        msg = (
            'STRATHMARK_SUPABASE_URL or STRATHMARK_SUPABASE_KEY is not set. '
            'STRATHMARK enrollment, result push, and mark assignment will '
            'silently no-op until both env vars are set in the Railway '
            'dashboard. The app remains fully operational for tournament '
            'logistics — only the global STRATHMARK sync is degraded.'
        )
        warnings.append(msg)
        logger.error('PRODUCTION CONFIG WARNING: %s', msg)

    # Stash warnings on the config so /health/diag can surface them.
    app_config['_PRODUCTION_WARNINGS'] = warnings


# ---------------------------------------------------------------------------
# Scoring rule helpers
# ---------------------------------------------------------------------------

# Events where the TIE-BREAKER is elapsed time (lowest time wins).
# Primary score is hits; if two competitors tie on hits, fastest time wins.
HARD_HIT_EVENTS = [
    'Underhand Hard Hit',
    'Standing Block Hard Hit',
]

# Axe throw events that use 3 cumulative throws (run1 + run2 + run3 = result).
# Excludes the Pro-Am Relay, which uses a single team time entry.
AXE_THROW_CUMULATIVE_EVENTS = [
    'Axe Throw',           # College open event
    'Partnered Axe Throw', # Pro show event
]

# ---------------------------------------------------------------------------
# Scoring points for college competition
PLACEMENT_POINTS = {
    1: 10,
    2: 7,
    3: 5,
    4: 3,
    5: 2,
    6: 1
}

# Stand types that support Championship vs. Handicap format selection (STRATHMARK integration)
HANDICAP_ELIGIBLE_STAND_TYPES = {'underhand', 'standing_block', 'springboard'}

# Dual-run events whose Run 2 splits to Saturday (day-split rule).
# Caber Toss is dual-run but both runs stay on Friday — not listed here.
DAY_SPLIT_EVENT_NAMES = {"Chokerman's Race", "Speed Climb"}

# Gear family taxonomy — groups events by shared equipment type.
# cascade=True means sharing gear in ANY event within the family creates a
# conflict in ALL events within that family (e.g. sharing an axe for springboard
# means conflict in underhand + standing block too).
# pro_only=True means the gear constraint only applies to pro events (college
# uses show-provided equipment for that stand type).
GEAR_FAMILIES = {
    'chopping': {
        'stand_types': {'underhand', 'standing_block', 'springboard'},
        'cascade': True,
    },
    'crosscut_saw': {
        'stand_types': {'saw_hand'},
        'cascade': True,
    },
    'hot_saw': {
        'stand_types': {'hot_saw'},
        'cascade': False,
    },
    'climbing': {
        'stand_types': {'speed_climb'},
        'cascade': False,
    },
    'op_saw': {
        'stand_types': {'obstacle_pole'},
        'cascade': False,
        'pro_only': True,
    },
    'cookie_stack': {
        'stand_types': {'cookie_stack'},
        'cascade': False,
    },
}

# ---------------------------------------------------------------------------
# Wood preset templates — common species/size combos for Virtual Woodboss.
# Each preset defines species + diameter for block and log categories.
# Presets are applied to all config_keys within the category.
# Custom presets can be saved to instance/wood_presets.json at runtime.
# ---------------------------------------------------------------------------
COMMON_WOOD_SPECIES = [
    'Western White Pine',
    'Cottonwood',
    'Aspen',
    'Poplar',
    'Western Larch',
    'Douglas Fir',
    'Lodgepole Pine',
    'Sitka Spruce',
    'Ponderosa Pine',
    'White Fir',
    'Western Red Cedar',
]

WOOD_PRESETS = {
    'Missoula Standard': {
        'blocks': {'species': 'Western White Pine', 'size_value': 13, 'size_unit': 'in'},
        'log_general': {'species': 'Western Larch', 'size_value': 14, 'size_unit': 'in'},
        'log_stock': {'species': 'Western Larch', 'size_value': 14, 'size_unit': 'in'},
        'log_op': {'species': 'Lodgepole Pine', 'size_value': 10, 'size_unit': 'in'},
        'log_cookie': {'species': 'Lodgepole Pine', 'size_value': 10, 'size_unit': 'in'},
    },
}

# Stand types that have NO personal gear constraints — equipment is either
# show-provided or not applicable.  The gear completeness check skips these.
NO_CONSTRAINT_STAND_TYPES = {
    'stock_saw', 'axe_throw', 'birling', 'peavey',
    'caber', 'pulp_toss', 'chokerman',
}

# Stand configurations
STAND_CONFIGS = {
    'springboard': {
        'total': 4,
        'uses_per_event': 3,
        'supports_handedness': True,
        'labels': ['Dummy 1', 'Dummy 2', 'Dummy 3', 'Dummy 4']
    },
    'underhand': {
        'total': 5,
        'labels': ['Stand 1', 'Stand 2', 'Stand 3', 'Stand 4', 'Stand 5']
    },
    'standing_block': {
        'total': 5,
        'shared_with': 'cookie_stack',
        'labels': ['Stand 1', 'Stand 2', 'Stand 3', 'Stand 4', 'Stand 5']
    },
    'cookie_stack': {
        'total': 5,
        'shared_with': 'standing_block',
        'labels': ['Stand 1', 'Stand 2', 'Stand 3', 'Stand 4', 'Stand 5']
    },
    'saw_hand': {
        'total': 8,
        'groups': [[1, 2, 3, 4], [5, 6, 7, 8]],
        'labels': ['Stand 1', 'Stand 2', 'Stand 3', 'Stand 4',
                   'Stand 5', 'Stand 6', 'Stand 7', 'Stand 8']
    },
    'stock_saw': {
        'total': 2,
        'specific_stands': [1, 2],
        'labels': ['Stand 1', 'Stand 2']
    },
    'hot_saw': {
        'total': 4,
        'specific_stands': [1, 2, 3, 4],
        'labels': ['Stand 1', 'Stand 2', 'Stand 3', 'Stand 4']
    },
    'obstacle_pole': {
        'total': 2,
        'labels': ['Pole 1', 'Pole 2']
    },
    'speed_climb': {
        'total': 2,
        'labels': ['Pole 2', 'Pole 4']
    },
    'chokerman': {
        'total': 2,
        'labels': ['Course 1', 'Course 2']
    },
    'axe_throw': {
        'total': 1,
        'labels': ['Target']
    },
    'caber': {
        'total': 1,
        'labels': ['Field']
    },
    'peavey': {
        'total': 1,
        'labels': ['Log']
    },
    'pulp_toss': {
        'total': 1,
        'labels': ['Platform']
    },
    'birling': {
        'total': 1,
        'labels': ['Pond']
    }
}

# College events - traditionally OPEN (can be configured as CLOSED)
COLLEGE_OPEN_EVENTS = [
    # Axe Throw: 3 throws, cumulative score, highest wins. Tie → throw-off.
    {'name': 'Axe Throw', 'scoring_type': 'score', 'scoring_order': 'highest_wins',
     'stand_type': 'axe_throw', 'requires_triple_runs': True},
    {'name': 'Peavey Log Roll', 'scoring_type': 'time', 'stand_type': 'peavey',
     'is_partnered': True, 'partner_gender': 'mixed'},
    # Caber Toss: 2 throws (dual run), farthest throw counts. Tie → combined.
    {'name': 'Caber Toss', 'scoring_type': 'distance', 'scoring_order': 'highest_wins',
     'stand_type': 'caber', 'requires_dual_runs': True},
    {'name': 'Pulp Toss', 'scoring_type': 'time', 'stand_type': 'pulp_toss',
     'is_partnered': True, 'partner_gender': 'mixed'},
]

# College events - CLOSED (max 6 per athlete)
COLLEGE_CLOSED_EVENTS = [
    {'name': 'Underhand Hard Hit', 'scoring_type': 'hits', 'stand_type': 'underhand', 'is_gendered': True},
    {'name': 'Underhand Speed', 'scoring_type': 'time', 'stand_type': 'underhand', 'is_gendered': True},
    {'name': 'Standing Block Hard Hit', 'scoring_type': 'hits', 'stand_type': 'standing_block', 'is_gendered': True},
    {'name': 'Standing Block Speed', 'scoring_type': 'time', 'stand_type': 'standing_block', 'is_gendered': True},
    {'name': 'Single Buck', 'scoring_type': 'time', 'stand_type': 'saw_hand', 'is_gendered': True},
    {'name': 'Double Buck', 'scoring_type': 'time', 'stand_type': 'saw_hand', 'is_gendered': True, 'is_partnered': True, 'partner_gender': 'same'},
    {'name': 'Jack & Jill Sawing', 'scoring_type': 'time', 'stand_type': 'saw_hand', 'is_partnered': True, 'partner_gender': 'mixed'},
    {'name': 'Stock Saw', 'scoring_type': 'time', 'stand_type': 'stock_saw', 'is_gendered': True},
    {'name': 'Speed Climb', 'scoring_type': 'time', 'stand_type': 'speed_climb', 'is_gendered': True, 'requires_dual_runs': True},
    {'name': 'Obstacle Pole', 'scoring_type': 'time', 'stand_type': 'obstacle_pole', 'is_gendered': True},
    {'name': 'Chokerman\'s Race', 'scoring_type': 'time', 'stand_type': 'chokerman', 'is_gendered': True, 'requires_dual_runs': True},
    {'name': 'Birling', 'scoring_type': 'bracket', 'stand_type': 'birling', 'is_gendered': True},
    {'name': '1-Board Springboard', 'scoring_type': 'time', 'stand_type': 'springboard', 'is_gendered': True},
]

# Pro events
PRO_EVENTS = [
    {'name': 'Springboard', 'scoring_type': 'time', 'stand_type': 'springboard'},
    {'name': 'Pro 1-Board', 'scoring_type': 'time', 'stand_type': 'springboard'},
    {'name': '3-Board Jigger', 'scoring_type': 'time', 'stand_type': 'springboard'},
    {'name': 'Underhand', 'scoring_type': 'time', 'stand_type': 'underhand', 'is_gendered': True},
    {'name': 'Standing Block', 'scoring_type': 'time', 'stand_type': 'standing_block', 'is_gendered': True},
    {'name': 'Stock Saw', 'scoring_type': 'time', 'stand_type': 'stock_saw', 'is_gendered': True},
    {'name': 'Hot Saw', 'scoring_type': 'time', 'stand_type': 'hot_saw'},
    {'name': 'Single Buck', 'scoring_type': 'time', 'stand_type': 'saw_hand', 'is_gendered': True},
    {'name': 'Double Buck', 'scoring_type': 'time', 'stand_type': 'saw_hand', 'is_gendered': True, 'is_partnered': True},
    {'name': 'Jack & Jill Sawing', 'scoring_type': 'time', 'stand_type': 'saw_hand', 'is_partnered': True, 'partner_gender': 'mixed'},
    # Partnered Axe Throw: 3 throws per pair, cumulative. Tie → throw-off.
    {'name': 'Partnered Axe Throw', 'scoring_type': 'score', 'scoring_order': 'highest_wins',
     'stand_type': 'axe_throw', 'is_partnered': True, 'has_prelims': True, 'requires_triple_runs': True},
    {'name': 'Obstacle Pole', 'scoring_type': 'time', 'stand_type': 'obstacle_pole'},
    {'name': 'Pole Climb', 'scoring_type': 'time', 'stand_type': 'speed_climb'},
    {'name': 'Cookie Stack', 'scoring_type': 'time', 'stand_type': 'cookie_stack'},
]

# Team constraints
MIN_TEAM_SIZE = 2  # Minimum per gender
MAX_TEAM_SIZE = 8
MAX_CLOSED_EVENTS_PER_ATHLETE = 6

# Shirt sizes
SHIRT_SIZES = ['XS', 'S', 'M', 'L', 'XL', '2XL', '3XL']

# Friday Night Feature — crowd-pleasing pro events eligible for the Friday evening showcase.
# Selections are persisted per-tournament in instance/friday_feature_<id>.json
FRIDAY_NIGHT_EVENTS = [
    'Springboard',
    'Pro 1-Board',
    '3-Board Jigger',
]

# Heat edit-lock TTL (seconds). A lock acquired when opening the entry form auto-expires
# after this duration. Centralised here so it's visible alongside other timeout constants.
HEAT_LOCK_TTL_SECONDS = 300  # 5 minutes

# Tournament lifecycle states — use these constants instead of bare strings so typos are
# caught at import time rather than silently breaking state-machine logic.
class TournamentStatus:
    SETUP = 'setup'
    COLLEGE_ACTIVE = 'college_active'
    PRO_ACTIVE = 'pro_active'
    COMPLETED = 'completed'
    # Convenience tuple for "tournament is currently running" queries.
    ACTIVE_STATUSES = ('setup', 'college_active', 'pro_active')

# Normalised event names (lower-case, alphanum-only) that are tracked as sign-up lists
# only — no heats are generated for them. Defined here so both routes/scheduling.py and
# services/heat_generator.py read from a single source of truth.
LIST_ONLY_EVENT_NAMES = {
    'axethrow',
    'peaveylogroll',
    'cabertoss',
    'pulptoss',
}


def event_rank_category(event) -> 'str | None':
    """
    Return the ProEventRank category string for *event*, or None if the event
    type is not tracked by the ability-ranking system.

    This is the single source of truth for stand_type → rank category mapping.
    Both routes/scheduling.py and services/heat_generator.py import this function
    instead of maintaining their own copies.
    """
    if event is None:
        return None
    st = getattr(event, 'stand_type', None)
    if st == 'springboard':
        # Differentiate springboard sub-events by name.
        name = getattr(event, 'name', '') or ''
        name_lower = name.lower()
        if '1-board' in name_lower or '1 board' in name_lower:
            return 'pro_1board'
        if '3-board' in name_lower or '3 board' in name_lower or 'jigger' in name_lower:
            return '3board_jigger'
        return 'springboard'
    if st == 'underhand':
        return 'underhand'
    if st == 'standing_block':
        return 'standing_block'
    if st == 'obstacle_pole':
        return 'obstacle_pole'
    if st == 'saw_hand':
        if not getattr(event, 'is_partnered', False):
            return 'singlebuck'
        if getattr(event, 'partner_gender', None) == 'mixed':
            return 'jack_jill'
        return 'doublebuck'
    if st == 'birling':
        return 'birling'
    return None


# Valid event categories for ProEventRank.  Moved here from models/pro_event_rank.py
# so services and routes can import them without pulling in the full ORM model.
RANKED_CATEGORIES = {
    'springboard',
    'pro_1board',
    '3board_jigger',
    'underhand',
    'standing_block',
    'obstacle_pole',
    'singlebuck',
    'doublebuck',
    'jack_jill',
    'birling',
}

CATEGORY_DISPLAY_NAMES = {
    'springboard': 'Springboard',
    'pro_1board': 'Pro 1-Board',
    '3board_jigger': '3-Board Jigger',
    'underhand': 'Underhand',
    'standing_block': 'Standing Block',
    'obstacle_pole': 'Obstacle Pole',
    'singlebuck': 'Single Buck',
    'doublebuck': 'Double Buck',
    'jack_jill': 'Jack & Jill Sawing',
    'birling': 'Birling',
}

CATEGORY_DESCRIPTIONS = {
    'springboard': "Men's and Women's Springboard",
    'pro_1board': "Men's and Women's Pro 1-Board",
    '3board_jigger': "Men's and Women's 3-Board Jigger",
    'underhand': "Men's and Women's Underhand Butcher Block",
    'standing_block': "Men's and Women's Standing Block (Speed & Hard Hit)",
    'obstacle_pole': "Men's and Women's Obstacle Pole",
    'singlebuck': "Men's and Women's Single Buck",
    'doublebuck': "Men's and Women's Double Buck",
    'jack_jill': 'Jack & Jill Sawing (mixed gender)',
    'birling': "Double-elimination bracket seeding (Men's and Women's)",
}

# Saturday priority ordering defaults (override via EVENT_ORDER_CONFIG_PATH if needed).
COLLEGE_SATURDAY_PRIORITY_DEFAULT = [
    ('Standing Block Speed', 'M'),
    ('Standing Block Hard Hit', 'M'),
    ('Standing Block Speed', 'F'),
    ('Standing Block Hard Hit', 'F'),
    ('Obstacle Pole', 'M'),
    ('Obstacle Pole', 'F'),
    ('Double Buck', 'M'),
    ('Double Buck', 'F'),
    ('Jack & Jill Sawing', None),
    ('Jack & Jill Sawing', 'M'),
    ('Jack & Jill Sawing', 'F'),
]
