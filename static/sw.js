/**
 * Missoula Pro-Am Service Worker
 * Caches scoring entry pages and queues POST submissions when offline.
 * Replays the queue via Background Sync when connectivity is restored.
 */

const CACHE_PREFIX = 'proam-scoring-';
const CACHE_NAME = 'proam-scoring-v2';
const OFFLINE_FALLBACK = '/static/offline.html';
const SCORE_ENTRY_PATTERN = /\/scoring\/\d+\/heat\/\d+\/enter/;

// ── Install: cache the offline fallback ─────────────────────────────────────
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache =>
            cache.addAll([OFFLINE_FALLBACK])
        ).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(
                keys
                    .filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
                    .map(key => caches.delete(key))
            ))
            .then(() => self.clients.claim())
    );
});

// ── IndexedDB helpers ────────────────────────────────────────────────────────
function openDB() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open('proam-offline-queue', 1);
        req.onupgradeneeded = (e) => {
            e.target.result.createObjectStore('queue', { keyPath: 'id', autoIncrement: true });
        };
        req.onsuccess = (e) => resolve(e.target.result);
        req.onerror = (e) => reject(e.target.error);
    });
}

function enqueue(db, entry) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction('queue', 'readwrite');
        tx.objectStore('queue').add(entry);
        tx.oncomplete = resolve;
        tx.onerror = (e) => reject(e.target.error);
    });
}

function dequeueAll(db) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction('queue', 'readonly');
        const req = tx.objectStore('queue').getAll();
        req.onsuccess = (e) => resolve(e.target.result);
        req.onerror = (e) => reject(e.target.error);
    });
}

function removeEntry(db, id) {
    return new Promise((resolve, reject) => {
        const tx = db.transaction('queue', 'readwrite');
        tx.objectStore('queue').delete(id);
        tx.oncomplete = resolve;
        tx.onerror = (e) => reject(e.target.error);
    });
}

function isSameOriginScoreEntryUrl(rawUrl) {
    try {
        const url = new URL(rawUrl, self.location.origin);
        return url.origin === self.location.origin
            && SCORE_ENTRY_PATTERN.test(url.pathname);
    } catch (_invalidUrl) {
        return false;
    }
}

function isScoreEntryRequest(request) {
    return isSameOriginScoreEntryUrl(request.url);
}

// ── Fetch handler ────────────────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
    const req = event.request;

    // Intercept POST to score entry when offline
    if (req.method === 'POST' && isScoreEntryRequest(req)) {
        event.respondWith(handleScorePost(req));
        return;
    }

    // Network-first for GET score entry pages (cache for offline fallback)
    if (req.method === 'GET' && isScoreEntryRequest(req)) {
        event.respondWith(loadScorePage(req));
        return;
    }
    // All other requests: default browser handling
});

function isCacheableScorePage(response, request) {
    const contentType = response.headers.get('content-type') || '';
    return response.ok
        && !response.redirected
        && response.url === request.url
        && contentType.includes('text/html');
}

async function loadScorePage(req) {
    try {
        const resp = await fetch(req);
        if (isCacheableScorePage(resp, req)) {
            try {
                const cache = await caches.open(CACHE_NAME);
                await cache.put(req, resp.clone());
            } catch (_cacheErr) {
                // A quota/cache failure must never replace a valid live page.
            }
        }
        return resp;
    } catch (_networkErr) {
        try {
            const cache = await caches.open(CACHE_NAME);
            const cachedPage = await cache.match(req);
            if (cachedPage) return cachedPage;

            const fallback = await cache.match(OFFLINE_FALLBACK);
            if (fallback) return fallback;
        } catch (_cacheReadErr) {
            // Fall through to an explicit bounded response.
        }

        return new Response('Offline score page unavailable', {
            status: 503,
            headers: { 'Content-Type': 'text/plain; charset=utf-8' },
        });
    }
}

