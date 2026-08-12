"""QA route smoke tests over the full Flask URL map."""
from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlencode

import pytest

from app import create_app


def _discover_routes() -> list[dict[str, object]]:
    """Return a normalized route list from the app URL map."""
    app = create_app()
    routes: list[dict[str, object]] = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: (r.endpoint, r.rule)):
        methods = sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"})
        if not methods:
            continue
        method = "GET" if "GET" in methods else "POST"
        routes.append(
            {
                "endpoint": rule.endpoint,
                "rule": rule.rule,
                "method": method,
                "methods": methods,
            }
        )
    return routes


ROUTE_SPECS = _discover_routes()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_DB_ENV = os.environ.get("PROAM_ROUTE_SMOKE_SOURCE_DB", "").strip()
SOURCE_DB = Path(_SOURCE_DB_ENV) if _SOURCE_DB_ENV else PROJECT_ROOT / "instance" / "proam.db"
if not SOURCE_DB.is_absolute():
    SOURCE_DB = PROJECT_ROOT / SOURCE_DB
TMP_ROOT = PROJECT_ROOT / ".qa_tmp"


def _seed_minimal_smoke_data(app):
    """Seed a fresh migrated DB with minimal entities for route smoke tests.

    Used in CI where instance/proam.db is absent (gitignored). Creates one of
    each entity the smoke_env fixture needs: tournament, event (birling +
    regular + Pro-Am Relay), heat, user, team, college + pro competitors,
    and one EventResult for settlement-toggle routes.
    """
    from database import db as _db
    from models.relay import RelayState, RelayTeam, RelayTeamEvent
    from models.user import User
    from tests.conftest import (
        make_college_competitor,
        make_event,
        make_event_result,
        make_heat,
        make_pro_competitor,
        make_team,
        make_tournament,
    )

    with app.app_context():
        admin = User(username="smoke_admin", role="admin")
        admin.set_password("smoketest")
        _db.session.add(admin)
        _db.session.flush()

        tournament = make_tournament(_db.session, name="Smoke Tournament", year=2026)
        team = make_team(_db.session, tournament)

        college = make_college_competitor(
            _db.session, tournament, team, name="Smoke College", gender="M", events=[]
        )
        pro = make_pro_competitor(
            _db.session, tournament, name="Smoke Pro", gender="M", events=[]
        )

        # Partnered event first so it becomes the first_event — needed for
        # partner_queue / reassign_partner routes which 404 on non-partnered events
        event = make_event(
            _db.session, tournament, name="Jack & Jill Sawing", event_type="pro",
            scoring_type="time", stand_type="saw_hand", is_partnered=True,
        )
        make_event(
            _db.session, tournament, name="Underhand", event_type="pro",
            gender="M", stand_type="underhand",
        )
        make_event(
            _db.session, tournament, name="Birling", event_type="college",
            gender="M", scoring_type="bracket", stand_type="birling",
        )
        # Relay payout routes require a final, payable relay team rather than
        # the generic college Team record used by other routes.
        relay_event = make_event(
            _db.session, tournament, name="Pro-Am Relay", event_type="pro",
            scoring_type="time", stand_type="underhand", payouts={"1": 100.0},
        )
        relay_state = RelayState(event_id=relay_event.id, status="completed")
        _db.session.add(relay_state)
        _db.session.flush()
        relay_team = RelayTeam(
            relay_state_id=relay_state.id,
            team_number=1,
            name="Smoke Relay Team",
            total_time=100.0,
        )
        _db.session.add(relay_team)
        _db.session.flush()
        for event_key in (
            "partnered_sawing",
            "standing_butcher_block",
            "underhand_butcher_block",
            "team_axe_throw",
        ):
            _db.session.add(RelayTeamEvent(
                relay_team_id=relay_team.id,
                event_key=event_key,
                result=25.0,
                status="completed",
            ))
        make_heat(_db.session, event, heat_number=1, competitors=[pro.id])
        # EventResult needed for toggle_settlement route
        make_event_result(
            _db.session, event, pro, competitor_type="pro",
            result_value=90.0, status="completed",
        )

        _db.session.commit()


