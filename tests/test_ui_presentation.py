"""Static presentation contracts that must hold without a network connection."""

from pathlib import Path

from routes.demo_data import DEMO_TOURNAMENT_NAME

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_demo_name_does_not_duplicate_the_year():
    assert DEMO_TOURNAMENT_NAME == '[DEMO] Missoula Pro-Am'


def test_vendored_ui_runtime_is_present():
    required_assets = (
        'static/vendor/bootstrap/css/bootstrap.min.css',
        'static/vendor/bootstrap/js/bootstrap.bundle.min.js',
        'static/vendor/bootstrap-icons/font/bootstrap-icons.min.css',
        'static/vendor/bootstrap-icons/font/fonts/bootstrap-icons.woff2',
        'static/vendor/sortable/Sortable.min.js',
    )

    for relative_path in required_assets:
        asset = PROJECT_ROOT / relative_path
        assert asset.is_file(), f'missing vendored UI asset: {relative_path}'
        assert asset.stat().st_size > 1_000, f'invalid vendored UI asset: {relative_path}'


def test_templates_do_not_depend_on_public_ui_cdns():
    forbidden_hosts = ('cdn.jsdelivr.net', 'fonts.googleapis.com', 'fonts.gstatic.com')
    templates = PROJECT_ROOT / 'templates'

    for template in templates.rglob('*.html'):
        source = template.read_text(encoding='utf-8')
        for host in forbidden_hosts:
            assert host not in source, f'{template.relative_to(PROJECT_ROOT)} uses {host}'
