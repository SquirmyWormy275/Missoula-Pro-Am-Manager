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


def _repair_legacy_references_in_clone(dburl: str) -> None:
    """Apply the checked, deterministic era-1 repair only to a test clone.

    The post-reseed 2026 mirror holds known stale ids in legacy Birling and
    relay documents. Current migrations deliberately fail closed until those
    ids are repaired. The repair utility resolves every reference by an exact
    stored-name match; any ambiguity aborts this fixture before it can write
    a partial result.
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from scripts import repair_era1_references as repair

    engine = sa.create_engine(dburl)
    try:
        with Session(engine) as session:
            findings = repair.audit(session)
            by_id, by_name = repair.load_rosters(session)
            plan, refusals, _details = repair.build_plan(findings, by_id, by_name)
            if refusals:
                session.rollback()
                raise RuntimeError(
                    'Historical reference repair was ambiguous; refusing to '
                    'migrate this disposable regression clone.'
                )
            if not plan:
                return

            events = repair._event_rows(session, {row_id for _store, row_id in plan})
            applied = repair.apply_plan(session, plan, events)
            defects = repair.post_check(session, plan, applied, findings)
            if defects:
                session.rollback()
                raise RuntimeError(
                    'Historical reference repair post-check failed for this '
                    'disposable regression clone.'
                )
            session.commit()
    finally:
        engine.dispose()


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
    # Hold our liveness lock before inspecting clone databases. A concurrent
    # lane sees this token as active and will not reap our per-test copies.
    with rig.hold_run_lock():
        # A killed run (Ctrl-C, CI timeout, SIGKILL) never reaches the per-test
        # finally, so clones survive. Reap only proven-dead tokenized runs.
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
        yield


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
def raw_sql(dburl):
    """Query an untouched private clone without creating or migrating Flask."""
    import sqlalchemy as sa

    engine = sa.create_engine(dburl)
    try:
        def _q(statement, **params):
            with engine.connect() as conn:
                return conn.execute(sa.text(statement), params).fetchall()

        yield _q
    finally:
        engine.dispose()


def _application_for_clone(dburl):
    """Create an application bound to one already-private clone."""
    os.environ["DATABASE_URL"] = dburl
    os.environ.setdefault("SECRET_KEY", "regression-harness-" + "x" * 48)
    if rig.APP_ROOT not in sys.path:
        sys.path.insert(0, rig.APP_ROOT)

    from app import create_app

    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


def _prepare_clone(dburl):
    """Repair and migrate a private clone before current-code route probes."""
    _repair_legacy_references_in_clone(dburl)
    application = _application_for_clone(dburl)

    from flask_migrate import upgrade

    from database import db

    migrations_dir = os.path.join(rig.APP_ROOT, "migrations")
    with application.app_context():
        # The clone is private to this test. Keep the mirror frozen, but bring
        # the clone to the schema current production code needs.
        db.engine.dispose()
        upgrade(directory=migrations_dir)
        db.engine.dispose()


@pytest.fixture()
def prepared_dburl(dburl):
    """Return a repaired, current-schema private clone for route probes.

    The mirror preserves the 2026 rows at its original migration stamp. Each
    disposable clone is upgraded to this checkout's schema before the app
    loads so current model code is tested against historical data safely.
    """
    _prepare_clone(dburl)
    return dburl


@pytest.fixture()
def prepared_app_factory():
    """Build a current-code app after a test performs clone-local setup."""
    def _build(dburl):
        _prepare_clone(dburl)
        return _application_for_clone(dburl)

    return _build


@pytest.fixture()
def app(prepared_dburl):
    """The real application, bound to a prepared production-data clone."""
    application = _application_for_clone(prepared_dburl)

    # flask_login caches the resolved user on ``g`` as ``_login_user``. Flask
    # pushes a new application context per request only when one is not already
    # current, and this fixture deliberately holds one open for the whole test
    # so that ``sql`` can read the database back afterwards. The two facts
    # combine badly: ``g`` survives from one test-client request to the next, so
    # the second request is served with the first request's identity no matter
    # what session cookie it carries.
    #
    # Measured, not theorised. A client whose session held a spectator's user id
    # was served as the STRATHEX admin, the admin's write succeeded, and the
    # assertion reported it as "a spectator changed fee payment state". Every
    # read-only role assertion in this suite was vacuous until this landed.
    #
    # Production never sees this: gunicorn pushes a fresh application context
    # per request. Restore that one property here, registered ahead of the
    # application's own hooks so the permission gate in app.py reads the
    # identity the request actually carries rather than the previous one's.
    from flask import g as _g

    def _drop_cached_login_user():
        _g.pop("_login_user", None)
        return None

    application.before_request_funcs.setdefault(None, []).insert(
        0, _drop_cached_login_user)

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
