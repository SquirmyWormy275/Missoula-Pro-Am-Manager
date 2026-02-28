"""Race-day load test for Missoula Pro Am Manager.

Creates/ensures representative seed data, starts the Flask app locally, and
executes a mixed-role concurrent HTTP test:
- 200 spectator users
- 50 competitor users
- 10 judge users
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _seed_race_day_data() -> int:
    from app import create_app
    from database import db
    from models import Event, EventResult, Team, Tournament, User
    from models.competitor import CollegeCompetitor, ProCompetitor

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
            tournament = Tournament(name="Missoula Pro Am Load Test", year=2026, status="college_active")
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
                individual_points=random.randint(0, 30),
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
                total_earnings=float(random.randint(0, 1500)),
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

        db.session.commit()
        return int(tournament.id)


@dataclass
class Stats:
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    success: int = 0
    total: int = 0
    status_codes: dict[int, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, latency: float, status: int | None) -> None:
        with self.lock:
            self.total += 1
            if latency >= 0:
                self.latencies.append(latency)
            if status is None:
                self.errors += 1
            elif 200 <= status < 400:
                self.success += 1
                self.status_codes[status] = self.status_codes.get(status, 0) + 1
            else:
                self.errors += 1
                self.status_codes[status] = self.status_codes.get(status, 0) + 1


def _worker(base_url: str, endpoints: list[str], end_time: float, stats: Stats, timeout: float) -> None:
    rng = random.Random()
    while time.time() < end_time:
        path = rng.choice(endpoints)
        start = time.perf_counter()
        status = None
        try:
            req = urllib.request.Request(f"{base_url}{path}", headers={"User-Agent": "race-day-load-test/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _ = resp.read(256)
                status = int(resp.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
        except Exception:
            status = None
        latency_ms = (time.perf_counter() - start) * 1000.0
        stats.add(latency_ms, status)
        time.sleep(rng.uniform(0.2, 1.0))


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * p))))
    return ordered[index]


def _run_load_test(base_url: str, tournament_id: int, duration_s: int, timeout_s: float) -> dict:
    spectator_paths = [
        f"/portal/spectator/{tournament_id}",
        f"/portal/spectator/{tournament_id}/college",
        f"/portal/spectator/{tournament_id}/pro",
        f"/api/public/tournaments/{tournament_id}/standings-poll",
    ]
    competitor_paths = ["/portal/competitor-access", "/"]
    judge_paths = ["/auth/login", "/"]

    spectator_stats = Stats()
    competitor_stats = Stats()
    judge_stats = Stats()

    users = []
    end_time = time.time() + duration_s
    for _ in range(200):
        users.append(threading.Thread(target=_worker, args=(base_url, spectator_paths, end_time, spectator_stats, timeout_s)))
    for _ in range(50):
        users.append(threading.Thread(target=_worker, args=(base_url, competitor_paths, end_time, competitor_stats, timeout_s)))
    for _ in range(10):
        users.append(threading.Thread(target=_worker, args=(base_url, judge_paths, end_time, judge_stats, timeout_s)))

    random.shuffle(users)
    start = time.time()
    for thread in users:
        thread.start()
    for thread in users:
        thread.join()
    elapsed = max(0.001, time.time() - start)

    all_latencies = spectator_stats.latencies + competitor_stats.latencies + judge_stats.latencies
    total_requests = spectator_stats.total + competitor_stats.total + judge_stats.total
    total_errors = spectator_stats.errors + competitor_stats.errors + judge_stats.errors
    total_success = spectator_stats.success + competitor_stats.success + judge_stats.success
    status_totals: dict[int, int] = {}
    for role_stats in (spectator_stats, competitor_stats, judge_stats):
        for code, count in role_stats.status_codes.items():
            status_totals[code] = status_totals.get(code, 0) + count

    return {
        "duration_seconds": elapsed,
        "users": {"spectators": 200, "competitors": 50, "judges": 10, "total": 260},
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
        "by_role": {
            "spectators": {
                "requests": spectator_stats.total,
                "errors": spectator_stats.errors,
                "p95_ms": _percentile(spectator_stats.latencies, 0.95),
            },
            "competitors": {
                "requests": competitor_stats.total,
                "errors": competitor_stats.errors,
                "p95_ms": _percentile(competitor_stats.latencies, 0.95),
            },
            "judges": {
                "requests": judge_stats.total,
                "errors": judge_stats.errors,
                "p95_ms": _percentile(judge_stats.latencies, 0.95),
            },
        },
    }


def _start_server(host: str, port: int, server_mode: str, workers: int) -> subprocess.Popen:
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

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return proc


def _wait_for_server(base_url: str, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=2.0) as resp:
                if 200 <= resp.status < 500:
                    return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("Server did not become ready in time.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run race-day mixed-role load test.")
    parser.add_argument("--duration", type=int, default=45, help="Test duration in seconds (default: 45).")
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
        "--output",
        default=str(PROJECT_ROOT / "instance" / "load_test_report.json"),
        help="Path for JSON report output.",
    )
    args = parser.parse_args()

    tournament_id = _seed_race_day_data()
    base_url = f"http://{args.host}:{args.port}"

    server = _start_server(args.host, args.port, args.server, args.workers)
    try:
        _wait_for_server(base_url)
        report = _run_load_test(base_url, tournament_id, args.duration, args.timeout)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()

    passed = (
        report["requests"]["error_rate"] <= args.max_error_rate
        and report["latency_ms"]["p95"] <= args.target_p95_ms
    )
    report["gate"] = {
        "server_mode": args.server,
        "workers": args.workers if args.server == "werkzeug-multiprocess" else 1,
        "target_p95_ms": args.target_p95_ms,
        "max_error_rate": args.max_error_rate,
        "passed": passed,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    print(f"\nReport written to: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
