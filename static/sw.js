/** Missoula Pro-Am prepared offline cache and legacy queue drain. */

const CACHE_PREFIX = 'proam-scoring-';
const CACHE_NAME = 'proam-scoring-v2';
const OFFLINE_FALLBACK = '/static/offline.html';
const SCORE_ENTRY_PATTERN = /\/scoring\/\d+\/heat\/\d+\/enter/;
const LOGOUT_PATTERN = /\/auth\/logout\/?$/;
const PREPARED_CACHE_PREFIX = 'proam-prepared-pages-v2-';
const PREPARED_META_CACHE = 'proam-prepared-meta-v2';
const PREPARED_META_URL = '/__proam_prepared_manifest__';
const LEGACY_DB_NAME = 'proam-offline-queue';
const LEGACY_STORE_NAME = 'queue';
const MAX_REPLAY_AGE_MS = 30 * 24 * 60 * 60 * 1000;

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache =>
            cache.addAll([OFFLINE_FALLBACK])
        ).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const names = await caches.keys();
        await Promise.all(names.filter((name) =>
            (name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME) ||
            name.startsWith('proam-prepared-pages-v1-') ||
            name === 'proam-prepared-meta-v1'
        ).map((name) => caches.delete(name)));
        await self.clients.claim();
    })());
});

function sameOrigin(url) {
    return new URL(url, self.location.origin).origin === self.location.origin;
}

function isCacheableScorePage(response, request) {
    const contentType = response.headers.get('content-type') || '';
    return response.ok
        && !response.redirected
        && response.url === request.url
        && contentType.includes('text/html');
}

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method === 'POST' && LOGOUT_PATTERN.test(request.url)) {
        event.respondWith(handleLogout(request));
        return;
    }
    if (request.method !== 'GET' || !sameOrigin(request.url)) return;
    event.respondWith(networkFirstPreparedFallback(request));
});

async function handleLogout(request) {
    const response = await fetch(request);
    if (response.ok || response.redirected) await clearPreparedPackage();
    return response;
}

async function networkFirstPreparedFallback(request) {
    try {
        const response = await fetch(request);
        if (SCORE_ENTRY_PATTERN.test(request.url) &&
                isCacheableScorePage(response, request)) {
            try {
                const cache = await caches.open(CACHE_NAME);
                await cache.put(request, response.clone());
            } catch (_cacheErr) {
                // A quota/cache failure must never replace a valid live page.
            }
        }
        return response;
    } catch (networkError) {
        const manifest = await readPreparedManifest();
        if (manifest && manifest.cache_name) {
            const preparedCache = await caches.open(manifest.cache_name);
            const prepared = await preparedCache.match(request, {ignoreSearch: false});
            if (prepared) return prepared;
        }

        if (SCORE_ENTRY_PATTERN.test(request.url)) {
            try {
                const cache = await caches.open(CACHE_NAME);
                const cachedPage = await cache.match(request);
                if (cachedPage) return cachedPage;

                const fallback = await cache.match(OFFLINE_FALLBACK);
                if (fallback) return fallback;
            } catch (_cacheReadErr) {
                // Fall through to an explicit bounded response.
            }

            return new Response('Offline score page unavailable', {
                status: 503,
                headers: {'Content-Type': 'text/plain; charset=utf-8'}
            });
        }
        throw networkError;
    }
}

async function sha256Bytes(value) {
    const digest = await crypto.subtle.digest('SHA-256', value);
    return Array.from(new Uint8Array(digest), (byte) =>
        byte.toString(16).padStart(2, '0')
    ).join('');
}

async function sha256(value) {
    return sha256Bytes(new TextEncoder().encode(value));
}

function contextMatches(manifest, context) {
    return Boolean(manifest && context &&
        Number(manifest.schema_version) === Number(context.schema_version) &&
        String(manifest.application_build) === String(context.application_build) &&
        String(manifest.schedule_fingerprint) === String(context.schedule_fingerprint) &&
        Number(manifest.tournament_id) === Number(context.tournament_id) &&
        Number(manifest.issuer_user_id) === Number(context.issuer_user_id) &&
        String(manifest.issuer_role) === String(context.issuer_role));
}

