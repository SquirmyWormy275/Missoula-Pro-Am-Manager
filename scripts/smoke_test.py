#!/usr/bin/env python3
"""Read-only post-deploy smoke test for the public production surface.

The smoke proves application, database, migration, spectator HTML, and public
API availability. It does not authenticate a judge or mutate tournament data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

DEFAULT_PRODUCTION_URL = (
    "https://missoula-pro-am-manager-production.up.railway.app"
)
_SPECTATOR_PATH_RE = re.compile(r"^/portal/spectator/(\d+)$")


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def same_origin(base_url: str, target_url: str) -> bool:
    """Return whether a target, including a relative target, stays on origin."""
    return _origin(base_url) == _origin(urljoin(base_url.rstrip("/") + "/", target_url))


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects before urllib can send a request to another origin."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urljoin(req.full_url, newurl)
        if not same_origin(self.base_url, resolved):
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "cross-origin redirect refused",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def validate_health_payload(
    payload,
    *,
    expected_version: str | None = None,
    expected_migration: str | None = None,
    expected_commit: str | None = None,
) -> str | None:
    if not isinstance(payload, dict):
        return "health response JSON is not an object"
    if payload.get("status") != "ok":
        return "status is not ok"
    if payload.get("db") is not True:
        return "database is not healthy"
    if payload.get("migration_current") is not True:
        return "migration is not current"
    migration_head = payload.get("migration_head")
    migration_rev = payload.get("migration_rev")
    if not isinstance(migration_head, str) or not migration_head:
        return "migration head is missing"
    if not isinstance(migration_rev, str) or not migration_rev:
        return "migration revision is missing"
    if migration_rev != migration_head:
        return "migration revision does not match head"
    if expected_version and payload.get("version") != expected_version:
        return "version does not match expected release"
    if expected_migration and migration_rev != expected_migration:
        return "migration does not match expected revision"
    if expected_commit and payload.get("git_commit") != expected_commit:
        return "Git commit does not match expected deployment"
    return None


def extract_spectator_tournament_id(url: str) -> int | None:
    match = _SPECTATOR_PATH_RE.fullmatch(urlsplit(url).path.rstrip("/"))
    return int(match.group(1)) if match else None


def validate_public_standings(
    body: str,
    content_type: str,
    tournament_id: int,
) -> str | None:
    if "json" not in content_type.lower():
        return "response is not JSON"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "response is not valid JSON"
    if not isinstance(payload, dict):
        return "response JSON is not an object"
    if payload.get("error"):
        return "response contains an error"
    tournament = payload.get("tournament")
    if not isinstance(tournament, dict) or tournament.get("id") != tournament_id:
        return "response tournament does not match the active tournament"
    for key in ("teams", "bull", "belle", "pro_earnings"):
        if not isinstance(payload.get(key), list):
            return f"response field {key} is missing or not a list"
    return None


def local_git_head() -> str | None:
    """Return the checkout commit used as the default release-gate target."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip().lower()
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None


