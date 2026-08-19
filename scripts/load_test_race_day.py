"""Race-day load test for Missoula Pro Am Manager.

Creates/ensures representative seed data, starts the Flask app locally, and
executes a mixed-role concurrent HTTP test with configurable ramp and pacing:
- 200 spectator users
- 50 competitor users
- 10 judge users

The spectator default tracks the application's 30-second standings poll. Use
``--ramp-up 0`` and shorter think times explicitly for a cold-burst stress run.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB_PATH = (PROJECT_ROOT / "instance" / "proam.db").resolve()
JUDGE_USERNAME = "judge_loadtest_1"
JUDGE_PASSWORD = "LoadTest123!"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _release_app(app) -> None:
    """Release DB handles and the SQLite process fence before a child starts."""
    from database import db

    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    finalizer = app.extensions.get("sqlite_process_fence_finalizer")
    if finalizer is not None and finalizer.alive:
        finalizer()


def _seed_race_day_data() -> dict:
    from app import create_app
    from database import db
    from models import Event, EventResult, Heat, Team, Tournament, User
    from models.competitor import CollegeCompetitor, ProCompetitor

    seed_rng = random.Random(2027)
    app = create_app()
    app_context = app.app_context()
    app_context.push()
    try:
        tournament = (
            Tournament.query.filter(
                Tournament.status.in_(["setup", "college_active", "pro_active"])
            )
            .order_by(Tournament.year.desc())
            .first()
        )
        if not tournament:
            tournament = Tournament(
                name="[REHEARSAL] Missoula Pro Am",
                year=2027,
                status="college_active",
            )
            db.session.add(tournament)
            db.session.flush()
        else:
            tournament.status = "college_active"

        teams = tournament.teams.order_by(Team.id).all()
        while len(teams) < 8:
            idx = len(teams) + 1
            team = Team(
                tournament_id=tournament.id,
                team_code=f"LT-{idx}",
                school_name=f"Load Test School {idx}",
                school_abbreviation=f"LTS{idx}",
            )
            db.session.add(team)
            db.session.flush()
            teams.append(team)

        college = tournament.college_competitors.filter_by(status="active").all()
        pro = tournament.pro_competitors.filter_by(status="active").all()

        while len(college) < 25:
            idx = len(college) + 1
            comp = CollegeCompetitor(
                tournament_id=tournament.id,
                team_id=teams[idx % len(teams)].id,
                name=f"College LoadTest {idx}",
                gender="M" if idx % 2 else "F",
                individual_points=seed_rng.randint(0, 30),
                status="active",
            )
            db.session.add(comp)
            db.session.flush()
            college.append(comp)

        while len(pro) < 25:
            idx = len(pro) + 1
            comp = ProCompetitor(
                tournament_id=tournament.id,
                name=f"Pro LoadTest {idx}",
                gender="M" if idx % 2 else "F",
                phone=f"406555{1000 + idx}",
                email=f"pro{idx}@loadtest.local",
                status="active",
                total_earnings=float(seed_rng.randint(0, 1500)),
            )
            db.session.add(comp)
            db.session.flush()
            pro.append(comp)

        judge_usernames = []
        for idx in range(1, 11):
            username = f"judge_loadtest_{idx}"
            user = User.query.filter_by(username=username).first()
            if user is None:
                user = User(
                    username=username,
                    role=User.ROLE_JUDGE,
                    display_name=f"Judge {idx}",
                )
                db.session.add(user)
            else:
                user.role = User.ROLE_JUDGE
            user.set_password(JUDGE_PASSWORD)
            judge_usernames.append(username)

        existing_college_events = (
            Event.query.filter_by(tournament_id=tournament.id, event_type="college", status="completed")
            .order_by(Event.id)
            .all()
        )
        existing_pro_events = (
            Event.query.filter_by(tournament_id=tournament.id, event_type="pro", status="completed")
            .order_by(Event.id)
            .all()
        )

        while len(existing_college_events) < 3:
            idx = len(existing_college_events) + 1
            event = Event(
                tournament_id=tournament.id,
                name=f"College Load Event {idx}",
                event_type="college",
                gender="M" if idx % 2 else "F",
                scoring_type="time",
                scoring_order="lowest_wins",
                status="completed",
            )
            db.session.add(event)
            db.session.flush()
            existing_college_events.append(event)

        while len(existing_pro_events) < 3:
            idx = len(existing_pro_events) + 1
            event = Event(
                tournament_id=tournament.id,
                name=f"Pro Load Event {idx}",
                event_type="pro",
                gender="M" if idx % 2 else "F",
                scoring_type="time",
                scoring_order="lowest_wins",
                status="completed",
            )
            db.session.add(event)
            db.session.flush()
            existing_pro_events.append(event)

        for event in existing_college_events[:3]:
            for i, comp in enumerate(college[:25], start=1):
                result = EventResult.query.filter_by(
                    event_id=event.id, competitor_id=comp.id, competitor_type="college"
                ).first()
                if not result:
                    result = EventResult(
                        event_id=event.id,
                        competitor_id=comp.id,
                        competitor_type="college",
                        competitor_name=comp.name,
                        final_position=i,
                        result_value=15.0 + i / 10.0,
                        result_unit="seconds",
                        points_awarded=max(0, 12 - i),
                        status="completed",
                    )
                    db.session.add(result)

        for event in existing_pro_events[:3]:
            for i, comp in enumerate(pro[:25], start=1):
                result = EventResult.query.filter_by(
                    event_id=event.id, competitor_id=comp.id, competitor_type="pro"
                ).first()
                if not result:
                    result = EventResult(
                        event_id=event.id,
                        competitor_id=comp.id,
                        competitor_type="pro",
                        competitor_name=comp.name,
                        final_position=i,
                        result_value=12.0 + i / 10.0,
                        result_unit="seconds",
                        payout_amount=float(max(0, (26 - i) * 5)),
                        status="completed",
                    )
                    db.session.add(result)

        live_event = Event.query.filter_by(
            tournament_id=tournament.id,
            name="Rehearsal Timed Heat",
        ).first()
        if live_event is None:
            live_event = Event(
                tournament_id=tournament.id,
                name="Rehearsal Timed Heat",
                event_type="pro",
                scoring_type="time",
                scoring_order="lowest_wins",
                stand_type="underhand",
                max_stands=2,
                status="in_progress",
                is_finalized=False,
            )
            db.session.add(live_event)
            db.session.flush()

            for competitor in pro[:4]:
                entered = competitor.get_events_entered()
                if live_event.id not in entered:
                    entered.append(live_event.id)
                    competitor.set_events_entered(entered)

            for heat_number, competitors in enumerate((pro[:2], pro[2:4]), start=1):
                heat = Heat(
                    event_id=live_event.id,
                    heat_number=heat_number,
                    run_number=1,
                    status="pending",
                )
                db.session.add(heat)
                heat.set_roster(
                    "pro",
                    [competitor.id for competitor in competitors],
                    {
                        str(competitor.id): stand
                        for stand, competitor in enumerate(competitors, start=1)
                    },
                )

        db.session.commit()
        seed = {
            "tournament_id": int(tournament.id),
            "live_event_id": int(live_event.id),
            "heat_ids": [
                int(heat.id)
                for heat in live_event.heats.order_by(Heat.heat_number).all()
            ],
            "judge_username": JUDGE_USERNAME,
            "judge_usernames": judge_usernames,
            "judge_password": JUDGE_PASSWORD,
        }
    finally:
        try:
            app_context.pop()
        finally:
            _release_app(app)
    return seed


@dataclass
class Stats:
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    success: int = 0
    total: int = 0
    status_codes: dict[int, int] = field(default_factory=dict)
    error_kinds: dict[str, int] = field(default_factory=dict)
    error_paths: dict[str, int] = field(default_factory=dict)
    path_requests: dict[str, int] = field(default_factory=dict)
    path_errors: dict[str, int] = field(default_factory=dict)
    path_latencies: dict[str, list[float]] = field(default_factory=dict)
    activations: list[float] = field(default_factory=list)
    authentications: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def activate(self, activated_at: float) -> None:
        with self.lock:
            self.activations.append(activated_at)

    def authenticate(self) -> None:
        with self.lock:
            self.authentications += 1

    def add(
        self,
        latency: float,
        status: int | None,
        *,
        error_kind: str | None = None,
        path: str | None = None,
    ) -> None:
        with self.lock:
            self.total += 1
            if latency >= 0:
                self.latencies.append(latency)
            if path:
                self.path_requests[path] = self.path_requests.get(path, 0) + 1
                if latency >= 0:
                    self.path_latencies.setdefault(path, []).append(latency)
            if status is None:
                self.errors += 1
                kind = error_kind or "request_error"
                self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1
                if path:
                    self.error_paths[path] = self.error_paths.get(path, 0) + 1
                    self.path_errors[path] = self.path_errors.get(path, 0) + 1
            elif 200 <= status < 400:
                self.success += 1
                self.status_codes[status] = self.status_codes.get(status, 0) + 1
            else:
                self.errors += 1
                self.status_codes[status] = self.status_codes.get(status, 0) + 1
                kind = error_kind or f"http_{status}"
                self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1
                if path:
                    self.error_paths[path] = self.error_paths.get(path, 0) + 1
                    self.path_errors[path] = self.path_errors.get(path, 0) + 1


def _error_kind(exc: Exception) -> str:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, TimeoutError):
        return "timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(reason, ConnectionResetError):
        return "connection_reset"
    if isinstance(reason, OSError):
        return f"network_{type(reason).__name__.lower()}"
    return type(reason).__name__.lower()


def _drain_http_error(exc: urllib.error.HTTPError) -> None:
    try:
        exc.read()
    except Exception:
        pass


class CrossOriginRedirectError(RuntimeError):
    pass


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.allowed_origin = _url_origin(base_url)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirect_url = urllib.parse.urljoin(req.full_url, newurl)
        if _url_origin(redirect_url) != self.allowed_origin:
            raise CrossOriginRedirectError(
                f"Refused cross-origin redirect to {_url_origin(redirect_url)}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _CsrfTokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        attributes = dict(attrs)
        if attributes.get("name") == "csrf_token" and attributes.get("value"):
            self.token = attributes["value"]


def _consume_measured_response(
    response,
    requested_url: str,
    *,
    authenticated: bool,
) -> tuple[int | None, str | None]:
    """Read the full response and reject lost authenticated sessions."""
    response.read()
    status = int(response.status)
    final_url = urllib.parse.urlsplit(response.geturl())
    expected_url = urllib.parse.urlsplit(requested_url)
    final_target = (
        *_url_origin(response.geturl()),
        final_url.path,
    )
    expected_target = (
        *_url_origin(requested_url),
        expected_url.path,
    )
    if final_target == expected_target:
        return status, None
    if authenticated and final_url.path == "/auth/login":
        return 401, "authentication_lost"
    return None, "unexpected_redirect"


def _worker(
    base_url: str,
    endpoints: list[str],
    stop_event: threading.Event,
    stats: Stats,
    timeout: float,
    start_at: float,
    think_min: float,
    think_max: float,
    login_credentials: tuple[str, str] | None,
    rng_seed: int,
) -> None:
    rng = random.Random(rng_seed)
    start_delay = max(0.0, start_at - time.perf_counter())
    if start_delay and stop_event.wait(start_delay):
        return
    stats.activate(time.perf_counter())
    opener = urllib.request.build_opener(
        _SameOriginRedirectHandler(base_url),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    if login_credentials:
        username, password = login_credentials
        login_url = f"{base_url}/auth/login"
        csrf_token = None
        csrf_start = time.perf_counter()
        csrf_status = None
        csrf_error = None
        try:
            csrf_request = urllib.request.Request(
                login_url,
                headers={"User-Agent": "race-day-load-test/1.0"},
            )
            with opener.open(csrf_request, timeout=timeout) as response:
                parser = _CsrfTokenParser()
                parser.feed(response.read().decode("utf-8", errors="replace"))
                csrf_status = int(response.status)
                csrf_token = parser.token
                if not csrf_token:
                    csrf_status = 401
                    csrf_error = "csrf_token_missing"
        except urllib.error.HTTPError as exc:
            _drain_http_error(exc)
            csrf_status = int(exc.code)
        except Exception as exc:
            csrf_error = _error_kind(exc)
        stats.add(
            (time.perf_counter() - csrf_start) * 1000.0,
            csrf_status,
            error_kind=csrf_error,
            path="GET /auth/login",
        )
        if csrf_status is None or not 200 <= csrf_status < 400:
            return

        login_start = time.perf_counter()
        login_status = None
        login_error = None
        try:
            login_data = urllib.parse.urlencode({
                "username": username,
                "password": password,
                "csrf_token": csrf_token,
            }).encode("ascii")
            login_request = urllib.request.Request(
                login_url,
                data=login_data,
                headers={"User-Agent": "race-day-load-test/1.0"},
            )
            with opener.open(login_request, timeout=timeout) as response:
                response.read()
                login_status = int(response.status)
                if urllib.parse.urlsplit(response.geturl()).path == "/auth/login":
                    login_status = 401
                    login_error = "authentication_failed"
        except urllib.error.HTTPError as exc:
            _drain_http_error(exc)
            login_status = int(exc.code)
        except Exception as exc:
            login_error = _error_kind(exc)
        stats.add(
            (time.perf_counter() - login_start) * 1000.0,
            login_status,
            error_kind=login_error,
            path="POST /auth/login",
        )
        if login_status is None or not 200 <= login_status < 400:
            return
        stats.authenticate()

    while not stop_event.is_set():
        path = rng.choice(endpoints)
        start = time.perf_counter()
        status = None
        error_kind = None
        try:
            request_url = f"{base_url}{path}"
            req = urllib.request.Request(request_url, headers={"User-Agent": "race-day-load-test/1.0"})
            with opener.open(req, timeout=timeout) as resp:
                status, error_kind = _consume_measured_response(
                    resp,
                    request_url,
                    authenticated=login_credentials is not None,
                )
        except urllib.error.HTTPError as exc:
            _drain_http_error(exc)
            status = int(exc.code)
        except Exception as exc:
            error_kind = _error_kind(exc)
        latency_ms = (time.perf_counter() - start) * 1000.0
        stats.add(latency_ms, status, error_kind=error_kind, path=f"GET {path}")
        stop_event.wait(rng.uniform(think_min, think_max))


def _guarded_worker(
    worker_errors: list[str],
    worker_errors_lock: threading.Lock,
    ready_barrier: threading.Barrier,
    launch_event: threading.Event,
    launch_clock: list[float],
    start_delay: float,
    base_url: str,
    endpoints: list[str],
    stop_event: threading.Event,
    stats: Stats,
    timeout: float,
    think_min: float,
    think_max: float,
    login_credentials: tuple[str, str] | None,
    rng_seed: int,
) -> None:
    try:
        ready_barrier.wait(timeout=60)
        launch_event.wait()
        _worker(
            base_url,
            endpoints,
            stop_event,
            stats,
            timeout,
            launch_clock[0] + start_delay,
            think_min,
            think_max,
            login_credentials,
            rng_seed,
        )
    except Exception as exc:
        with worker_errors_lock:
            worker_errors.append(f"{type(exc).__name__}: {exc}")
        stop_event.set()


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * p))))
    return ordered[index]


def _passes_gate(
    report: dict,
    *,
    target_p95_ms: float,
    target_role_p95_ms: float | None = None,
    max_error_rate: float,
    max_server_errors: int = 0,
) -> bool:
    role_results = _role_gate_results(
        report,
        target_role_p95_ms=(
            target_p95_ms
            if target_role_p95_ms is None
            else target_role_p95_ms
        ),
        max_error_rate=max_error_rate,
    )
    server_errors = sum(
        count
        for code, count in report.get("status_codes", {}).items()
        if int(code) >= 500
    )
    return (
        report["requests"]["error_rate"] <= max_error_rate
        and report["latency_ms"]["p95"] <= target_p95_ms
        and server_errors <= max_server_errors
        and all(result["passed"] for result in role_results.values())
    )


def _role_gate_results(
    report: dict,
    *,
    target_role_p95_ms: float,
    max_error_rate: float,
) -> dict[str, dict]:
    results = {}
    users = report.get("users", {})
    for role, summary in report.get("by_role", {}).items():
        user_count = int(users.get(role, 0))
        if user_count <= 0:
            continue
        minimum_requests = user_count * (3 if role == "judges" else 1)
        request_count = int(summary.get("requests", 0))
        error_count = int(summary.get("errors", 0))
        activation_count = int(summary.get("activations", 0))
        authentication_count = int(summary.get("authentications", 0))
        error_rate = error_count / request_count if request_count else 1.0
        p95_ms = float(summary.get("p95_ms", 0.0))
        endpoint_results = {}
        for path in report.get("load_shape", {}).get(
            "expected_paths",
            {},
        ).get(role, []):
            path_summary = summary.get("by_path", {}).get(path, {})
            path_requests = int(path_summary.get("requests", 0))
            path_errors = int(path_summary.get("errors", 0))
            path_error_rate = (
                path_errors / path_requests if path_requests else 1.0
            )
            path_p95_ms = float(path_summary.get("p95_ms", 0.0))
            minimum_path_requests = (
                user_count
                if role == "judges" and path in {
                    "GET /auth/login",
                    "POST /auth/login",
                }
                else 1
            )
            endpoint_results[path] = {
                "requests": path_requests,
                "minimum_requests": minimum_path_requests,
                "error_rate": path_error_rate,
                "p95_ms": path_p95_ms,
                "passed": (
                    path_requests >= minimum_path_requests
                    and path_error_rate <= max_error_rate
                    and path_p95_ms <= target_role_p95_ms
                ),
            }
        results[role] = {
            "requests": request_count,
            "minimum_requests": minimum_requests,
            "configured_users": user_count,
            "activations": activation_count,
            "authentications": authentication_count,
            "error_rate": error_rate,
            "p95_ms": p95_ms,
            "endpoints": endpoint_results,
            "passed": (
                request_count >= minimum_requests
                and activation_count == user_count
                and (
                    role != "judges"
                    or authentication_count == user_count
                )
                and error_rate <= max_error_rate
                and p95_ms <= target_role_p95_ms
                and all(
                    result["passed"]
                    for result in endpoint_results.values()
                )
            ),
        }
    return results


def _run_load_test(
    base_url: str,
    tournament_id: int,
    duration_s: int,
    timeout_s: float,
    *,
    spectator_users: int = 200,
    competitor_users: int = 50,
    judge_users: int = 10,
    judge_heat_ids: list[int] | None = None,
    ramp_up_s: float = 15.0,
    spectator_think: tuple[float, float] = (25.0, 35.0),
    competitor_think: tuple[float, float] = (20.0, 60.0),
    judge_think: tuple[float, float] = (3.0, 8.0),
    judge_credentials: list[tuple[str, str]] | None = None,
    seed: int = 2027,
) -> dict:
    user_counts = {
        "spectators": spectator_users,
        "competitors": competitor_users,
        "judges": judge_users,
    }
    if duration_s < 0 or ramp_up_s < 0 or any(count < 0 for count in user_counts.values()):
        raise ValueError("Duration, ramp-up, and virtual-user counts must be non-negative.")
    think_times = {
        "spectators": spectator_think,
        "competitors": competitor_think,
        "judges": judge_think,
    }
    for role, (minimum, maximum) in think_times.items():
        if minimum < 0 or maximum < minimum:
            raise ValueError(f"Invalid think-time range for {role}: {minimum}, {maximum}")

    spectator_paths = [
        f"/portal/spectator/{tournament_id}",
        f"/portal/spectator/{tournament_id}/college",
        f"/portal/spectator/{tournament_id}/pro",
        f"/api/public/tournaments/{tournament_id}/standings-poll",
    ]
    competitor_paths = ["/portal/competitor-access", "/"]
    judge_paths = [f"/scoring/{tournament_id}/offline-ops"]
    judge_paths.extend(
        f"/scoring/{tournament_id}/heat/{heat_id}/enter"
        for heat_id in (judge_heat_ids or [])
    )

    spectator_stats = Stats()
    competitor_stats = Stats()
    judge_stats = Stats()

    stop_event = threading.Event()
    user_specs = []
    judge_accounts = (
        [(JUDGE_USERNAME, JUDGE_PASSWORD)]
        if judge_credentials is None
        else judge_credentials
    )
    if judge_users and not judge_accounts:
        raise ValueError("At least one judge account is required for judge users.")
    role_specs = (
        (spectator_users, spectator_paths, spectator_stats, spectator_think, None),
        (competitor_users, competitor_paths, competitor_stats, competitor_think, None),
    )
    for count, paths, stats, (think_min, think_max), credentials in role_specs:
        for _ in range(count):
            user_specs.append((paths, stats, think_min, think_max, credentials))
    for index in range(judge_users):
        user_specs.append((
            judge_paths,
            judge_stats,
            judge_think[0],
            judge_think[1],
            judge_accounts[index % len(judge_accounts)],
        ))

    workload_rng = random.Random(seed)
    workload_rng.shuffle(user_specs)
    users = []
    worker_errors = []
    worker_errors_lock = threading.Lock()
    ready_barrier = threading.Barrier(len(user_specs) + 1)
    launch_event = threading.Event()
    launch_clock = [0.0]
    for index, (paths, stats, think_min, think_max, credentials) in enumerate(user_specs):
        start_delay = ramp_up_s * index / max(1, len(user_specs) - 1)
        users.append(threading.Thread(
                target=_guarded_worker,
                args=(
                    worker_errors,
                    worker_errors_lock,
                    ready_barrier,
                    launch_event,
                    launch_clock,
                    start_delay,
                    base_url,
                    paths,
                    stop_event,
                    stats,
                    timeout_s,
                    think_min,
                    think_max,
                    credentials,
                    workload_rng.randrange(2**63),
                ),
                daemon=True,
                name=f"race-day-user-{index + 1}",
            ))

    started_users = []
    start = None
    ramp_finished = None
    try:
        for thread in users:
            thread.start()
            started_users.append(thread)
        ready_barrier.wait(timeout=60)
        start = time.perf_counter()
        launch_clock[0] = start
        launch_event.set()
        stop_event.wait(max(0.0, start + ramp_up_s - time.perf_counter()))
        ramp_finished = time.perf_counter()
        stop_event.wait(max(0.0, duration_s))
    finally:
        stop_event.set()
        launch_event.set()
        if start is None:
            try:
                ready_barrier.abort()
            except threading.BrokenBarrierError:
                pass
        join_deadline = (
            time.perf_counter()
            + timeout_s
            + max(maximum for _, maximum in think_times.values())
            + 5.0
        )
        for thread in started_users:
            thread.join(timeout=max(0.0, join_deadline - time.perf_counter()))
        stuck = [thread.name for thread in started_users if thread.is_alive()]
        if stuck:
            raise RuntimeError(
                f"{len(stuck)} load workers did not stop before the shutdown deadline."
            )
    if worker_errors:
        raise RuntimeError(
            "Load worker failed unexpectedly: " + "; ".join(worker_errors[:3])
        )
    assert start is not None and ramp_finished is not None
    elapsed = max(0.001, time.perf_counter() - start)

    all_latencies = spectator_stats.latencies + competitor_stats.latencies + judge_stats.latencies
    total_requests = spectator_stats.total + competitor_stats.total + judge_stats.total
    total_errors = spectator_stats.errors + competitor_stats.errors + judge_stats.errors
    total_success = spectator_stats.success + competitor_stats.success + judge_stats.success
    status_totals: dict[int, int] = {}
    error_totals: dict[str, int] = {}
    error_path_totals: dict[str, int] = {}
    for role_stats in (spectator_stats, competitor_stats, judge_stats):
        for code, count in role_stats.status_codes.items():
            status_totals[code] = status_totals.get(code, 0) + count
        for kind, count in role_stats.error_kinds.items():
            error_totals[kind] = error_totals.get(kind, 0) + count
        for path, count in role_stats.error_paths.items():
            error_path_totals[path] = error_path_totals.get(path, 0) + count

    def role_summary(stats: Stats) -> dict:
        path_summaries = {}
        for path, requests in sorted(stats.path_requests.items()):
            errors = stats.path_errors.get(path, 0)
            path_summaries[path] = {
                "requests": requests,
                "errors": errors,
                "error_rate": errors / requests if requests else 1.0,
                "p95_ms": _percentile(stats.path_latencies.get(path, []), 0.95),
            }
        return {
            "requests": stats.total,
            "errors": stats.errors,
            "activations": len(stats.activations),
            "authentications": stats.authentications,
            "error_rate": (stats.errors / stats.total) if stats.total else 1.0,
            "p95_ms": _percentile(stats.latencies, 0.95),
            "error_kinds": dict(sorted(stats.error_kinds.items())),
            "error_paths": dict(sorted(stats.error_paths.items())),
            "by_path": path_summaries,
        }

    activation_times = (
        spectator_stats.activations
        + competitor_stats.activations
        + judge_stats.activations
    )
    actual_ramp = (
        max(activation_times) - min(activation_times)
        if len(activation_times) > 1
        else 0.0
    )

    return {
        "duration_seconds": elapsed,
        "users": {
            **user_counts,
            "total": sum(user_counts.values()),
        },
        "load_shape": {
            "configured_steady_state_seconds": duration_s,
            "configured_ramp_up_seconds": ramp_up_s,
            "actual_ramp_up_seconds": actual_ramp,
            "ramp_wait_seconds": ramp_finished - start,
            "seed": seed,
            "expected_paths": {
                "spectators": [f"GET {path}" for path in spectator_paths],
                "competitors": [f"GET {path}" for path in competitor_paths],
                "judges": [
                    "GET /auth/login",
                    "POST /auth/login",
                    *[f"GET {path}" for path in judge_paths],
                ],
            },
            "think_time_seconds": {
                role: list(values)
                for role, values in think_times.items()
            },
        },
        "requests": {
            "total": total_requests,
            "success": total_success,
            "errors": total_errors,
            "rps": total_requests / elapsed,
            "error_rate": (total_errors / total_requests) if total_requests else 1.0,
        },
        "latency_ms": {
            "mean": statistics.fmean(all_latencies) if all_latencies else 0.0,
            "p50": _percentile(all_latencies, 0.50),
            "p95": _percentile(all_latencies, 0.95),
            "p99": _percentile(all_latencies, 0.99),
            "max": max(all_latencies) if all_latencies else 0.0,
        },
        "status_codes": {
            str(code): count
            for code, count in sorted(status_totals.items())
        },
        "error_kinds": dict(sorted(error_totals.items())),
        "error_paths": dict(sorted(error_path_totals.items())),
        "by_role": {
            "spectators": role_summary(spectator_stats),
            "competitors": role_summary(competitor_stats),
            "judges": role_summary(judge_stats),
        },
    }


def _start_server(
    host: str,
    port: int,
    server_mode: str,
    workers: int,
) -> tuple[subprocess.Popen, Path]:
    if server_mode == "flask-threaded":
        cmd = [
            sys.executable,
            "-m",
            "flask",
            "--app",
            "app:create_app",
            "run",
            "--with-threads",
            "--no-reload",
            "--no-debugger",
            "--host",
            host,
            "--port",
            str(port),
        ]
    elif server_mode == "werkzeug-multiprocess":
        cmd = [
            sys.executable,
            "-c",
            (
                "from app import create_app; "
                "from werkzeug.serving import run_simple; "
                "app=create_app(); "
                f"run_simple('{host}', {port}, app, use_reloader=False, use_debugger=False, threaded=False, processes={max(1, workers)})"
            ),
        ]
    else:
        raise ValueError(f"Unsupported server mode: {server_mode}")

    log_handle = tempfile.NamedTemporaryFile(
        prefix="proam-race-day-server-",
        suffix=".log",
        delete=False,
    )
    log_path = Path(log_handle.name)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception:
        log_handle.close()
        try:
            log_path.unlink()
        except FileNotFoundError:
            pass
        raise
    log_handle.close()
    return proc, log_path


def _server_log_tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "(server log unavailable)"


def _wait_for_server(
    base_url: str,
    *,
    process: subprocess.Popen,
    log_path: Path,
    timeout_s: float = 20.0,
) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "Server exited before becoming ready.\n" + _server_log_tail(log_path)
            )
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=2.0) as resp:
                if 200 <= resp.status < 500:
                    return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(
        "Server did not become ready in time.\n" + _server_log_tail(log_path)
    )


def _resolve_synthetic_database(raw_path: str | None) -> tuple[Path, bool]:
    """Return a non-production SQLite path and whether this run created it."""
    if raw_path:
        path = Path(raw_path).expanduser().resolve()
        if path == PRODUCTION_DB_PATH:
            raise ValueError("The race-day load test refuses instance/proam.db.")
        if path.exists():
            raise FileExistsError(
                f"Synthetic database already exists; refusing to overwrite: {path}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, False

    handle, generated = tempfile.mkstemp(prefix="proam-race-day-rehearsal-", suffix=".db")
    os.close(handle)
    return Path(generated).resolve(), True


def _report_path_aliases_database(
    output_path: Path,
    database_path: Path,
) -> bool:
    protected_paths = (PRODUCTION_DB_PATH, database_path.resolve())
    for protected_path in protected_paths:
        if output_path.resolve() == protected_path:
            return True
        if output_path.exists() and protected_path.exists():
            try:
                if os.path.samefile(output_path, protected_path):
                    return True
            except OSError:
                pass
    return False


def _resolve_report_output(
    raw_path: str,
    *,
    database_path: Path,
    overwrite: bool,
) -> Path:
    """Resolve a report target without allowing database aliases."""
    output_path = Path(os.path.abspath(Path(raw_path).expanduser()))
    if _report_path_aliases_database(output_path, database_path):
        raise ValueError("The report output path cannot be a database path.")
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Report output already exists; refusing to overwrite: {output_path}"
        )
    return output_path


def _write_report(
    path: Path,
    report: dict,
    *,
    overwrite: bool,
    database_path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _report_path_aliases_database(path, database_path):
        raise ValueError("The report output path cannot be a database path.")

    temp_handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temp_path = Path(temp_handle.name)
    try:
        handle = temp_handle
        json.dump(report, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        if _report_path_aliases_database(path, database_path):
            raise ValueError("The report output path cannot be a database path.")
        if overwrite:
            os.replace(temp_path, path)
        else:
            os.link(temp_path, path)
    finally:
        if not temp_handle.closed:
            temp_handle.close()
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _prepare_synthetic_database(path: Path) -> None:
    """Migrate an isolated SQLite database before importing app state."""
    for name in (
        "PRODUCTION",
        "RAILWAY_ENVIRONMENT",
        "STRATHMARK_SUPABASE_URL",
        "STRATHMARK_SUPABASE_KEY",
    ):
        os.environ.pop(name, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{path.as_posix()}"
    os.environ["FLASK_ENV"] = "testing"
    os.environ["TESTING"] = "1"
    os.environ["SECRET_KEY"] = "race-day-load-rehearsal-only"

    from flask_migrate import upgrade

    from app import create_app
    from database import db

    app = create_app()
    try:
        with app.app_context():
            db.engine.dispose()
            upgrade(directory=str(PROJECT_ROOT / "migrations"))
            db.engine.dispose()
    finally:
        _release_app(app)


def _remove_synthetic_database(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run race-day mixed-role load test.")
    parser.add_argument("--duration", type=int, default=45, help="Test duration in seconds (default: 45).")
    parser.add_argument(
        "--ramp-up",
        type=float,
        default=15.0,
        help="Seconds over which virtual users start (default: 15).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host bind/target (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=5050, help="Port bind/target (default: 5050).")
    parser.add_argument("--timeout", type=float, default=6.0, help="Per-request timeout seconds (default: 6).")
    parser.add_argument(
        "--server",
        default="flask-threaded",
        choices=["flask-threaded", "werkzeug-multiprocess"],
        help="Server mode for test run.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Worker process count for multiprocess mode (default: 4).",
    )
    parser.add_argument("--target-p95-ms", type=float, default=800.0, help="Pass/fail target for p95 latency.")
    parser.add_argument(
        "--target-role-p95-ms",
        type=float,
        default=800.0,
        help="Pass/fail p95 target applied independently to each active role.",
    )
    parser.add_argument("--max-error-rate", type=float, default=0.005, help="Pass/fail max error rate.")
    parser.add_argument(
        "--max-server-errors",
        type=int,
        default=0,
        help="Pass/fail maximum HTTP 5xx responses (default: 0).",
    )
    parser.add_argument("--spectator-users", type=int, default=200)
    parser.add_argument("--competitor-users", type=int, default=50)
    parser.add_argument("--judge-users", type=int, default=10)
    parser.add_argument("--spectator-think-min", type=float, default=25.0)
    parser.add_argument("--spectator-think-max", type=float, default=35.0)
    parser.add_argument("--competitor-think-min", type=float, default=20.0)
    parser.add_argument("--competitor-think-max", type=float, default=60.0)
    parser.add_argument("--judge-think-min", type=float, default=3.0)
    parser.add_argument("--judge-think-max", type=float, default=8.0)
    parser.add_argument(
        "--seed",
        type=int,
        default=2027,
        help="Deterministic workload ordering and pacing seed (default: 2027).",
    )
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "instance" / "load_test_report.json"),
        help="Path for JSON report output.",
    )
    parser.add_argument(
        "--overwrite-output",
        action="store_true",
        help="Allow replacing an existing non-database report file.",
    )
    parser.add_argument(
        "--database",
        help=(
            "New synthetic SQLite database path. Existing files and "
            "instance/proam.db are always refused."
        ),
    )
    parser.add_argument(
        "--keep-database",
        action="store_true",
        help="Retain an automatically created synthetic database after the run.",
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Migrate and seed the browser rehearsal database without running load.",
    )
    args = parser.parse_args()

    database_path, generated = _resolve_synthetic_database(args.database)
    retain_database = bool(args.database or args.keep_database or args.seed_only)
    try:
        output_path = None
        if not args.seed_only:
            output_path = _resolve_report_output(
                args.output,
                database_path=database_path,
                overwrite=args.overwrite_output,
            )
        _prepare_synthetic_database(database_path)
        seed = _seed_race_day_data()

        if args.seed_only:
            print(json.dumps({
                "database": str(database_path),
                "synthetic": True,
                **seed,
            }, indent=2))
            return 0

        base_url = f"http://{args.host}:{args.port}"
        server, server_log = _start_server(
            args.host,
            args.port,
            args.server,
            args.workers,
        )
        server_log_tail = ""
        try:
            _wait_for_server(
                base_url,
                process=server,
                log_path=server_log,
            )
            report = _run_load_test(
                base_url,
                seed["tournament_id"],
                args.duration,
                args.timeout,
                spectator_users=args.spectator_users,
                competitor_users=args.competitor_users,
                judge_users=args.judge_users,
                judge_heat_ids=seed["heat_ids"],
                ramp_up_s=args.ramp_up,
                spectator_think=(
                    args.spectator_think_min,
                    args.spectator_think_max,
                ),
                competitor_think=(
                    args.competitor_think_min,
                    args.competitor_think_max,
                ),
                judge_think=(args.judge_think_min, args.judge_think_max),
                judge_credentials=[
                    (username, seed["judge_password"])
                    for username in seed["judge_usernames"]
                ],
                seed=args.seed,
            )
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            server_log_tail = _server_log_tail(server_log, limit=16000)
            try:
                server_log.unlink()
            except FileNotFoundError:
                pass

        passed = _passes_gate(
            report,
            target_p95_ms=args.target_p95_ms,
            target_role_p95_ms=args.target_role_p95_ms,
            max_error_rate=args.max_error_rate,
            max_server_errors=args.max_server_errors,
        )
        report["gate"] = {
            "server_mode": args.server,
            "workers": args.workers if args.server == "werkzeug-multiprocess" else 1,
            "target_p95_ms": args.target_p95_ms,
            "target_role_p95_ms": args.target_role_p95_ms,
            "max_error_rate": args.max_error_rate,
            "max_server_errors": args.max_server_errors,
            "role_results": _role_gate_results(
                report,
                target_role_p95_ms=args.target_role_p95_ms,
                max_error_rate=args.max_error_rate,
            ),
            "passed": passed,
        }
        report["fixture"] = {
            "synthetic": True,
            "tournament_id": seed["tournament_id"],
            "live_event_id": seed["live_event_id"],
            "heat_count": len(seed["heat_ids"]),
        }
        if report["requests"]["errors"]:
            report["diagnostics"] = {"server_log_tail": server_log_tail}

        assert output_path is not None
        _write_report(
            output_path,
            report,
            overwrite=args.overwrite_output,
            database_path=database_path,
        )

        print(json.dumps(report, indent=2))
        print(f"\nReport written to: {output_path}")

        return 0 if passed else 1
    finally:
        if generated and not retain_database:
            _remove_synthetic_database(database_path)


if __name__ == "__main__":
    raise SystemExit(main())