async function readPreparedManifest() {
    const cache = await caches.open(PREPARED_META_CACHE);
    const response = await cache.match(PREPARED_META_URL);
    if (!response) return null;
    try {
        return await response.json();
    } catch (_error) {
        return null;
    }
}

async function writePreparedManifest(manifest) {
    const cache = await caches.open(PREPARED_META_CACHE);
    await cache.put(PREPARED_META_URL, new Response(JSON.stringify(manifest), {
        headers: {'Content-Type': 'application/json'}
    }));
}

async function clearPreparedPackage() {
    const names = await caches.keys();
    await Promise.all(names.filter((name) =>
        name.startsWith(PREPARED_CACHE_PREFIX)
    ).map((name) => caches.delete(name)));
    await caches.delete(PREPARED_META_CACHE);
}

async function verifyManifestRows(manifest) {
    const rows = [].concat(manifest.pages || [], manifest.assets || []);
    for (const row of rows) {
        if (!row.url || !sameOrigin(row.url)) return false;
        if (row.kind === 'asset' &&
                !/^[a-f0-9]{64}$/.test(String(row.content_sha256 || ''))) return false;
        if (row.kind === 'page' &&
                (!row.fetch_url || !sameOrigin(row.fetch_url))) return false;
        if (row.kind !== 'asset' && row.kind !== 'page') return false;
    }
    return rows.length > 0;
}

async function verifyFetchedRow(manifest, row, response) {
    const contentDigest = await sha256Bytes(await response.clone().arrayBuffer());
    if (row.kind === 'asset') {
        if (contentDigest !== String(row.content_sha256)) {
            const error = new Error('content_digest_mismatch');
            error.code = 'content_digest_mismatch';
            throw error;
        }
        return contentDigest;
    }

    const expectedHeaders = {
        'X-ProAm-Offline-Build': String(manifest.application_build),
        'X-ProAm-Offline-Schedule': String(manifest.schedule_fingerprint),
        'X-ProAm-Offline-Tournament': String(manifest.tournament_id),
        'X-ProAm-Offline-Issuer': String(manifest.issuer_user_id),
        'X-ProAm-Offline-Role': String(manifest.issuer_role)
    };
    for (const [name, expected] of Object.entries(expectedHeaders)) {
        if (String(response.headers.get(name) || '') !== expected) {
            const error = new Error('prepared_context_mismatch');
            error.code = 'prepared_context_mismatch';
            throw error;
        }
    }
    if (String(response.headers.get('X-ProAm-Offline-Content-SHA256') || '') !==
            contentDigest) {
        const error = new Error('content_digest_mismatch');
        error.code = 'content_digest_mismatch';
        throw error;
    }
    return contentDigest;
}

async function verifyPreparedCache(manifest) {
    if (!manifest || !manifest.cache_name || !manifest.cached_content_digests) {
        return false;
    }
    const cache = await caches.open(manifest.cache_name);
    const rows = [].concat(manifest.assets || [], manifest.pages || []);
    for (const row of rows) {
        const response = await cache.match(new Request(row.url, {method: 'GET'}), {
            ignoreSearch: false
        });
        const expected = manifest.cached_content_digests[row.url];
        if (!response || !expected) return false;
        const actual = await sha256Bytes(await response.arrayBuffer());
        if (actual !== expected) return false;
    }
    return true;
}

function cacheNameFor(manifest) {
    return PREPARED_CACHE_PREFIX + [
        manifest.tournament_id,
        manifest.issuer_user_id,
        String(manifest.application_build).slice(0, 16),
        String(manifest.schedule_fingerprint).slice(0, 16)
    ].join('-').replace(/[^a-zA-Z0-9_-]/g, '_');
}