@pytest.fixture()
def smoke_env(monkeypatch):
    """Return a fresh app/client pair backed by either a copied real DB
    (local dev) or a freshly migrated + seeded DB (CI)."""
    TMP_ROOT.mkdir(exist_ok=True)
    temp_dir = TMP_ROOT / f"route-smoke-{uuid.uuid4().hex}"
    temp_dir.mkdir()
    db_copy = temp_dir / "proam-copy.db"

    use_real_db = SOURCE_DB.exists()
    if use_real_db:
        # D12-C commit F3: a copy of the developer database is only useful if
        # the chain will run over it. Revision t9b3c4d5e6f7 refuses one that
        # holds a heat whose roster exists only in heats.competitors, which
        # any database stamped before D12-C commit E can be. Asking first
        # costs one chain replay per session; not asking cost 243 setup
        # errors, none of them about a route.
        #
        # The fresh-DB branch below is the one CI has always taken, so
        # falling back to it loses no coverage, and it replays the whole
        # chain itself, so a genuinely broken migration still fails loudly
        # there instead of hiding here. The only thing this can mask is a
        # bad source database, which is the thing it is reporting.
        from tests.db_test_utils import source_db_reaches_head
        stale = source_db_reaches_head(SOURCE_DB)
        if stale:
            print(
                f"route smoke: {SOURCE_DB} cannot reach chain head, seeding a "
                f"fresh database instead. {stale}"
            )
            use_real_db = False
    if use_real_db:
        shutil.copy2(SOURCE_DB, db_copy)

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_copy}")
    monkeypatch.setenv("SECRET_KEY", "route-smoke-secret")
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("TESTING", "1")

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    # Migrate unconditionally, including the copied database.  A copy of
    # instance/proam.db is stamped at whatever revision that developer's
    # machine last ran, which is not necessarily head, and running these routes
    # against a stale schema produces failures that look like route bugs.
    # D12-C commit A is where this first bit: nine smoke tests failed with
    # "no such column: heat_assignments.uid" on a machine holding a dev
    # database, and passed in CI, where SOURCE_DB does not exist and the
    # fresh-DB branch below already migrated.  On a copy already at head this
    # is a no-op that costs one alembic_version read.
    from flask_migrate import upgrade
    migrations_dir = PROJECT_ROOT / "migrations"
    with app.app_context():
        upgrade(directory=str(migrations_dir))

    if not use_real_db:
        _seed_minimal_smoke_data(app)

    with app.app_context():
        from models import Event, EventResult, Heat, Team, Tournament, User
        from models.competitor import CollegeCompetitor, ProCompetitor
        from models.relay import RelayTeam

        first_tournament = Tournament.query.order_by(Tournament.id).first()
        first_event = Event.query.order_by(Event.id).first()
        first_heat = Heat.query.order_by(Heat.id).first()
        first_user = User.query.order_by(User.id).first()
        first_team = Team.query.order_by(Team.id).first()
        first_college = CollegeCompetitor.query.order_by(CollegeCompetitor.id).first()
        first_pro = ProCompetitor.query.order_by(ProCompetitor.id).first()
        birling_event = Event.query.filter_by(stand_type="birling").order_by(Event.id).first()
        first_result = EventResult.query.order_by(EventResult.id).first()
        # Route-specific event lookups (some routes 404 on wrong event type)
        partnered_event = Event.query.filter_by(is_partnered=True).order_by(Event.id).first()
        relay_event = Event.query.filter_by(name="Pro-Am Relay").order_by(Event.id).first()
        relay_team = RelayTeam.query.order_by(RelayTeam.id).first()

        ids = {
            "tournament_id": first_tournament.id,
            "tid": first_tournament.id,
            "event_id": first_event.id if first_event else None,
            "heat_id": first_heat.id if first_heat else None,
            "heat_event_id": first_heat.event_id if first_heat else None,
            "user_id": first_user.id if first_user else None,
            "team_id": first_team.id if first_team else None,
            "college_competitor_id": first_college.id if first_college else None,
            "pro_competitor_id": first_pro.id if first_pro else None,
            "competitor_id": first_college.id if first_college else None,
            "competitor_type": "pro",
            "portal_competitor_id": first_pro.id if first_pro else None,
            "birling_event_id": birling_event.id if birling_event else None,
            "partnered_event_id": partnered_event.id if partnered_event else None,
            "relay_event_id": relay_event.id if relay_event else None,
            "relay_tournament_id": relay_event.tournament_id if relay_event else None,
            "relay_team_id": relay_team.id if relay_team else None,
            "result_id": first_result.id if first_result else None,
            "flight_id": None,
            "job_id": None,
            "competition_type": "college",
            "lang_code": "en",
            "filename": "img/favicon.svg",
            "headshot_filename": (
                ProCompetitor.query.filter(
                    ProCompetitor.headshot_filename.isnot(None),
                    ProCompetitor.headshot_filename != "",
                )
                .order_by(ProCompetitor.id)
                .with_entities(ProCompetitor.headshot_filename)
                .scalar()
                or CollegeCompetitor.query.filter(
                    CollegeCompetitor.headshot_filename.isnot(None),
                    CollegeCompetitor.headshot_filename != "",
                )
                .order_by(CollegeCompetitor.id)
                .with_entities(CollegeCompetitor.headshot_filename)
                .scalar()
            ),
        }

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(ids["user_id"])
        sess["_fresh"] = True

    try:
        yield {"app": app, "client": client, "ids": ids}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _build_path(rule: str, ids: dict[str, object]) -> str:
    """Resolve a Flask rule string to a concrete path."""
    event_id = ids["event_id"]
    tournament_id = ids["tournament_id"]
    competitor_id = ids["competitor_id"]
    competitor_type = ids["competitor_type"]
    team_id = ids["team_id"]

    if "/birling" in rule and ids["birling_event_id"] is not None:
        event_id = ids["birling_event_id"]
    if ("/partner-queue" in rule or "/reassign-partner" in rule) and ids.get("partnered_event_id") is not None:
        event_id = ids["partnered_event_id"]
    if "/proam-relay/payouts" in rule and ids.get("relay_tournament_id") is not None:
        tournament_id = ids["relay_tournament_id"]
    if "/proam-relay/team/" in rule:
        team_id = ids["relay_team_id"]
    if "/delete-heat/" in rule:
        event_id = ids["heat_event_id"]
    if "/pro/" in rule:
        competitor_id = ids["pro_competitor_id"]
    if "/college/competitor/" in rule:
        competitor_id = ids["college_competitor_id"]
    if "/portal/competitor/" in rule and "/my-results" in rule:
        competitor_id = ids["portal_competitor_id"]
        competitor_type = ids["competitor_type"]

    replacements = {
        "<int:tournament_id>": str(tournament_id),
        "<int:tid>": str(tournament_id),
        "<int:event_id>": str(event_id),
        "<int:eid>": str(event_id),
        "<int:heat_id>": str(ids["heat_id"]),
        "<int:source_heat_id>": str(ids["heat_id"]),
        "<int:user_id>": str(ids["user_id"]),
        "<int:team_id>": str(team_id) if team_id is not None else "",
        "<int:competitor_id>": str(competitor_id) if competitor_id is not None else "",
        "<int:flight_id>": str(ids["flight_id"]) if ids["flight_id"] is not None else "",
        "<int:result_id>": str(ids["result_id"]) if ids.get("result_id") is not None else "",
        "<int:rid>": str(ids["result_id"]) if ids.get("result_id") is not None else "",
        "<job_id>": str(ids["job_id"]) if ids["job_id"] is not None else "",
        "<competition_type>": str(ids["competition_type"]),
        "<lang_code>": str(ids["lang_code"]),
        "<competitor_type>": str(competitor_type),
        "<path:filename>": str(ids["filename"]),
        "<path:headshot_filename>": str(ids["headshot_filename"]) if ids["headshot_filename"] else "",
    }
    path = rule
    for pattern, value in replacements.items():
        path = path.replace(pattern, value)
    return path


