"""Executable contract for the race-day scoring service worker."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = REPO_ROOT / "static" / "sw.js"


def _worker_source() -> str:
    return WORKER_PATH.read_text(encoding="utf-8")


def test_offline_fallback_is_present_accessible_and_self_contained(client):
    response = client.get("/static/offline.html")

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    body = response.get_data(as_text=True)
    assert "Score page unavailable offline" in body
    assert "Do not clear this browser's site data" in body
    assert "http://" not in body
    assert "https://" not in body
    assert "<script" not in body


def test_worker_installs_fallback_and_retires_only_old_scoring_caches():
    source = _worker_source()

    assert "const CACHE_PREFIX = 'proam-scoring-'" in source
    assert "const CACHE_NAME = 'proam-scoring-v2'" in source
    assert "const OFFLINE_FALLBACK = '/static/offline.html'" in source
    assert "cache.addAll([OFFLINE_FALLBACK])" in source
    assert ".then(() => self.skipWaiting())" in source
    assert "offline.html optional" not in source
    assert "name.startsWith(CACHE_PREFIX)" in source
    assert "name !== CACHE_NAME" in source
    assert "caches.delete(name)" in source


def test_worker_never_caches_redirects_errors_or_non_html_score_responses():
    source = _worker_source()

    assert "response.ok" in source
    assert "!response.redirected" in source
    assert "response.url === request.url" in source
    assert "contentType.includes('text/html')" in source
    assert "isCacheableScorePage(response, request)" in source
    assert "catch (_cacheErr)" in source


def test_worker_keeps_new_score_posts_in_the_canonical_page_queue():
    source = _worker_source()

    assert "handleScorePost" not in source
    assert "objectStore('queue').add" not in source
    assert "request.method !== 'GET' || !sameOrigin(request.url)" in source
    assert "request.method === 'POST' && LOGOUT_PATTERN.test(request.url)" in source
    assert "drain-legacy-queue" in source
    assert "renewLegacyReplayToken" in source


def test_worker_uses_exact_cached_page_then_static_fallback_then_bounded_503():
    source = _worker_source()

    assert source.count("const cache = await caches.open(CACHE_NAME)") == 2
    assert "await cache.match(request)" in source
    assert "await cache.match(OFFLINE_FALLBACK)" in source
    assert "await caches.match(request)" not in source
    assert "status: 503" in source
    assert "Offline score page unavailable" in source


def test_worker_only_renews_legacy_entries_with_server_issued_replay_tokens():
    source = _worker_source()

    assert "params.set('replay_token', String(data.replay_token || ''))" in source
    assert "'/scoring/api/replay-token'" in source
    assert "legacy_issuer_session_mismatch" in source
    assert "generateReplayToken" not in source


def test_service_worker_route_is_strict_javascript_and_not_cache_stale(client):
    response = client.get("/sw.js")

    assert response.status_code == 200
    assert response.mimetype == "application/javascript"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.get_data() == WORKER_PATH.read_bytes()