def fetch(opener, base_url: str, path: str) -> dict:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    started = time.monotonic()
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "ProAm-PostDeploy-Smoke/2.0"},
        )
        with opener.open(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {
                "path": path,
                "status": response.status,
                "body": body,
                "content_type": response.headers.get("Content-Type", ""),
                "final_url": response.geturl(),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error": None,
            }
    except urllib.error.HTTPError as exc:
        return {
            "path": path,
            "status": exc.code,
            "body": "",
            "content_type": "",
            "final_url": exc.geturl(),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": str(exc.reason),
        }
    except Exception as exc:  # pragma: no cover - network-specific error types
        return {
            "path": path,
            "status": 0,
            "body": "",
            "content_type": "",
            "final_url": url,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": str(exc),
        }


def _finish(result: dict, validation_error: str | None = None) -> dict:
    result["validation_error"] = validation_error
    result["ok"] = (
        result["status"] == 200
        and result["error"] is None
        and validation_error is None
    )
    result.pop("body", None)
    result.pop("content_type", None)
    return result


def run_smoke(
    base_url: str,
    *,
    expected_version: str | None = None,
    expected_migration: str | None = None,
    expected_commit: str | None = None,
) -> list[dict]:
    opener = urllib.request.build_opener(SameOriginRedirectHandler(base_url))
    results = []

    health = fetch(opener, base_url, "/health")
    health_error = health["error"]
    if health["status"] == 200 and health_error is None:
        try:
            payload = json.loads(health["body"])
        except json.JSONDecodeError:
            health_error = "health response is not valid JSON"
        else:
            health_error = validate_health_payload(
                payload,
                expected_version=expected_version,
                expected_migration=expected_migration,
                expected_commit=expected_commit,
            )
    results.append(_finish(health, health_error))

    portal = fetch(opener, base_url, "/portal/")
    tournament_id = extract_spectator_tournament_id(portal["final_url"])
    portal_error = portal["error"]
    if portal["status"] == 200 and portal_error is None:
        if not same_origin(base_url, portal["final_url"]):
            portal_error = "portal left the configured origin"
        elif tournament_id is None:
            portal_error = "portal did not resolve to an active spectator tournament"
        elif not portal["body"].strip():
            portal_error = "spectator page is empty"
    results.append(_finish(portal, portal_error))

    if tournament_id is not None:
        api_path = f"/api/public/tournaments/{tournament_id}/standings"
        standings = fetch(opener, base_url, api_path)
        standings_error = standings["error"]
        if standings["status"] == 200 and standings_error is None:
            standings_error = validate_public_standings(
                standings["body"], standings["content_type"], tournament_id
            )
        results.append(_finish(standings, standings_error))
    else:
        results.append({
            "path": "/api/public/tournaments/<active>/standings",
            "status": 0,
            "final_url": "",
            "elapsed_ms": 0,
            "error": None,
            "validation_error": "active tournament was not discovered",
            "ok": False,
        })

    login = fetch(opener, base_url, "/auth/login")
    login_error = login["error"]
    if login["status"] == 200 and login_error is None:
        body = login["body"]
        if 'name="username"' not in body or 'name="password"' not in body:
            login_error = "login page does not contain the sign-in form"
    results.append(_finish(login, login_error))
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "base_url",
        nargs="?",
        default=os.environ.get("PRODUCTION_URL", DEFAULT_PRODUCTION_URL),
    )
    parser.add_argument(
        "--expected-version",
        default=os.environ.get("EXPECTED_PRODUCTION_VERSION"),
    )
    parser.add_argument(
        "--expected-migration",
        default=os.environ.get("EXPECTED_MIGRATION_HEAD"),
    )
    parser.add_argument(
        "--expected-commit",
        default=os.environ.get("EXPECTED_GIT_COMMIT") or local_git_head(),
        help="Exact 40-character Git SHA expected from the deployment.",
    )
    parser.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="Run an exploratory smoke without binding it to a Git commit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.allow_unpinned and not args.expected_commit:
        parser.error(
            "release smoke requires --expected-commit (or EXPECTED_GIT_COMMIT); "
            "use --allow-unpinned only for an exploratory health check"
        )
    results = run_smoke(
        args.base_url,
        expected_version=args.expected_version,
        expected_migration=args.expected_migration,
        expected_commit=None if args.allow_unpinned else args.expected_commit,
    )

    print(f"Smoke testing: {args.base_url}")
    print("=" * 72)
    for result in results:
        marker = "PASS" if result["ok"] else "FAIL"
        detail = result["error"] or result["validation_error"] or ""
        print(
            f"  [{marker}] {result['path']:<48} "
            f"HTTP {result['status']:<3} {result['elapsed_ms']:>5}ms {detail}"
        )
    passed = sum(result["ok"] for result in results)
    print("=" * 72)
    print(f"Result: {passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