def _should_skip(rule: str, ids: dict[str, object]) -> str | None:
    """Return a skip reason when no real parameter exists for a route."""
    if "<int:heat_id>" in rule and ids["heat_id"] is None:
        return "no real heat_id exists in Phase 0B database state"
    if "<int:event_id>" in rule and ids["event_id"] is None:
        return "no real event_id exists in Phase 0B database state"
    if "<int:flight_id>" in rule and ids["flight_id"] is None:
        return "no real flight_id exists in Phase 0B database state"
    if "<job_id>" in rule and ids["job_id"] is None:
        return "no real async job_id exists in Phase 0B database state"
    if "/birling" in rule and ids["birling_event_id"] is None:
        return "no real birling event exists in Phase 0B database state"
    if "<int:team_id>" in rule and ids["team_id"] is None:
        return "no real team_id exists in Phase 0B database state"
    if "/college/competitor/" in rule and ids["college_competitor_id"] is None:
        return "no real college competitor exists in Phase 0B database state"
    if "/pro/" in rule and "<int:competitor_id>" in rule and ids["pro_competitor_id"] is None:
        return "no real pro competitor exists in Phase 0B database state"
    if "/portal/competitor/" in rule and "/my-results" in rule and ids["portal_competitor_id"] is None:
        return "no real portal competitor exists in Phase 0B database state"
    if "registration/headshots/<path:filename>" in rule and not ids["headshot_filename"]:
        return "no real headshot filename exists in Phase 0B database state"
    if ("<int:rid>" in rule or "<int:result_id>" in rule) and ids.get("result_id") is None:
        return "no real result_id exists in Phase 0B database state"
    if ("/partner-queue" in rule or "/reassign-partner" in rule) and ids.get("partnered_event_id") is None:
        return "no partnered event exists in Phase 0B database state"
    if "/proam-relay/payouts" in rule and ids.get("relay_event_id") is None:
        return "no Pro-Am Relay event exists in Phase 0B database state"
    if "/proam-relay/team/" in rule and ids.get("relay_team_id") is None:
        return "no final Pro-Am Relay team exists in Phase 0B database state"

    # Generic backstop.  _build_path resolves parameters through a fixed
    # lookup table, so any route carrying a parameter name that is not in that
    # table used to be requested with the literal "<int:conflict_id>" still in
    # the URL, which 404s and reads as a route failure.  Detect the leftover
    # and skip with the parameter named, so adding a route with a new
    # parameter produces an actionable skip instead of a mystery red.
    unresolved = re.findall(r"<[^>]+>", _build_path(rule, ids))
    if unresolved:
        return (
            "route parameter(s) %s have no resolver in _build_path; add them "
            "to the replacements table to smoke this route"
            % ", ".join(unresolved)
        )
    return None