async function prepareOfflinePackage(manifest, source) {
    if (!manifest || Number(manifest.schema_version) !== 2 ||
            !Array.isArray(manifest.pages) || !Array.isArray(manifest.assets) ||
            !await verifyManifestRows(manifest)) {
        throw new Error('Invalid prepared-offline manifest.');
    }

    const previous = await readPreparedManifest();
    if (previous && !contextMatches(previous, manifest)) {
        await clearPreparedPackage();
    }

    const cacheName = cacheNameFor(manifest);
    await caches.delete(cacheName);
    const cache = await caches.open(cacheName);
    const rows = [].concat(manifest.assets, manifest.pages);
    const failures = [];
    const cachedContentDigests = {};
    let completed = 0;

    for (const row of rows) {
        try {
            const response = await fetch(row.fetch_url || row.url, {
                credentials: 'include',
                redirect: 'follow',
                cache: 'no-store',
                headers: {'X-Offline-Prepare': '1'}
            });
            const contentType = response.headers.get('Content-Type') || '';
            const isPage = row.kind === 'page';
            if (!response.ok || response.redirected ||
                    (isPage && contentType.indexOf('text/html') === -1)) {
                throw new Error('HTTP ' + response.status);
            }
            cachedContentDigests[row.url] = await verifyFetchedRow(
                manifest, row, response
            );
            await cache.put(new Request(row.url, {method: 'GET'}), response.clone());
            completed += 1;
        } catch (error) {
            failures.push({url: row.url, error: String(error.message || error)});
        }
        if (source) {
            source.postMessage({
                type: 'prepare-offline-progress',
                completed: completed,
                attempted: completed + failures.length,
                total: rows.length,
                failures: failures.slice()
            });
        }
    }

    if (failures.length) {
        await caches.delete(cacheName);
        return {
            state: 'incomplete',
            completed: completed,
            total: rows.length,
            failures: failures
        };
    }

    const names = await caches.keys();
    await Promise.all(names.filter((name) =>
        name.startsWith(PREPARED_CACHE_PREFIX) && name !== cacheName
    ).map((name) => caches.delete(name)));
    const stored = Object.assign({}, manifest, {
        cache_name: cacheName,
        prepared_at: new Date().toISOString(),
        completed: completed,
        total: rows.length,
        cached_content_digests: cachedContentDigests
    });
    await writePreparedManifest(stored);
    return Object.assign({state: 'ready'}, stored);
}

async function preparedStatus(context) {
    const manifest = await readPreparedManifest();
    if (!manifest) return {state: 'not_prepared'};
    if (!contextMatches(manifest, context)) {
        await clearPreparedPackage();
        return {state: 'invalidated'};
    }
    if (!await verifyPreparedCache(manifest)) {
        await clearPreparedPackage();
        return {state: 'invalidated'};
    }
    return Object.assign({state: 'ready'}, manifest);
}

function openLegacyDB() {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(LEGACY_DB_NAME, 1);
        request.onupgradeneeded = (event) => {
            const database = event.target.result;
            if (!database.objectStoreNames.contains(LEGACY_STORE_NAME)) {
                database.createObjectStore(LEGACY_STORE_NAME, {
                    keyPath: 'id', autoIncrement: true
                });
            }
        };
        request.onsuccess = (event) => resolve(event.target.result);
        request.onerror = (event) => reject(event.target.error);
    });
}

function legacyEntries(database) {
    return new Promise((resolve, reject) => {
        const transaction = database.transaction(LEGACY_STORE_NAME, 'readonly');
        const request = transaction.objectStore(LEGACY_STORE_NAME).getAll();
        request.onsuccess = (event) => resolve(event.target.result);
        request.onerror = (event) => reject(event.target.error);
    });
}

function updateLegacyEntry(database, entry) {
    return new Promise((resolve, reject) => {
        const transaction = database.transaction(LEGACY_STORE_NAME, 'readwrite');
        transaction.objectStore(LEGACY_STORE_NAME).put(entry);
        transaction.oncomplete = resolve;
        transaction.onerror = (event) => reject(event.target.error);
    });
}

function removeLegacyEntry(database, id) {
    return new Promise((resolve, reject) => {
        const transaction = database.transaction(LEGACY_STORE_NAME, 'readwrite');
        transaction.objectStore(LEGACY_STORE_NAME).delete(id);
        transaction.oncomplete = resolve;
        transaction.onerror = (event) => reject(event.target.error);
    });
}

function legacyUuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16),
        hex.slice(16, 20), hex.slice(20)].join('-');
}

