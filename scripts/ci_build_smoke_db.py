"""Build the seeded SQLite database that tests/test_route_smoke.py expects.

Why this exists
---------------
`smoke_env` in tests/test_route_smoke.py has two branches:

    use_real_db = SOURCE_DB.exists()          # instance/proam.db
    if use_real_db:
        shutil.copy2(SOURCE_DB, db_copy)      # cheap
    ...
    if not use_real_db:
        upgrade(directory=str(migrations_dir))  # <-- runs the WHOLE alembic
        _seed_minimal_smoke_data(app)           #     chain, per test

`instance/` is gitignored, so on a fresh CI checkout the file is absent and
CI takes the second branch for all 245 parametrized route tests. Replaying
the full migration chain 245 times inside one process leaks roughly
17 MB per test on top of the ~3.3 MB per test that `create_app()` costs on
its own. Measured stair, same machine, same suite:

    tests   unseeded   seeded
       20     554 MB    179 MB
       60    1367 MB    310 MB
      100    2181 MB    442 MB
      130    2789 MB    537 MB
      245   ~5130 MB    934 MB   (extrapolated / measured, RC=0)

Roughly 20.3 MB/test unseeded against 3.3 MB/test seeded: the alembic chain
is 84% of the growth. Building the database ONCE here and letting every test
take the `shutil.copy2` branch is what keeps the job inside the runner's
7 GB.

A bare `flask db upgrade` is NOT sufficient. The `use_real_db` branch skips
`_seed_minimal_smoke_data` entirely, and the fixture immediately does

    ids["tournament_id"] = first_tournament.id

with no None guard. A migrated-but-unseeded file would AttributeError on all
245 tests. So this script migrates AND seeds, reusing the test module's own
seeding function rather than a second copy of it that could drift.

Run from the repository root:

    python scripts/ci_build_smoke_db.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TARGET = PROJECT_ROOT / "instance" / "proam.db"


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if TARGET.exists():
        TARGET.unlink()

    # Set before importing app/tests: create_app() reads these at call time,
    # but the test module runs _discover_routes() at import time.
    os.environ["DATABASE_URL"] = f"sqlite:///{TARGET}"
    os.environ["SECRET_KEY"] = "route-smoke-secret"
    os.environ["FLASK_ENV"] = "testing"
    os.environ["TESTING"] = "1"
    # Explicitly off. This script migrates on purpose; create_all would
    # produce a schema alembic has never stamped.
    os.environ.pop("TEST_USE_CREATE_ALL", None)

    from flask_migrate import upgrade

    from app import create_app
    from tests.test_route_smoke import _seed_minimal_smoke_data

    app = create_app()
    with app.app_context():
        upgrade(directory=str(PROJECT_ROOT / "migrations"))
    _seed_minimal_smoke_data(app)

    # Fail loudly here rather than as 245 AttributeErrors later.
    with app.app_context():
        from models import Event, EventResult, Heat, Team, Tournament, User
        from models.competitor import CollegeCompetitor, ProCompetitor

        required = {
            "tournament": Tournament.query.first(),
            "event": Event.query.first(),
            "heat": Heat.query.first(),
            "user": User.query.first(),
            "team": Team.query.first(),
            "college_competitor": CollegeCompetitor.query.first(),
            "pro_competitor": ProCompetitor.query.first(),
            "event_result": EventResult.query.first(),
            "birling_event": Event.query.filter_by(stand_type="birling").first(),
            "partnered_event": Event.query.filter_by(is_partnered=True).first(),
            "relay_event": Event.query.filter_by(name="Pro-Am Relay").first(),
        }
        missing = sorted(k for k, v in required.items() if v is None)
        if missing:
            print(f"FAIL: seeded database is missing: {', '.join(missing)}")
            return 1

    print(f"OK: built {TARGET} ({TARGET.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