# Query arguments a route needs before it can answer at all, keyed by
# endpoint.  This is the query-string counterpart to the replacements table in
# _build_path, and it exists for the same reason: the smoke request has to be
# well formed before "did it 500" means anything.
#
# scoring.scratch_preview: college and pro competitor ids come from separate
# autoincrement sequences and collide, so the view refuses a bare id with 400
# rather than guessing which of the two people the judge meant.  ids
# ["competitor_id"] is the first COLLEGE competitor, so the request says so.
# Deliberately not widening the GET allowlist to include 400 instead: that
# would let every GET route in the app answer 400 and still pass.
_QUERY_ARGS: dict[str, dict[str, str]] = {
    "scoring.scratch_preview": {"competitor_type": "college"},
}

# Some mutation routes deliberately reject a syntactically valid request when
# the generated smoke fixture cannot satisfy the domain precondition. Relay
# settlement is only available for a completed relay team with a configured
# prize, so its placeholder team id correctly returns 404 outside that state.
_POST_ALLOWED_STATUS: dict[str, set[int]] = {
    "proam_relay.toggle_relay_settlement": {404},
}


def _run_smoke(route: dict[str, object], smoke_env) -> None:
    """Execute one smoke request and assert it does not 500."""
    method = str(route["method"])
    rule = str(route["rule"])
    endpoint = str(route["endpoint"])
    client = smoke_env["client"]
    ids = smoke_env["ids"]

    skip_reason = _should_skip(rule, ids)
    if skip_reason:
        pytest.skip(skip_reason)

    path = _build_path(rule, ids)
    query = _QUERY_ARGS.get(endpoint)
    if query:
        path = path + ("&" if "?" in path else "?") + urlencode(query)
    if method == "GET":
        response = client.get(path, follow_redirects=False)
    else:
        response = client.post(path, data={}, follow_redirects=False)

    allowed = {200, 202, 301, 302, 403}
    if method == "POST":
        allowed.add(400)
        allowed.update(_POST_ALLOWED_STATUS.get(endpoint, set()))

    assert response.status_code in allowed, (
        f"{endpoint} {method} {path} returned {response.status_code}"
    )


def _test_name(endpoint: str) -> str:
    """Return the required test function name for an endpoint."""
    blueprint, function_name = endpoint.split(".", 1) if "." in endpoint else ("app", endpoint)
    return f"test_smoke_{blueprint}_{function_name}".replace("-", "_")


for _route in ROUTE_SPECS:
    def _make_test(route):
        def _test(smoke_env):
            """Smoke-test one route from the live URL map."""
            _run_smoke(route, smoke_env)

        _test.__name__ = _test_name(str(route["endpoint"]))
        return _test

    globals()[_test_name(str(_route["endpoint"]))] = _make_test(_route)
