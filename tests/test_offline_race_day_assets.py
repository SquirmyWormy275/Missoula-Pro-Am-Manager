"""Race-day pages must not depend on public runtime asset CDNs."""

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = REPO_ROOT / "static" / "vendor"
SORTABLE_TEMPLATES = (
    "templates/proam_relay/manual_teams.html",
    "templates/scheduling/ability_rankings.html",
    "templates/scheduling/events.html",
    "templates/scheduling/heat_sheets_print.html",
)


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


def test_drag_and_drop_pages_use_the_manifest_backed_sortable_asset(client):
    manifest = json.loads((VENDOR_ROOT / "manifest.json").read_text(encoding="utf-8"))
    sortable = next(
        entry for entry in manifest["assets"] if entry["package"] == "sortablejs"
    )
    template_reference = f"filename='vendor/{sortable['path']}'"

    for relative_path in SORTABLE_TEMPLATES:
        template = REPO_ROOT / relative_path
        assert template_reference in template.read_text(encoding="utf-8")

    response = client.get(f"/static/vendor/{sortable['path']}")
    assert response.status_code == 200
    assert hashlib.sha256(response.data).hexdigest() == sortable["sha256"]


def test_login_renders_only_local_core_assets_and_self_only_csp(client):
    response = client.get("/auth/login")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "/static/vendor/bootstrap/css/bootstrap.min.css" in body
    assert "/static/vendor/bootstrap-icons/font/bootstrap-icons.min.css" in body
    assert "/static/vendor/bootstrap/js/bootstrap.bundle.min.js" in body
    assert "https://cdn.jsdelivr.net" not in body
    assert "https://fonts.googleapis.com" not in body

    csp = response.headers["Content-Security-Policy"]
    assert "cdn.jsdelivr.net" not in csp
    assert "fonts.googleapis.com" not in csp
    assert "fonts.gstatic.com" not in csp
