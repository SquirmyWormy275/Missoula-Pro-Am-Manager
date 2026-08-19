from contextlib import nullcontext
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


def test_report_output_refuses_database_aliases_without_touching_bytes(tmp_path):
    database_path = tmp_path / "retained-fixture.db"
    database_path.write_bytes(b"synthetic-database")

    for protected_path in (
        load_test_race_day.PRODUCTION_DB_PATH,
        database_path,
    ):
        with pytest.raises(ValueError, match="cannot be a database path"):
            load_test_race_day._resolve_report_output(
                str(protected_path),
                database_path=database_path,
                overwrite=True,
            )

    assert database_path.read_bytes() == b"synthetic-database"


def test_report_output_requires_explicit_overwrite(tmp_path):
    database_path = tmp_path / "fixture.db"
    output_path = tmp_path / "report.json"
    output_path.write_bytes(b"keep-this-report")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        load_test_race_day._resolve_report_output(
            str(output_path),
            database_path=database_path,
            overwrite=False,
        )

    assert output_path.read_bytes() == b"keep-this-report"


def test_report_output_refuses_hard_link_database_alias(tmp_path):
    database_path = tmp_path / "fixture.db"
    database_path.write_bytes(b"database-bytes")
    output_path = tmp_path / "report.json"
    output_path.hardlink_to(database_path)

    with pytest.raises(ValueError, match="cannot be a database path"):
        load_test_race_day._resolve_report_output(
            str(output_path),
            database_path=database_path,
            overwrite=True,
        )

    assert database_path.read_bytes() == b"database-bytes"


def test_report_write_rechecks_path_after_validation(tmp_path):
    database_path = tmp_path / "fixture.db"
    database_path.write_bytes(b"database-bytes")
    output_path = load_test_race_day._resolve_report_output(
        str(tmp_path / "report.json"),
        database_path=database_path,
        overwrite=True,
    )
    output_path.hardlink_to(database_path)

    with pytest.raises(ValueError, match="cannot be a database path"):
        load_test_race_day._write_report(
            output_path,
            {"passed": True},
            overwrite=True,
            database_path=database_path,
        )

    assert database_path.read_bytes() == b"database-bytes"


def test_report_overwrite_uses_atomic_replacement(tmp_path):
    database_path = tmp_path / "fixture.db"
    database_path.write_bytes(b"database-bytes")
    output_path = tmp_path / "report.json"
    output_path.write_text("old-report", encoding="utf-8")

    load_test_race_day._write_report(
        output_path,
        {"passed": True},
        overwrite=True,
        database_path=database_path,
    )

    assert output_path.read_text(encoding="utf-8") == '{\n  "passed": true\n}\n'
    assert database_path.read_bytes() == b"database-bytes"


def test_report_creation_is_atomic_and_no_clobber(tmp_path):
    database_path = tmp_path / "fixture.db"
    database_path.write_bytes(b"database-bytes")
    output_path = tmp_path / "report.json"

    load_test_race_day._write_report(
        output_path,
        {"passed": True},
        overwrite=False,
        database_path=database_path,
    )

    assert output_path.read_text(encoding="utf-8") == '{\n  "passed": true\n}\n'
    with pytest.raises(FileExistsError):
        load_test_race_day._write_report(
            output_path,
            {"passed": False},
            overwrite=False,
            database_path=database_path,
        )
    assert output_path.read_text(encoding="utf-8") == '{\n  "passed": true\n}\n'


def test_report_serialization_failure_leaves_no_partial_output(tmp_path):
    database_path = tmp_path / "fixture.db"
    database_path.write_bytes(b"database-bytes")
    output_path = tmp_path / "report.json"

    with pytest.raises(TypeError):
        load_test_race_day._write_report(
            output_path,
            {"not_json": object()},
            overwrite=False,
            database_path=database_path,
        )

    assert not output_path.exists()
    assert list(tmp_path.glob(".report.json.*.tmp")) == []


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


