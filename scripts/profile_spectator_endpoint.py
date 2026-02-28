"""Profile spectator endpoints and deep-profile the slowest one.

Usage:
    python scripts/profile_spectator_endpoint.py
"""

from __future__ import annotations

import cProfile
import io
import pstats
import statistics
import time
from collections import Counter
from pathlib import Path
import sys

from sqlalchemy import event


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * p))))
    return ordered[idx]


def compact_sql(sql: str) -> str:
    one_line = " ".join(sql.strip().split())
    return (one_line[:140] + "...") if len(one_line) > 140 else one_line


def main() -> int:
    from app import create_app
    from database import db
    from models import Tournament

    app = create_app()
    with app.app_context():
        tournament = (
            Tournament.query.filter(Tournament.status.in_(["setup", "college_active", "pro_active"]))
            .order_by(Tournament.year.desc())
            .first()
        )
        if not tournament:
            raise RuntimeError("No active/setup tournament found.")

        tournament_id = tournament.id
        endpoints = [
            f"/portal/spectator/{tournament_id}",
            f"/portal/spectator/{tournament_id}/college",
            f"/portal/spectator/{tournament_id}/pro",
            f"/portal/spectator/{tournament_id}/relay",
            f"/api/public/tournaments/{tournament_id}/standings-poll",
        ]

        timings: dict[str, list[float]] = {ep: [] for ep in endpoints}
        iterations = 40

        with app.test_client() as client:
            for ep in endpoints:
                for _ in range(5):
                    resp = client.get(ep)
                    if resp.status_code >= 400:
                        raise RuntimeError(f"Warm-up failed for {ep}: {resp.status_code}")

            for ep in endpoints:
                for _ in range(iterations):
                    start = time.perf_counter()
                    resp = client.get(ep)
                    duration_ms = (time.perf_counter() - start) * 1000.0
                    if resp.status_code >= 400:
                        raise RuntimeError(f"Request failed for {ep}: {resp.status_code}")
                    timings[ep].append(duration_ms)

        summary = []
        for ep, vals in timings.items():
            summary.append(
                {
                    "endpoint": ep,
                    "mean_ms": statistics.fmean(vals),
                    "p95_ms": percentile(vals, 0.95),
                    "max_ms": max(vals),
                }
            )
        summary.sort(key=lambda row: row["mean_ms"], reverse=True)
        slowest = summary[0]["endpoint"]

        sql_count = 0
        sql_total_ms = 0.0
        sql_samples: Counter[str] = Counter()
        start_stack: list[float] = []

        def before_cursor_execute(conn, cursor, statement, params, context, executemany):
            del conn, cursor, params, context, executemany
            start_stack.append(time.perf_counter())
            sql_samples[compact_sql(statement)] += 1

        def after_cursor_execute(conn, cursor, statement, params, context, executemany):
            del conn, cursor, statement, params, context, executemany
            nonlocal sql_count, sql_total_ms
            sql_count += 1
            started = start_stack.pop() if start_stack else time.perf_counter()
            sql_total_ms += (time.perf_counter() - started) * 1000.0

        profile = cProfile.Profile()
        with app.test_client() as client:
            event.listen(db.engine, "before_cursor_execute", before_cursor_execute)
            event.listen(db.engine, "after_cursor_execute", after_cursor_execute)
            try:
                profile.enable()
                for _ in range(30):
                    resp = client.get(slowest)
                    if resp.status_code >= 400:
                        raise RuntimeError(f"Profile request failed for {slowest}: {resp.status_code}")
                profile.disable()
            finally:
                event.remove(db.engine, "before_cursor_execute", before_cursor_execute)
                event.remove(db.engine, "after_cursor_execute", after_cursor_execute)

        stream = io.StringIO()
        stats = pstats.Stats(profile, stream=stream).sort_stats("cumulative")
        stats.print_stats(35)
        cprofile_top = stream.getvalue()

        print("Spectator Endpoint Benchmark (40 req each):")
        for row in summary:
            print(
                f"- {row['endpoint']}: mean={row['mean_ms']:.2f}ms p95={row['p95_ms']:.2f}ms max={row['max_ms']:.2f}ms"
            )
        print(f"\nSlowest endpoint: {slowest}")
        print(f"SQL during 30 requests to slowest: count={sql_count}, total_sql_time={sql_total_ms:.2f}ms")
        print("\nMost frequent SQL statements:")
        for statement, count in sql_samples.most_common(10):
            print(f"- {count}x {statement}")
        print("\nTop cProfile cumulative functions:")
        print(cprofile_top)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