async function canonicalLegacyFingerprint(params, tournamentId, heatId) {
    const ignored = new Set([
        'csrf_token', 'replay_token', 'payload_sha256', 'tournament_id', 'heat_id'
    ]);
    const pairs = [];
    params.forEach((value, key) => {
        if (!ignored.has(key)) pairs.push([String(key), String(value || '')]);
    });
    pairs.push(['heat_id', String(Number(heatId))]);
    pairs.push(['tournament_id', String(Number(tournamentId))]);
    pairs.sort((left, right) =>
        left[0].localeCompare(right[0]) || left[1].localeCompare(right[1])
    );
    return sha256(JSON.stringify(pairs));
}

async function prepareLegacyEntry(database, entry) {
    const params = new URLSearchParams(entry.body || '');
    const match = String(entry.url || '').match(/\/scoring\/(\d+)\/heat\/(\d+)\/enter/);
    const tournamentId = Number(entry.tournament_id || (match && match[1]) || 0);
    const heatId = Number(entry.heat_id || (match && match[2]) || 0);
    const issuerUserId = Number(
        entry.issuer_user_id || params.get('issuer_user_id') || 0
    );
    const issuerRole = String(
        entry.issuer_role || params.get('issuer_role') || ''
    ).trim();
    const hasBoundIssuer = Number.isInteger(issuerUserId) &&
        issuerUserId > 0 && Boolean(issuerRole);
    if (!params.get('request_id')) params.set('request_id', legacyUuid());
    if (!params.get('queued_at')) {
        params.set('queued_at', new Date(entry.timestamp || Date.now()).toISOString());
    }
    params.set('tournament_id', String(tournamentId));
    params.set('heat_id', String(heatId));
    if (hasBoundIssuer) {
        params.set('issuer_user_id', String(issuerUserId));
        params.set('issuer_role', issuerRole);
    }
    const fingerprint = await canonicalLegacyFingerprint(params, tournamentId, heatId);
    params.set('payload_sha256', fingerprint);
    entry.body = params.toString();
    entry.request_id = params.get('request_id');
    entry.payload_sha256 = fingerprint;
    entry.tournament_id = tournamentId;
    entry.heat_id = heatId;
    entry.issuer_user_id = hasBoundIssuer ? issuerUserId : null;
    entry.issuer_role = hasBoundIssuer ? issuerRole : null;
    entry.auto_replay = hasBoundIssuer;
    entry.reconciliation_state = hasBoundIssuer ? null : 'legacy_unbound_issuer';
    entry.timestamp = entry.timestamp || Date.now();
    await updateLegacyEntry(database, entry);
    return {entry, params, replayable: hasBoundIssuer};
}

async function legacyJson(response) {
    if (response.redirected) throw new Error('session_required');
    const contentType = response.headers.get('Content-Type') || '';
    if (contentType.indexOf('application/json') === -1) {
        throw new Error('unexpected_response');
    }
    const data = await response.json();
    if (!response.ok || !data.ok) {
        const code = data && data.error && data.error.code;
        const error = new Error(code || 'server_rejected_' + response.status);
        error.code = code;
        throw error;
    }
    return data;
}

function matchingLegacyReceipt(entry, receipt) {
    return Boolean(receipt && receipt.accepted === true &&
        String(receipt.request_id) === String(entry.request_id) &&
        Number(receipt.tournament_id) === Number(entry.tournament_id) &&
        Number(receipt.heat_id) === Number(entry.heat_id) &&
        Number(receipt.issuing_user_id) === Number(entry.issuer_user_id) &&
        String(receipt.payload_sha256) === String(entry.payload_sha256));
}

async function postLegacy(url, params) {
    return fetch(url, {
        method: 'POST',
        headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'XMLHttpRequest'
        },
        credentials: 'same-origin',
        redirect: 'follow',
        body: params.toString()
    });
}

async function renewLegacyReplayToken(database, entry, params) {
    const response = await fetch('/scoring/api/replay-token', {
        method: 'GET',
        headers: {'Accept': 'application/json'},
        credentials: 'same-origin',
        redirect: 'follow',
        cache: 'no-store'
    });
    const data = await legacyJson(response);
    if (Number(data.issuer_user_id) !== Number(entry.issuer_user_id) ||
            String(data.issuer_role) !== String(entry.issuer_role)) {
        const error = new Error('legacy_issuer_session_mismatch');
        error.code = 'legacy_issuer_session_mismatch';
        throw error;
    }
    params.set('replay_token', String(data.replay_token || ''));
    if (!params.get('replay_token')) throw new Error('replay_token_required');
    entry.body = params.toString();
    await updateLegacyEntry(database, entry);
    return data.replay_token;
}