def test_load_rehearsal_gate_rejects_slow_judge_role_hidden_by_aggregate():
    report = {
        "users": {"spectators": 200, "competitors": 50, "judges": 10},
        "requests": {"error_rate": 0.0},
        "latency_ms": {"p95": 120.0},
        "status_codes": {"200": 1000},
        "by_role": {
            "spectators": {
                "requests": 800,
                "errors": 0,
                "activations": 200,
                "p95_ms": 100.0,
            },
            "competitors": {
                "requests": 150,
                "errors": 0,
                "activations": 50,
                "p95_ms": 110.0,
            },
            "judges": {
                "requests": 50,
                "errors": 0,
                "activations": 10,
                "authentications": 10,
                "p95_ms": 900.0,
            },
        },
    }

    assert load_test_race_day._passes_gate(
        report,
        target_p95_ms=800.0,
        target_role_p95_ms=800.0,
        max_error_rate=0.005,
    ) is False


def test_load_rehearsal_gate_requires_each_judge_to_authenticate_and_request():
    report = {
        "users": {"judges": 10},
        "requests": {"error_rate": 0.0},
        "latency_ms": {"p95": 100.0},
        "status_codes": {"200": 29},
        "by_role": {
            "judges": {
                "requests": 29,
                "errors": 0,
                "activations": 10,
                "authentications": 10,
                "p95_ms": 100.0,
            },
        },
    }

    assert load_test_race_day._passes_gate(
        report,
        target_p95_ms=800.0,
        target_role_p95_ms=800.0,
        max_error_rate=0.005,
    ) is False


def test_load_rehearsal_gate_rejects_slow_endpoint_hidden_by_role():
    report = {
        "users": {"judges": 1},
        "load_shape": {
            "expected_paths": {
                "judges": [
                    "GET /auth/login",
                    "POST /auth/login",
                    "GET /scoring/1/offline-ops",
                ],
            },
        },
        "requests": {"error_rate": 0.0},
        "latency_ms": {"p95": 100.0},
        "status_codes": {"200": 102},
        "by_role": {
            "judges": {
                "requests": 102,
                "errors": 0,
                "activations": 1,
                "authentications": 1,
                "p95_ms": 100.0,
                "by_path": {
                    "GET /auth/login": {
                        "requests": 1,
                        "errors": 0,
                        "p95_ms": 20.0,
                    },
                    "POST /auth/login": {
                        "requests": 1,
                        "errors": 0,
                        "p95_ms": 30.0,
                    },
                    "GET /scoring/1/offline-ops": {
                        "requests": 100,
                        "errors": 0,
                        "p95_ms": 900.0,
                    },
                },
            },
        },
    }

    assert load_test_race_day._passes_gate(
        report,
        target_p95_ms=800.0,
        target_role_p95_ms=800.0,
        max_error_rate=0.005,
    ) is False


class _FakeResponse:
    def __init__(self, final_url, body=b"complete-body", status=200):
        self.status = status
        self._final_url = final_url
        self._body = body
        self.read_calls = []

    def read(self, *args):
        self.read_calls.append(args)
        return self._body

    def geturl(self):
        return self._final_url


def test_authenticated_measurement_rejects_login_redirect_and_reads_full_body():
    response = _FakeResponse("http://127.0.0.1:5050/auth/login")

    status, error = load_test_race_day._consume_measured_response(
        response,
        "http://127.0.0.1:5050/scoring/1/offline-ops",
        authenticated=True,
    )

    assert (status, error) == (401, "authentication_lost")
    assert response.read_calls == [()]


def test_authenticated_measurement_accepts_requested_scoring_page():
    response = _FakeResponse("http://127.0.0.1:5050/scoring/1/offline-ops")

    assert load_test_race_day._consume_measured_response(
        response,
        "http://127.0.0.1:5050/scoring/1/offline-ops",
        authenticated=True,
    ) == (200, None)


def test_public_measurement_rejects_redirect_to_wrong_page():
    response = _FakeResponse("http://127.0.0.1:5050/auth/login")

    assert load_test_race_day._consume_measured_response(
        response,
        "http://127.0.0.1:5050/portal/spectator/1",
        authenticated=False,
    ) == (None, "unexpected_redirect")


