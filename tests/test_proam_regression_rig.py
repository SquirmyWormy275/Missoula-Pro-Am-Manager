"""Unit coverage for safe ownership of real-data regression clones."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def rig_module():
    path = Path(__file__).parents[1] / "proam_regression" / "rig.py"
    spec = importlib.util.spec_from_file_location("proam_regression_rig_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_clone_name_contains_its_session_token(monkeypatch, rig_module):
    monkeypatch.setattr(rig_module, "RUN_TOKEN", "a" * 12)
    monkeypatch.setattr(rig_module.uuid, "uuid4", lambda: SimpleNamespace(hex="b" * 32))
    monkeypatch.setattr(rig_module, "_psql", lambda *_args: SimpleNamespace(returncode=0, stderr=""))

    name, _url = rig_module.clone_production()

    assert name == f"proam_rt_{'a' * 12}_{'b' * 10}"


def test_invalid_configured_run_token_fails_before_clone(monkeypatch, rig_module):
    monkeypatch.setattr(rig_module, "RUN_TOKEN", "not-a-safe-token")

    with pytest.raises(RuntimeError, match="PROAM_RIG_RUN_TOKEN"):
        rig_module.clone_production()


def test_cleanup_skips_live_and_legacy_clones(monkeypatch, rig_module):
    dead = "a" * 12
    live = "b" * 12
    dead_clone = f"proam_rt_{dead}_{'c' * 10}"
    live_clone = f"proam_rt_{live}_{'d' * 10}"
    legacy = "proam_rt_legacyclone"
    dropped = []
    monkeypatch.setattr(rig_module, "orphan_clones", lambda: [dead_clone, live_clone, legacy])
    monkeypatch.setattr(rig_module, "run_is_active", lambda token: token == live)
    monkeypatch.setattr(rig_module, "drop_clone", dropped.append)

    assert rig_module.drop_orphans() == [dead_clone]
    assert dropped == [dead_clone]


def test_session_cleanup_drops_only_its_own_tokenized_clones(monkeypatch, rig_module):
    ours = "a" * 12
    other = "b" * 12
    own_clone = f"proam_rt_{ours}_{'c' * 10}"
    other_clone = f"proam_rt_{other}_{'d' * 10}"
    dropped = []
    monkeypatch.setattr(rig_module, "orphan_clones", lambda: [own_clone, other_clone])
    monkeypatch.setattr(rig_module, "drop_clone", dropped.append)

    assert rig_module.drop_run_clones(ours) == [own_clone]
    assert dropped == [own_clone]


def test_drop_clone_raises_when_postgres_rejects_cleanup(monkeypatch, rig_module):
    monkeypatch.setattr(rig_module, "_CLONE_DROP_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(rig_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        rig_module,
        "_psql",
        lambda *_args: SimpleNamespace(returncode=1, stderr="database is busy"),
    )

    with pytest.raises(RuntimeError, match="database is busy"):
        rig_module.drop_clone("proam_rt_aaaaaaaaaaaa_bbbbbbbbbb")


def test_drop_clone_retries_a_transient_foreign_connection(monkeypatch, rig_module):
    outcomes = iter([
        SimpleNamespace(returncode=1, stderr="permission denied to terminate process"),
        SimpleNamespace(returncode=1, stderr="database is being accessed by other users"),
        SimpleNamespace(returncode=0, stderr=""),
    ])
    calls = []
    monkeypatch.setattr(rig_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        rig_module,
        "_psql",
        lambda *args: (calls.append(args), next(outcomes))[1],
    )

    rig_module.drop_clone("proam_rt_aaaaaaaaaaaa_bbbbbbbbbb")

    assert len(calls) == 3
    assert "WITH (FORCE)" in calls[0][1]
    assert "WITH (FORCE)" not in calls[1][1]
