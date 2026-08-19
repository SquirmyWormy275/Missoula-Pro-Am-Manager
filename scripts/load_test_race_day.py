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
    with app.app_context():
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

        judges = User.query.filter_by(role=User.ROLE_JUDGE).count()
        while judges < 10:
            idx = judges + 1
            username = f"judge_loadtest_{idx}"
            if User.query.filter_by(username=username).first():
                judges += 1
                continue
            user = User(username=username, role=User.ROLE_JUDGE, display_name=f"Judge {idx}")
            user.set_password("LoadTest123!")
            db.session.add(user)
            judges += 1

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
            "judge_username": "judge_loadtest_1",
        }
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
    lock: threading.Lock = field(default_factory=threading.Lock)

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
            if status is None:
                self.errors += 1
                kind = error_kind or "request_error"
                self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1
                if path:
                    self.error_paths[path] = self.error_paths.get(path, 0) + 1
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


def _worker(
    base_url: str,
    endpoints: list[str],
    stop_event: threading.Event,
    stats: Stats,
    timeout: float,
    start_delay: float,
    think_min: float,
    think_max: float,
    login_credentials: tuple[str, str] | None,
) -> None:
    rng = random.Random()
    if start_delay and stop_event.wait(start_delay):
        return
    opener = urllib.request.build_opener(
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
                _ = response.read(256)
                login_status = int(response.status)
                if "/auth/login" in response.geturl():
                    login_status = 401
                    login_error = "authentication_failed"
        except urllib.error.HTTPError as exc:
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

    while not stop_event.is_set():
        path = rng.choice(endpoints)
        start = time.perf_counter()
        status = None
        error_kind = None
        try:
            req = urllib.request.Request(f"{base_url}{path}", headers={"User-Agent": "race-day-load-test/1.0"})
            with opener.open(req, timeout=timeout) as resp:
                _ = resp.read(256)
                status = int(resp.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
        except Exception as exc:
            error_kind = _error_kind(exc)
        latency_ms = (time.perf_counter() - start) * 1000.0
        stats.add(latency_ms, status, error_kind=error_kind, path=f"GET {path}")
        stop_event.wait(rng.uniform(think_min, think_max))


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
    max_error_rate: float,
    max_server_errors: int = 0,
) -> bool:
    server_errors = sum(
        count
        for code, count in report.get("status_codes", {}).items()
        if int(code) >= 500
    )
    return (
        report["requests"]["error_rate"] <= max_error_rate
        and report["latency_ms"]["p95"] <= target_p95_ms
        and server_errors <= max_server_errors
    )


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
    role_specs = (
        (spectator_users, spectator_paths, spectator_stats, spectator_think, None),
        (competitor_users, competitor_paths, competitor_stats, competitor_think, None),
        (
            judge_users,
            judge_paths,
            judge_stats,
            judge_think,
            ("judge_loadtest_1", "LoadTest123!"),
        ),
    )
    for count, paths, stats, (think_min, think_max), credentials in role_specs:
        for _ in range(count):
            user_specs.append((paths, stats, think_min, think_max, credentials))

    random.shuffle(user_specs)
    users = []
    for index, (paths, stats, think_min, think_max, credentials) in enumerate(user_specs):
        start_delay = ramp_up_s * index / max(1, len(user_specs) - 1)
        users.append(threading.Thread(
                target=_worker,
                args=(
                    base_url,
                    paths,
                    stop_event,
                    stats,
                    timeout_s,
                    start_delay,
                    think_min,
                    think_max,
                    credentials,
                ),
            ))

    start = time.time()
    for thread in users:
        thread.start()
    remaining_ramp = max(0.0, ramp_up_s - (time.time() - start))
    if remaining_ramp:
        time.sleep(remaining_ramp)
    ramp_finished = time.time()
    if duration_s:
        time.sleep(duration_s)
    stop_event.set()
    for thread in users:
        thread.join()
    elapsed = max(0.001, time.time() - start)

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
        return {
            "requests": stats.total,
            "errors": stats.errors,
            "p95_ms": _percentile(stats.latencies, 0.95),
            "error_kinds": dict(sorted(stats.error_kinds.items())),
            "error_paths": dict(sorted(stats.error_paths.items())),
        }

    return {
        "duration_seconds": elapsed,
        "users": {
            **user_counts,
            "total": sum(user_counts.values()),
        },
        "load_shape": {
            "configured_steady_state_seconds": duration_s,
            "configured_ramp_up_seconds": ramp_up_s,
            "actual_ramp_up_seconds": ramp_finished - start,
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
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    finally:
        log_handle.close()
    return proc, Path(log_handle.name)


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
    os.environ["WTF_CSRF_ENABLED"] = "False"

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
        "--output",
        default=str(PROJECT_ROOT / "instance" / "load_test_report.json"),
        help="Path for JSON report output.",
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
        _prepare_synthetic_database(database_path)
        seed = _seed_race_day_data()

        if args.seed_only:
            print(json.dumps({
                "database": str(database_path),
                "synthetic": True,
                "judge_password": "LoadTest123!",
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
            max_error_rate=args.max_error_rate,
            max_server_errors=args.max_server_errors,
        )
        report["gate"] = {
            "server_mode": args.server,
            "workers": args.workers if args.server == "werkzeug-multiprocess" else 1,
            "target_p95_ms": args.target_p95_ms,
            "max_error_rate": args.max_error_rate,
            "max_server_errors": args.max_server_errors,
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

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        print(json.dumps(report, indent=2))
        print(f"\nReport written to: {output_path}")

        return 0 if passed else 1
    finally:
        if generated and not retain_database:
            _remove_synthetic_database(database_path)


if __name__ == "__main__":
    raise SystemExit(main())