def test_measurement_rejects_same_path_on_different_origin():
    response = _FakeResponse("http://127.0.0.1:5999/portal/spectator/1")

    assert load_test_race_day._consume_measured_response(
        response,
        "http://127.0.0.1:5050/portal/spectator/1",
        authenticated=False,
    ) == (None, "unexpected_redirect")


def test_redirect_handler_blocks_cross_origin_before_following():
    handler = load_test_race_day._SameOriginRedirectHandler(
        "http://127.0.0.1:5050"
    )
    request = load_test_race_day.urllib.request.Request(
        "http://127.0.0.1:5050/portal/spectator/1"
    )

    with pytest.raises(load_test_race_day.CrossOriginRedirectError):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://127.0.0.1:5999/portal/spectator/1",
        )


@pytest.mark.parametrize(
    ("activations", "authentications"),
    [(1, 2), (2, 1)],
)
def test_role_gate_rejects_surplus_requests_masking_missing_users(
    activations,
    authentications,
):
    report = {
        "users": {"judges": 2},
        "load_shape": {
            "expected_paths": {
                "judges": [
                    "GET /auth/login",
                    "POST /auth/login",
                    "GET /scoring/1/offline-ops",
                ],
            },
        },
        "requests": {"error_rate": 0.0},
        "latency_ms": {"p95": 100.0},
        "status_codes": {"200": 100},
        "by_role": {
            "judges": {
                "requests": 100,
                "errors": 0,
                "activations": activations,
                "authentications": authentications,
                "p95_ms": 100.0,
                "by_path": {
                    "GET /auth/login": {
                        "requests": 2,
                        "errors": 0,
                        "p95_ms": 20.0,
                    },
                    "POST /auth/login": {
                        "requests": 2,
                        "errors": 0,
                        "p95_ms": 30.0,
                    },
                    "GET /scoring/1/offline-ops": {
                        "requests": 96,
                        "errors": 0,
                        "p95_ms": 40.0,
                    },
                },
            },
        },
    }

    assert load_test_race_day._passes_gate(
        report,
        target_p95_ms=800.0,
        target_role_p95_ms=800.0,
        max_error_rate=0.005,
    ) is False


def test_role_gate_requires_one_successful_login_sample_per_judge():
    report = {
        "users": {"judges": 2},
        "load_shape": {
            "expected_paths": {
                "judges": ["GET /auth/login", "POST /auth/login"],
            },
        },
        "requests": {"error_rate": 0.0},
        "latency_ms": {"p95": 100.0},
        "status_codes": {"200": 100},
        "by_role": {
            "judges": {
                "requests": 100,
                "errors": 0,
                "activations": 2,
                "authentications": 2,
                "p95_ms": 100.0,
                "by_path": {
                    "GET /auth/login": {
                        "requests": 2,
                        "errors": 0,
                        "p95_ms": 20.0,
                    },
                    "POST /auth/login": {
                        "requests": 1,
                        "errors": 0,
                        "p95_ms": 30.0,
                    },
                },
            },
        },
    }

    assert load_test_race_day._passes_gate(
        report,
        target_p95_ms=800.0,
        target_role_p95_ms=800.0,
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
    monkeypatch.setattr(db_test_utils, "_pg_template_lock", nullcontext)
    monkeypatch.setattr(db_test_utils, "_pg_run", fake_pg_run)
    monkeypatch.setattr(db_test_utils, "_chain_head", lambda: "head")
    monkeypatch.setattr(
        db_test_utils,
        "_template_is_stale",
        lambda _head, _template_name: False,
    )
    db_test_utils._pg_template_ready = None
    try:
        db_test_utils._ensure_pg_template()
    finally:
        db_test_utils._pg_template_ready = was_ready

    rendered = "\n".join(sql for sql, _dbname in calls)
    assert "LIKE 'proam_unit_%'" not in rendered
    assert "DROP DATABASE" not in rendered


def test_postgres_template_name_is_namespaced_by_migration_head():
    first = db_test_utils._pg_template_name("revision-a")
    second = db_test_utils._pg_template_name("revision-b")

    assert first.startswith("proam_unit_template_")
    assert second.startswith("proam_unit_template_")
    assert first != second
    assert len(first) <= 63


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
        start_at,
        think_min,
        think_max,
        login_credentials,
        rng_seed,
    ):
        del base_url, endpoints, timeout, start_at, think_min, think_max, rng_seed
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
        judge_users=2,
        judge_heat_ids=[7, 8],
        ramp_up_s=0.0,
        spectator_think=(25.0, 35.0),
        competitor_think=(20.0, 60.0),
        judge_think=(3.0, 8.0),
        judge_credentials=[("judge-a", "password"), ("judge-b", "password")],
    )

    assert report["users"] == {
        "spectators": 2,
        "competitors": 1,
        "judges": 2,
        "total": 5,
    }
    assert report["requests"]["total"] == 5
    assert report["load_shape"]["configured_ramp_up_seconds"] == 0.0
    assert report["load_shape"]["think_time_seconds"]["spectators"] == [25.0, 35.0]
    assert report["load_shape"]["seed"] == 2027
    assert worker_calls.count(("judge-a", "password")) == 1
    assert worker_calls.count(("judge-b", "password")) == 1
    assert worker_calls.count(None) == 3