async function handleScorePost(request) {
    // Try network first
    try {
        return await fetch(request.clone());
    } catch (_networkErr) {
        // Offline — queue the submission with a replay token
        const body = await request.text();
        const replay_token = new URLSearchParams(body).get('replay_token') || '';
        // Extract tournament_id and heat_id from the URL pattern:
        // /scoring/{tournament_id}/heat/{heat_id}/enter
        const urlMatch = new URL(request.url).pathname.match(
            /\/scoring\/(\d+)\/heat\/(\d+)\/enter/
        );
        const tournament_id = urlMatch ? urlMatch[1] : '';
        const heat_id = urlMatch ? urlMatch[2] : '';
        const db = await openDB();
        await enqueue(db, {
            url: request.url,
            method: 'POST',
            body,
            replay_token,
            tournament_id,
            heat_id,
            timestamp: Date.now(),
        });

        // Register Background Sync to replay when back online
        try {
            await self.registration.sync.register('score-sync');
        } catch (_syncErr) {
            // Background Sync API not supported — queue will be replayed on next page load
        }

        return new Response(
            JSON.stringify({
                ok: false,
                offline: true,
                category: 'warning',
                message: 'You are offline. Score queued — it will sync automatically when connection restores.',
            }),
            {
                status: 202,
                headers: { 'Content-Type': 'application/json' },
            }
        );
    }
}

// ── Manual sync trigger (fallback for browsers without Background Sync) ──────
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'manual-sync') {
        replayQueue().catch(() => {});
    }
});

// ── Background Sync ──────────────────────────────────────────────────────────
self.addEventListener('sync', (event) => {
    if (event.tag === 'score-sync') {
        event.waitUntil(replayQueue());
    }
});

async function replayQueue() {
    const db = await openDB();
    const entries = await dequeueAll(db);
    let successCount = 0;
    let failedCount = 0;
    const failReasons = [];

    for (const entry of entries) {
        if (!isSameOriginScoreEntryUrl(entry.url)) {
            failedCount++;
            failReasons.push('invalid_queue_target');
            continue;
        }
        try {
            const resp = await fetch(entry.url, {
                method: entry.method,
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: entry.body,
            });

            if (resp.status >= 200 && resp.status < 300) {
                // 2xx — server accepted the score. Safe to dequeue.
                await removeEntry(db, entry.id);
                successCount++;
            } else if (resp.status === 400 || resp.status === 403) {
                // CSRF token expired or session invalid.
                // Try the CSRF-exempt replay endpoint. The HMAC replay_token is
                // already embedded in entry.body (from the form hidden input
                // populated via /scoring/api/replay-token on page load — CSO #6).
                if (entry.replay_token) {
                    const replayBody = entry.body
                        + '&tournament_id=' + encodeURIComponent(entry.tournament_id || '')
                        + '&heat_id=' + encodeURIComponent(entry.heat_id || '');
                    const replayResp = await fetch('/scoring/api/replay', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                        body: replayBody,
                    });
                    if (replayResp.status >= 200 && replayResp.status < 300) {
                        await removeEntry(db, entry.id);
                        successCount++;
                    } else {
                        // Replay endpoint also rejected — keep in queue
                        failedCount++;
                        failReasons.push('csrf_expired');
                    }
                } else {
                    // No replay token — keep in queue, notify user
                    failedCount++;
                    failReasons.push('csrf_expired');
                }
            } else if (resp.status === 409) {
                // Version conflict — another judge already updated this heat.
                // KEEP in queue — do NOT dequeue. User must resolve manually.
                failedCount++;
                failReasons.push('version_conflict');
            } else if (resp.status >= 500) {
                // Server error — transient, retry later. Keep in queue.
                // Do not increment failedCount — this is retryable.
            } else {
                // Other 4xx (404, 422, etc.) — keep in queue, flag as failed
                failedCount++;
                failReasons.push('server_rejected_' + resp.status);
            }
        } catch (_) {
            // Network error — still offline. Leave in queue for next attempt.
        }
    }

    // Notify all open windows of results
    const clients = await self.clients.matchAll({ type: 'window' });
    if (successCount > 0 || failedCount > 0) {
        clients.forEach(client =>
            client.postMessage({
                type: 'sync-complete',
                success: successCount,
                failed: failedCount,
                reasons: failReasons,
            })
        );
    }
    if (failedCount > 0) {
        clients.forEach(client =>
            client.postMessage({
                type: 'replay-failed',
                count: failedCount,
                reasons: failReasons,
            })
        );
    }
}
