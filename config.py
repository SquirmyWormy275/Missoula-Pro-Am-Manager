"""Configuration constants and runtime profiles for the app."""
import os


def _normalized_database_url() -> str:
    url = os.environ.get('DATABASE_URL', 'sqlite:///proam.db')
    if url.startswith('postgres://'):
        return url.replace('postgres://', 'postgresql://', 1)
    return url


class BaseConfig:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')
    SQLALCHEMY_DATABASE_URI = _normalized_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True}
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max upload
    STRUCTURED_LOGGING = os.environ.get('STRUCTURED_LOGGING', '1') == '1'
    SENTRY_DSN = os.environ.get('SENTRY_DSN', '').strip()
    JOB_MAX_WORKERS = int(os.environ.get('JOB_MAX_WORKERS', '2'))
    REPORT_CACHE_TTL_SECONDS = int(os.environ.get('REPORT_CACHE_TTL_SECONDS', '60'))
    ENABLE_UPLOAD_MALWARE_SCAN = os.environ.get('ENABLE_UPLOAD_MALWARE_SCAN', '0') == '1'
    MALWARE_SCAN_COMMAND = os.environ.get('MALWARE_SCAN_COMMAND', '').strip()
    EVENT_ORDER_CONFIG_PATH = os.environ.get('EVENT_ORDER_CONFIG_PATH', 'instance/event_order.json')


class DevelopmentConfig(BaseConfig):
    ENV_NAME = 'development'


class ProductionConfig(BaseConfig):
    ENV_NAME = 'production'


def get_config():
    env = os.environ.get('FLASK_ENV', '').strip().lower()
    if env == 'production' or os.environ.get('PRODUCTION', '').strip() == '1':
        return ProductionConfig
    return DevelopmentConfig


def validate_runtime(app_config: dict) -> None:
    """Fail fast for production misconfiguration."""
    env_name = app_config.get('ENV_NAME', 'development')
    if env_name != 'production':
        return

    secret = app_config.get('SECRET_KEY') or ''
    weak_values = {'dev-key-change-in-production', 'changeme', 'secret', 'default'}
    if len(secret) < 16 or secret.lower() in weak_values:
        raise RuntimeError('Invalid SECRET_KEY for production. Set a strong random secret.')

# Scoring points for college competition
PLACEMENT_POINTS = {
    1: 10,
    2: 7,
    3: 5,
    4: 3,
    5: 2,
    6: 1
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
    {'name': 'Axe Throw', 'scoring_type': 'score', 'stand_type': 'axe_throw'},
    {'name': 'Peavey Log Roll', 'scoring_type': 'time', 'stand_type': 'peavey', 'is_partnered': True, 'partner_gender': 'mixed'},
    {'name': 'Caber Toss', 'scoring_type': 'distance', 'stand_type': 'caber'},
    {'name': 'Pulp Toss', 'scoring_type': 'time', 'stand_type': 'pulp_toss', 'is_partnered': True, 'partner_gender': 'mixed'},
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
    {'name': 'Partnered Axe Throw', 'scoring_type': 'score', 'stand_type': 'axe_throw', 'is_partnered': True, 'has_prelims': True},
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

# Saturday priority ordering defaults (override via EVENT_ORDER_CONFIG_PATH if needed).
COLLEGE_SATURDAY_PRIORITY_DEFAULT = [
    ('Standing Block Speed', 'M'),
    ('Standing Block Hard Hit', 'M'),
    ('Standing Block Speed', 'F'),
    ('Standing Block Hard Hit', 'F'),
    ('Obstacle Pole', 'M'),
]