def test_load_shape_seed_replays_worker_random_streams(monkeypatch):
    captured = []

    def fake_worker(
        base_url,
        endpoints,
        stop_event,
        stats,
        timeout,
        start_at,
        think_min,
        think_max,
        login_credentials,
        rng_seed,
    ):
        del base_url, timeout, start_at, think_min, think_max
        captured.append((tuple(endpoints), login_credentials, rng_seed))
        stats.activate(load_test_race_day.time.perf_counter())
        stats.add(1.0, 200, path=f"GET {endpoints[0]}")
        stop_event.wait()

    monkeypatch.setattr(load_test_race_day, "_worker", fake_worker)

    def run_once():
        captured.clear()
        load_test_race_day._run_load_test(
            "http://127.0.0.1:1",
            tournament_id=1,
            duration_s=0,
            timeout_s=1.0,
            spectator_users=2,
            competitor_users=1,
            judge_users=1,
            judge_heat_ids=[7],
            ramp_up_s=0.0,
            seed=77,
        )
        return sorted(repr(item) for item in captured)

    assert run_once() == run_once()


def test_load_runner_stops_started_threads_when_startup_fails(monkeypatch):
    created = []

    class FakeThread:
        def __init__(self, target, args, **kwargs):
            del kwargs
            self.target = target
            self.args = args
            self.name = f"fake-{len(created)}"
            self.joined = False
            created.append(self)

        def start(self):
            if self.name == "fake-1":
                raise RuntimeError("synthetic thread-start failure")

        def join(self, timeout=None):
            del timeout
            assert self.args[8].is_set()
            self.joined = True

        def is_alive(self):
            return not self.joined

    monkeypatch.setattr(load_test_race_day.threading, "Thread", FakeThread)

    with pytest.raises(RuntimeError, match="synthetic thread-start failure"):
        load_test_race_day._run_load_test(
            "http://127.0.0.1:1",
            tournament_id=1,
            duration_s=0,
            timeout_s=1.0,
            spectator_users=2,
            competitor_users=0,
            judge_users=0,
            ramp_up_s=0.0,
        )

    assert created[0].joined is True


def test_load_runner_surfaces_unexpected_worker_failure(monkeypatch):
    def broken_worker(*args):
        del args
        raise RuntimeError("synthetic worker failure")

    monkeypatch.setattr(load_test_race_day, "_worker", broken_worker)

    with pytest.raises(RuntimeError, match="synthetic worker failure"):
        load_test_race_day._run_load_test(
            "http://127.0.0.1:1",
            tournament_id=1,
            duration_s=0,
            timeout_s=1.0,
            spectator_users=1,
            competitor_users=0,
            judge_users=0,
            ramp_up_s=0.0,
        )
