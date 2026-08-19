from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import load_test_race_day
from tests import db_test_utils


def test_csrf_parser_reads_login_token():
    parser = load_test_race_day._CsrfTokenParser()
    parser.feed(
        '<form><input value="session-token" type="hidden" '
        'name="csrf_token"></form>'
    )

    assert parser.token == "session-token"


def test_load_rehearsal_refuses_production_database():
    with pytest.raises(ValueError, match="refuses instance/proam.db"):
        load_test_race_day._resolve_synthetic_database(
            str(load_test_race_day.PRODUCTION_DB_PATH)
        )


def test_load_rehearsal_refuses_existing_database(tmp_path):
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"do-not-overwrite")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        load_test_race_day._resolve_synthetic_database(str(existing))

    assert existing.read_bytes() == b"do-not-overwrite"


def test_generated_rehearsal_database_is_disposable():
    path, generated = load_test_race_day._resolve_synthetic_database(None)
    try:
        assert generated is True
        assert path.is_file()
        assert path != load_test_race_day.PRODUCTION_DB_PATH
    finally:
        load_test_race_day._remove_synthetic_database(path)
    assert not path.exists()


@pytest.mark.parametrize(
    ("error_rate", "p95_ms", "expected"),
    [
        (0.0, 250.0, True),
        (0.01, 250.0, False),
        (0.0, 900.0, False),
    ],
)
def test_load_rehearsal_gate_is_truthful(error_rate, p95_ms, expected):
    report = {
        "requests": {"error_rate": error_rate},
        "latency_ms": {"p95": p95_ms},
        "status_codes": {},
    }

    assert load_test_race_day._passes_gate(
        report,
        target_p95_ms=800.0,
        max_error_rate=0.005,
    ) is expected


def test_load_rehearsal_gate_rejects_server_error_below_rate_limit():
    report = {
        "requests": {"error_rate": 0.001},
        "latency_ms": {"p95": 100.0},
        "status_codes": {"200": 999, "500": 1},
    }

    assert load_test_race_day._passes_gate(
        report,
        target_p95_ms=800.0,
        max_error_rate=0.005,
    ) is False


def test_postgres_template_setup_never_sweeps_unowned_clones(monkeypatch):
    calls = []
    was_ready = db_test_utils._pg_template_ready

    def fake_pg_run(sql, dbname="postgres"):
        calls.append((sql, dbname))
        if "SELECT 1 FROM pg_database" in sql:
            return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(db_test_utils, "_pg_preflight", lambda: None)
    monkeypatch.setattr(db_test_utils, "_pg_run", fake_pg_run)
    monkeypatch.setattr(db_test_utils, "_chain_head", lambda: "head")
    monkeypatch.setattr(db_test_utils, "_template_is_stale", lambda _head: False)
    db_test_utils._pg_template_ready = False
    try:
        db_test_utils._ensure_pg_template()
    finally:
        db_test_utils._pg_template_ready = was_ready

    rendered = "\n".join(sql for sql, _dbname in calls)
    assert "LIKE 'proam_unit_%'" not in rendered
    assert "DROP DATABASE" not in rendered


def test_load_stats_reports_timeout_categories():
    stats = load_test_race_day.Stats()

    stats.add(25.0, 200)
    stats.add(6000.0, None, error_kind="timeout", path="GET /slow")
    stats.add(40.0, 503, path="GET /unavailable")

    assert stats.total == 3
    assert stats.success == 1
    assert stats.errors == 2
    assert stats.error_kinds == {"timeout": 1, "http_503": 1}
    assert stats.error_paths == {
        "GET /slow": 1,
        "GET /unavailable": 1,
    }


def test_load_shape_uses_requested_virtual_user_counts(monkeypatch):
    worker_calls = []

    def fake_worker(
        base_url,
        endpoints,
        stop_event,
        stats,
        timeout,
        start_delay,
        think_min,
        think_max,
        login_credentials,
    ):
        del base_url, endpoints, timeout, start_delay, think_min, think_max
        worker_calls.append(login_credentials)
        stats.add(1.0, 200)
        stop_event.wait()

    monkeypatch.setattr(load_test_race_day, "_worker", fake_worker)

    report = load_test_race_day._run_load_test(
        "http://127.0.0.1:1",
        tournament_id=1,
        duration_s=0,
        timeout_s=1.0,
        spectator_users=2,
        competitor_users=1,
        judge_users=1,
        judge_heat_ids=[7, 8],
        ramp_up_s=0.0,
        spectator_think=(25.0, 35.0),
        competitor_think=(20.0, 60.0),
        judge_think=(3.0, 8.0),
    )

    assert report["users"] == {
        "spectators": 2,
        "competitors": 1,
        "judges": 1,
        "total": 4,
    }
    assert report["requests"]["total"] == 4
    assert report["load_shape"]["configured_ramp_up_seconds"] == 0.0
    assert report["load_shape"]["think_time_seconds"]["spectators"] == [25.0, 35.0]
    assert worker_calls.count(("judge_loadtest_1", "LoadTest123!")) == 1
    assert worker_calls.count(None) == 3