async function replayLegacyEntry(database, rawEntry) {
    const prepared = await prepareLegacyEntry(database, rawEntry);
    const entry = prepared.entry;
    const params = prepared.params;
    if (!prepared.replayable) {
        const error = new Error('legacy_unbound_issuer');
        error.code = 'manual_reconciliation_required';
        throw error;
    }
    if (Date.now() - Number(entry.timestamp || 0) > MAX_REPLAY_AGE_MS) {
        throw new Error('manual_reconciliation_required');
    }

    let response = await postLegacy(entry.url, params);
    let data;
    try {
        data = await legacyJson(response);
    } catch (error) {
        if (error.code !== 'csrf_expired') throw error;
        if (!params.get('replay_token')) {
            await renewLegacyReplayToken(database, entry, params);
        }
        response = await postLegacy('/scoring/api/replay', params);
        try {
            data = await legacyJson(response);
        } catch (replayError) {
            if (replayError.code !== 'replay_token_expired') throw replayError;
            await renewLegacyReplayToken(database, entry, params);
            response = await postLegacy('/scoring/api/replay', params);
            data = await legacyJson(response);
        }
    }
    if (!matchingLegacyReceipt(entry, data.receipt)) {
        throw new Error('receipt_mismatch');
    }
    await removeLegacyEntry(database, entry.id);
    return true;
}

async function legacyQueueStatus() {
    const database = await openLegacyDB();
    const entries = await legacyEntries(database);
    const prepared = [];
    for (const entry of entries) {
        prepared.push((await prepareLegacyEntry(database, entry)).entry);
    }
    database.close();
    return {
        count: prepared.length,
        manual_reconciliation_required: prepared.filter((entry) =>
            entry.reconciliation_state === 'legacy_unbound_issuer' ||
            Date.now() - Number(entry.timestamp || 0) > MAX_REPLAY_AGE_MS
        ).length
    };
}

async function replayLegacyQueue() {
    const database = await openLegacyDB();
    const entries = await legacyEntries(database);
    let success = 0;
    const reasons = [];
    for (const entry of entries) {
        if (!isSameOriginScoreEntryUrl(entry.url)) {
            failedCount++;
            failReasons.push('invalid_queue_target');
            continue;
        }
        try {
            await replayLegacyEntry(database, entry);
            success += 1;
        } catch (error) {
            reasons.push(String(error.code || error.message || error));
        }
    }
    database.close();
    const clients = await self.clients.matchAll({type: 'window'});
    clients.forEach((client) => client.postMessage({
        type: 'legacy-sync-complete',
        success: success,
        failed: reasons.length,
        reasons: reasons
    }));
    return {success, failed: reasons.length, reasons};
}

self.addEventListener('message', (event) => {
    const data = event.data || {};
    if (data.type === 'prepare-offline-package') {
        event.waitUntil(prepareOfflinePackage(data.manifest, event.source)
            .then((result) => event.source && event.source.postMessage({
                type: 'prepare-offline-complete', result: result
            }))
            .catch((error) => event.source && event.source.postMessage({
                type: 'prepare-offline-complete',
                result: {state: 'failed', error: String(error.message || error)}
            })));
    } else if (data.type === 'validate-prepared-package' ||
            data.type === 'prepared-package-status') {
        event.waitUntil(preparedStatus(data.context).then((result) =>
            event.source && event.source.postMessage({
                type: 'prepared-package-status', result: result
            })
        ));
    } else if (data.type === 'clear-prepared-package') {
        event.waitUntil(clearPreparedPackage().then(() =>
            event.source && event.source.postMessage({
                type: 'prepared-package-status', result: {state: 'not_prepared'}
            })
        ));
    } else if (data.type === 'legacy-queue-status') {
        event.waitUntil(legacyQueueStatus().then((result) =>
            event.source && event.source.postMessage({
                type: 'legacy-queue-status', result: result
            })
        ));
    } else if (data.type === 'drain-legacy-queue') {
        event.waitUntil(replayLegacyQueue());
    }
});
