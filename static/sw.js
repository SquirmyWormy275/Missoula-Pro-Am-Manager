/**
 * Missoula Pro-Am Service Worker
 * Caches scoring entry pages and queues POST submissions when offline.
 * Replays the queue via Background Sync when connectivity is restored.
 */

const CACHE_NAME = 'proam-scoring-v1';
const SCORE_ENTRY_PATTERN = /\/scoring\/\d+\/heat\/\d+\/enter/;

// ── Install: cache the offline fallback ─────────────────────────────────────
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache =>
            cache.addAll(['/static/offline.html'])
                 .catch(() => {/* offline.html optional */})
        )
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(self.clients.claim());
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

// ── Fetch handler ────────────────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
    const req = event.request;

    // Intercept POST to score entry when offline
    if (req.method === 'POST' && SCORE_ENTRY_PATTERN.test(req.url)) {
        event.respondWith(handleScorePost(req));
        return;
    }

    // Network-first for GET score entry pages (cache for offline fallback)
    if (req.method === 'GET' && SCORE_ENTRY_PATTERN.test(req.url)) {
        event.respondWith(
            fetch(req).then((resp) => {
                const clone = resp.clone();
                caches.open(CACHE_NAME).then(cache => cache.put(req, clone));
                return resp;
            }).catch(() => caches.match(req))
        );
        return;
    }
    // All other requests: default browser handling
});

async function handleScorePost(request) {
    // Try network first
    try {
        return await fetch(request.clone());
    } catch (_networkErr) {
        // Offline — queue the submission
        const body = await request.text();
        const db = await openDB();
        await enqueue(db, {
            url: request.url,
            method: 'POST',
            body,
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
    let syncedCount = 0;

    for (const entry of entries) {
        try {
            const resp = await fetch(entry.url, {
                method: entry.method,
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: entry.body,
            });
            // Remove from queue if server accepted (2xx or 4xx = not a network error)
            if (resp.status < 500) {
                await removeEntry(db, entry.id);
                syncedCount++;
            }
        } catch (_) {
            // Still offline — leave in queue
        }
    }

    if (syncedCount > 0) {
        // Notify all open windows
        const clients = await self.clients.matchAll({ type: 'window' });
        clients.forEach(client =>
            client.postMessage({ type: 'sync-complete', count: syncedCount })
        );
    }
}
