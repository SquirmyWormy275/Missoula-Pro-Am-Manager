"""Safety checks for the CI route-smoke database bootstrap."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_build_smoke_db.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("ci_build_smoke_db", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_db_default_never_targets_production_database():
    module = _load_script_module()

    assert module.DEFAULT_TARGET.name == "ci_route_smoke.db"
    assert module.DEFAULT_TARGET != module.PROJECT_ROOT / "instance" / "proam.db"


def test_smoke_db_builder_refuses_without_explicit_opt_in(monkeypatch, tmp_path, capsys):
    module = _load_script_module()
    sentinel = tmp_path / "ci_route_smoke.db"
    sentinel.write_text("do not replace")
    monkeypatch.delenv(module.BUILD_PERMISSION_ENV, raising=False)
    monkeypatch.setattr(module, "_target_path", lambda: sentinel)

    assert module.main() == 2
    assert sentinel.read_text() == "do not replace"
    assert "REFUSED" in capsys.readouterr().out


def test_smoke_db_target_rejects_production_filename(monkeypatch):
    module = _load_script_module()
    monkeypatch.setenv("PROAM_CI_SMOKE_DB_PATH", "instance/proam.db")

    try:
        module._target_path()
    except ValueError as exc:
        assert "non-production" in str(exc)
    else:
        raise AssertionError("production database path must be rejected")
