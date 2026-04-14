"""QA route smoke tests over the full Flask URL map."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

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
SOURCE_DB = PROJECT_ROOT / "instance" / "proam.db"
TMP_ROOT = PROJECT_ROOT / ".qa_tmp"


@pytest.fixture()
def smoke_env(monkeypatch):
    """Return a fresh app/client pair backed by a copied real database."""
    if not SOURCE_DB.exists():
        pytest.skip(
            f"route-smoke fixture requires a local {SOURCE_DB.name} with real data; "
            "not available in CI"
        )
    TMP_ROOT.mkdir(exist_ok=True)
    temp_dir = TMP_ROOT / f"route-smoke-{uuid.uuid4().hex}"
    temp_dir.mkdir()
    db_copy = temp_dir / "proam-copy.db"
    shutil.copy2(SOURCE_DB, db_copy)

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_copy}")
    monkeypatch.setenv("SECRET_KEY", "route-smoke-secret")
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("TESTING", "1")

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with app.app_context():
        from models import Event, Heat, Team, Tournament, User
        from models.competitor import CollegeCompetitor, ProCompetitor

        first_tournament = Tournament.query.order_by(Tournament.id).first()
        first_event = Event.query.order_by(Event.id).first()
        first_heat = Heat.query.order_by(Heat.id).first()
        first_user = User.query.order_by(User.id).first()
        first_team = Team.query.order_by(Team.id).first()
        first_college = CollegeCompetitor.query.order_by(CollegeCompetitor.id).first()
        first_pro = ProCompetitor.query.order_by(ProCompetitor.id).first()
        birling_event = Event.query.filter_by(stand_type="birling").order_by(Event.id).first()

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
    competitor_id = ids["competitor_id"]
    competitor_type = ids["competitor_type"]

    if "/birling" in rule and ids["birling_event_id"] is not None:
        event_id = ids["birling_event_id"]
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
        "<int:tournament_id>": str(ids["tournament_id"]),
        "<int:tid>": str(ids["tid"]),
        "<int:event_id>": str(event_id),
        "<int:heat_id>": str(ids["heat_id"]),
        "<int:user_id>": str(ids["user_id"]),
        "<int:team_id>": str(ids["team_id"]) if ids["team_id"] is not None else "",
        "<int:competitor_id>": str(competitor_id) if competitor_id is not None else "",
        "<int:flight_id>": str(ids["flight_id"]) if ids["flight_id"] is not None else "",
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
    return None


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
    if method == "GET":
        response = client.get(path, follow_redirects=False)
    else:
        response = client.post(path, data={}, follow_redirects=False)

    allowed = {200, 202, 301, 302, 403}
    if method == "POST":
        allowed.add(400)

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
