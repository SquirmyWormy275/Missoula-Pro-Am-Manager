"""Race-day pages must not depend on public runtime asset CDNs."""

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = REPO_ROOT / "static" / "vendor"


# Regression: ISSUE-002 — public CDNs broke the offline operator shell.
# Found by /qa on 2026-08-14.
# Report: .gstack/qa-reports/qa-report-local-shadow-2026-08-14.md
def test_templates_do_not_load_public_runtime_assets():
    forbidden = (
        "cdn.jsdelivr.net",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
    )

    violations = []
    for template in sorted((REPO_ROOT / "templates").rglob("*.html")):
        content = template.read_text(encoding="utf-8")
        for origin in forbidden:
            if origin in content:
                violations.append(f"{template.relative_to(REPO_ROOT)}: {origin}")

    assert violations == []


def test_vendored_asset_manifest_matches_every_runtime_byte():
    manifest_path = VENDOR_ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == "missoula.vendored-assets.v1"
    assert {entry["package"] for entry in manifest["assets"]} == {
        "bootstrap",
        "bootstrap-icons",
        "sortablejs",
    }
    assert len(manifest["assets"]) == 6

    for entry in manifest["assets"]:
        assert entry["license"] == "MIT"
        assert entry["version"]
        asset = VENDOR_ROOT / entry["path"]
        assert asset.is_file()
        assert asset.stat().st_size > 100
        assert hashlib.sha256(asset.read_bytes()).hexdigest() == entry["sha256"]


def test_login_renders_only_local_core_assets_and_self_only_csp(client):
    response = client.get("/auth/login")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "/static/vendor/bootstrap-5.3.2/bootstrap.min.css" in body
    assert "/static/vendor/bootstrap-icons-1.11.1/bootstrap-icons.min.css" in body
    assert "/static/vendor/bootstrap-5.3.2/bootstrap.bundle.min.js" in body
    assert "https://cdn.jsdelivr.net" not in body
    assert "https://fonts.googleapis.com" not in body

    csp = response.headers["Content-Security-Policy"]
    assert "cdn.jsdelivr.net" not in csp
    assert "fonts.googleapis.com" not in csp
    assert "fonts.gstatic.com" not in csp
