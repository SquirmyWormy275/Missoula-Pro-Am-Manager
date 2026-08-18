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
    assert "key.startsWith(CACHE_PREFIX)" in source
    assert "key !== CACHE_NAME" in source
    assert "caches.delete(key)" in source


def test_worker_never_caches_redirects_errors_or_non_html_score_responses():
    source = _worker_source()

    assert "response.ok" in source
    assert "!response.redirected" in source
    assert "response.url === request.url" in source
    assert "contentType.includes('text/html')" in source
    assert "isCacheableScorePage(resp, req)" in source
    assert "catch (_cacheErr)" in source


def test_worker_intercepts_and_replays_only_same_origin_score_urls():
    source = _worker_source()

    assert "function isSameOriginScoreEntryUrl(rawUrl)" in source
    assert "url.origin === self.location.origin" in source
    assert source.count("isScoreEntryRequest(req)") == 2
    assert "isSameOriginScoreEntryUrl(entry.url)" in source
    assert "failReasons.push('invalid_queue_target')" in source
    assert "SCORE_ENTRY_PATTERN.test(req.url)" not in source


def test_worker_uses_exact_cached_page_then_static_fallback_then_bounded_503():
    source = _worker_source()

    assert source.count("const cache = await caches.open(CACHE_NAME)") == 2
    assert "await cache.match(req)" in source
    assert "await cache.match(OFFLINE_FALLBACK)" in source
    assert "await caches.match(req)" not in source
    assert "status: 503" in source
    assert "Offline score page unavailable" in source


def test_worker_queues_only_the_server_issued_hmac_replay_token():
    source = _worker_source()

    assert "new URLSearchParams(body).get('replay_token')" in source
    assert "generateReplayToken" not in source
    assert "crypto.getRandomValues" not in source


def test_service_worker_route_is_strict_javascript_and_not_cache_stale(client):
    response = client.get("/sw.js")

    assert response.status_code == 200
    assert response.mimetype == "application/javascript"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.get_data() == WORKER_PATH.read_bytes()
