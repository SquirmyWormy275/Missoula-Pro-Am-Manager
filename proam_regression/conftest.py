"""
Pytest wiring for the real-data regression harness.

Each test function gets its own clone of the production mirror and its own
Flask app bound to that clone. Nothing is shared, so a test that corrupts data
cannot poison the next one, and test order is irrelevant.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rig  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "sev1: race-day fatal, confirmed on real data")
    config.addinivalue_line(
        "markers", "sev2: workflow breaking, confirmed on real data")
    config.addinivalue_line(
        "markers", "sev3: degraded, confirmed on real data")
    config.addinivalue_line(
        "markers", "slow: takes more than a few seconds")


@pytest.fixture(scope="session", autouse=True)
def _require_real_data():
    # A killed run (Ctrl-C, CI timeout, SIGKILL) never reaches the per-test
    # finally, so clones survive. Reap them before starting.
    orphans = rig.drop_orphans()
    if orphans:
        print(f"\nreaped {len(orphans)} orphan clone(s) from a previous run")
    if not rig.template_is_loaded():
        pytest.exit(
            f"Template database '{rig.TEMPLATE_DB}' is missing or holds no "
            f"production rows. This suite refuses to run against synthetic "
            f"data. Load the production dump first.",
            returncode=3,
        )


@pytest.fixture()
def dburl():
    """A private clone of the real production database, dropped after the test."""
    name, url = rig.clone_production()
    try:
        yield url
    finally:
        drop = os.environ.get("PROAM_KEEP_CLONES", "") != "1"
        if drop:
            rig.drop_clone(name)


@pytest.fixture()
def app(dburl):
    """The real application, bound to this test's private production clone."""
    os.environ["DATABASE_URL"] = dburl
    os.environ.setdefault("SECRET_KEY", "regression-harness-" + "x" * 48)
    if rig.APP_ROOT not in sys.path:
        sys.path.insert(0, rig.APP_ROOT)

    # create_app re-resolves DATABASE_URL at call time (config.py:120-123),
    # so a per-test app really does bind to this clone.
    from app import create_app

    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        yield application


@pytest.fixture()
def client(app):
    """An HTTP client authenticated as the real STRATHEX admin."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(rig.ADMIN_USER_ID)
        sess["_fresh"] = True
    return c


@pytest.fixture()
def sql(app):
    """Read the database back. Assertions check stored state, not return values."""
    from database import db

    def _q(statement, **params):
        return db.session.execute(db.text(statement), params).fetchall()

    return _q


@pytest.fixture()
def flashes(client):
    """Drain and return flashed messages as (category, text) tuples."""
    def _drain():
        with client.session_transaction() as sess:
            out = list(sess.get("_flashes", []))
            sess["_flashes"] = []
            return out
    return _drain


TID = rig.TOURNAMENT_ID
