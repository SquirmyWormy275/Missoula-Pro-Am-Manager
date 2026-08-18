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


def test_schedule_wizard_requires_preflight_before_generation():
    source = (PROJECT_ROOT / 'templates/scheduling/events.html').read_text(
        encoding='utf-8'
    )

    preflight_step = "('3', 'Preflight Check'"
    build_step = "('4', 'Generate & Print'"
    assert preflight_step in source
    assert build_step in source
    assert source.index(preflight_step) < source.index(build_step)
    assert "('3', 'Generate & Build'" not in source
    assert "('4', 'Preflight & Print'" not in source


def test_full_build_surface_exposes_fail_closed_preflight_state():
    events_source = (PROJECT_ROOT / 'templates/scheduling/events.html').read_text(
        encoding='utf-8'
    )
    preflight_source = (
        PROJECT_ROOT / 'templates/scheduling/preflight.html'
    ).read_text(encoding='utf-8')

    assert 'data-full-build-form' in events_source
    assert 'data-full-build-submit' in events_source
    assert 'data.pre_generation_has_blockers' in events_source
    assert 'Resolve hard blockers before generating the show.' in events_source
    assert 'will hold these competitors back' not in preflight_source
    assert 'The full show build is blocked' in preflight_source
    assert 'No schedule changes will be committed' in preflight_source
    assert 'heat sync mismatches' not in preflight_source


def test_mobile_operation_tabs_are_all_visible_without_horizontal_scrolling():
    source = (PROJECT_ROOT / 'templates/scheduling/events.html').read_text(
        encoding='utf-8'
    )

    assert 'grid-template-columns: repeat(2, minmax(0, 1fr));' in source
    assert '.ops-tabs .nav-item:first-child { grid-column: 1 / -1; }' in source
    assert 'overflow-x: auto;' not in source
