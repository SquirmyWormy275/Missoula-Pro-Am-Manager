/* Shared exactly-once queue helpers for offline scoring pages. */
(function () {
    'use strict';

    var KEY = 'proam_heat_score_queue_v1';
    var LOCK_PREFIX = KEY + '_mutation_participant_v1:';
    var LOCK_NAME = KEY + '_mutation';
    var SCHEMA_VERSION = 2;
    var MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
    var LOCK_WAIT_MS = 5000;
    var LOCK_LEASE_MS = 10000;
    var MAX_MUTATION_ATTEMPTS = 5;
    var TRANSPORT_FIELDS = {
        csrf_token: true,
        replay_token: true,
        payload_sha256: true,
        tournament_id: true,
        heat_id: true
    };

    function QueueError(code, message, httpStatus, data) {
        this.name = 'QueueError';
        this.code = code;
        this.message = message || code;
        this.httpStatus = httpStatus || 0;
        this.data = data || null;
    }
    QueueError.prototype = Object.create(Error.prototype);
    QueueError.prototype.constructor = QueueError;

    function uuid() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        var bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 15) | 64;
        bytes[8] = (bytes[8] & 63) | 128;
        var hex = Array.prototype.map.call(bytes, function (value) {
            return value.toString(16).padStart(2, '0');
        }).join('');
        return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16),
            hex.slice(16, 20), hex.slice(20)].join('-');
    }

    function read() {
        try {
            var parsed = JSON.parse(localStorage.getItem(KEY) || '[]');
            return Array.isArray(parsed) ? parsed : [];
        } catch (_err) {
            return [];
        }
    }

    function writeRaw(queue) {
        localStorage.setItem(KEY, JSON.stringify(queue));
    }

    function wait(milliseconds) {
        return new Promise(function (resolve) {
            setTimeout(resolve, milliseconds);
        });
    }

    function storageParticipants() {
        var participants = [];
        var expired = [];
        for (var index = 0; index < localStorage.length; index += 1) {
            var key = localStorage.key(index);
            if (!key || key.indexOf(LOCK_PREFIX) !== 0) continue;
            try {
                var participant = JSON.parse(localStorage.getItem(key) || 'null');
                if (!participant || Number(participant.expires_at || 0) <= Date.now()) {
                    expired.push(key);
                } else {
                    participants.push(participant);
                }
            } catch (_error) {
                expired.push(key);
            }
        }
        expired.forEach(function (key) { localStorage.removeItem(key); });
        return participants;
    }

    async function withStorageTicketLock(callback) {
        var owner = uuid();
        var participantKey = LOCK_PREFIX + owner;
        var deadline = Date.now() + LOCK_WAIT_MS;
        localStorage.setItem(participantKey, JSON.stringify({
            owner: owner,
            choosing: true,
            ticket: 0,
            expires_at: Date.now() + LOCK_LEASE_MS
        }));
        var ticket = storageParticipants().reduce(function (highest, participant) {
            return Math.max(highest, Number(participant.ticket || 0));
        }, 0) + 1;
        localStorage.setItem(participantKey, JSON.stringify({
            owner: owner,
            choosing: false,
            ticket: ticket,
            expires_at: Date.now() + LOCK_LEASE_MS
        }));
        try {
            while (Date.now() < deadline) {
                var blocked = storageParticipants().some(function (participant) {
                    if (participant.owner === owner) return false;
                    if (participant.choosing) return true;
                    var otherTicket = Number(participant.ticket || 0);
                    return otherTicket < ticket ||
                        (otherTicket === ticket && String(participant.owner) < owner);
                });
                if (!blocked) return await callback();
                await wait(20 + Math.floor(Math.random() * 30));
            }
            throw new QueueError(
                'queue_lock_timeout',
                'Another tab is updating the offline queue. Try again.'
            );
        } finally {
            localStorage.removeItem(participantKey);
        }
    }

    function withQueueLock(callback) {
        if (navigator.locks && typeof navigator.locks.request === 'function') {
            return navigator.locks.request(
                LOCK_NAME,
                {mode: 'exclusive'},
                callback
            );
        }
        return withStorageTicketLock(callback);
    }

    function entryKey(entry, index) {
        if (entry && entry.request_id) return 'request:' + String(entry.request_id);
        return [
            'legacy',
            String((entry && entry.url) || ''),
            String((entry && entry.heat_id) || ''),
            String((entry && (entry.queued_at || entry.saved_at)) || ''),
            String(index || 0)
        ].join(':');
    }

    function normalizeQueue(queue) {
        var normalized = [];
        var positions = {};
        (Array.isArray(queue) ? queue : []).forEach(function (entry, index) {
            var key = entryKey(entry, index);
            if (Object.prototype.hasOwnProperty.call(positions, key)) {
                normalized[positions[key]] = entry;
            } else {
                positions[key] = normalized.length;
                normalized.push(entry);
            }
        });
        return normalized;
    }

    function sameEntry(left, right) {
        return JSON.stringify(left) === JSON.stringify(right);
    }

    function containsExpected(queue, expected) {
        return expected.every(function (entry, index) {
            var key = entryKey(entry, index);
            return queue.some(function (candidate, candidateIndex) {
                return entryKey(candidate, candidateIndex) === key &&
                    sameEntry(candidate, entry);
            });
        });
    }

    async function mutateQueue(mutator, verifier) {
        return withQueueLock(async function () {
            for (var attempt = 0; attempt < MAX_MUTATION_ATTEMPTS; attempt += 1) {
                var next = normalizeQueue(mutator(read().slice()));
                writeRaw(next);
                var committed = read();
                if (containsExpected(committed, next) && verifier(committed, next)) {
                    return committed;
                }
                await wait(0);
            }
            throw new QueueError(
                'queue_write_conflict',
                'Another tab changed the offline queue before this update was verified.'
            );
        });
    }

    function canonicalPairs(payload, tournamentId, heatId) {
        var pairs = [];
        Object.keys(payload || {}).forEach(function (key) {
            if (TRANSPORT_FIELDS[key]) return;
            var value = payload[key];
            if (Array.isArray(value)) {
                value.forEach(function (item) {
                    pairs.push([String(key), String(item || '')]);
                });
            } else {
                pairs.push([String(key), String(value || '')]);
            }
        });
        pairs.push(['heat_id', String(Number(heatId))]);
        pairs.push(['tournament_id', String(Number(tournamentId))]);
        pairs.sort(function (left, right) {
            if (left[0] < right[0]) return -1;
            if (left[0] > right[0]) return 1;
            if (left[1] < right[1]) return -1;
            if (left[1] > right[1]) return 1;
            return 0;
        });
        return pairs;
    }

    async function sha256(value) {
        var encoded = new TextEncoder().encode(value);
        var digest = await window.crypto.subtle.digest('SHA-256', encoded);
        return Array.prototype.map.call(new Uint8Array(digest), function (byte) {
            return byte.toString(16).padStart(2, '0');
        }).join('');
    }

    async function payloadFingerprint(payload, tournamentId, heatId) {
        return sha256(JSON.stringify(canonicalPairs(payload, tournamentId, heatId)));
    }

    async function prepareEntry(entry, context) {
        var prepared = Object.assign({}, entry || {});
        var sourcePayload = Object.assign({}, prepared.payload || {});
        prepared.schema_version = SCHEMA_VERSION;
        // A cached offline page contains the request ID minted when the package
        // was prepared. Treat that hidden value as a no-JS fallback only: each
        // new JS submission is a new action. A queued retry already carries
        // request_id on the entry itself and therefore preserves its receipt
        // identity across reconnects and lost responses.
        prepared.request_id = prepared.request_id || uuid();
        prepared.tournament_id = Number(
            prepared.tournament_id || sourcePayload.tournament_id || context.tournament_id
        );
        prepared.heat_id = Number(prepared.heat_id || sourcePayload.heat_id || 0);
        prepared.issuer_user_id = Number(
            prepared.issuer_user_id || sourcePayload.issuer_user_id || context.issuer_user_id
        );
        prepared.issuer_role = prepared.issuer_role || sourcePayload.issuer_role ||
            context.issuer_role;
        prepared.schedule_fingerprint = prepared.schedule_fingerprint ||
            sourcePayload.schedule_fingerprint || context.schedule_fingerprint;
        prepared.application_build = prepared.application_build ||
            sourcePayload.application_build || context.application_build;
        prepared.queued_at = prepared.queued_at || sourcePayload.queued_at ||
            new Date().toISOString();
        prepared.payload = sourcePayload;
        prepared.payload.request_id = prepared.request_id;
        prepared.payload.tournament_id = String(prepared.tournament_id);
        prepared.payload.heat_id = String(prepared.heat_id);
        prepared.payload.issuer_user_id = String(prepared.issuer_user_id);
        prepared.payload.issuer_role = prepared.issuer_role;
        prepared.payload.schedule_fingerprint = prepared.schedule_fingerprint;
        prepared.payload.application_build = prepared.application_build;
        prepared.payload.queued_at = prepared.queued_at;
        prepared.payload_sha256 = await payloadFingerprint(
            prepared.payload,
            prepared.tournament_id,
            prepared.heat_id
        );
        prepared.payload.payload_sha256 = prepared.payload_sha256;
        return prepared;
    }

    function upsert(entry) {
        return mutateQueue(function (queue) {
            queue = queue.filter(function (item) {
                return String(item.request_id || '') !== String(entry.request_id || '');
            });
            queue.push(entry);
            return queue;
        }, function (committed) {
            return committed.some(function (item) {
                return String(item.request_id || '') === String(entry.request_id || '') &&
                    sameEntry(item, entry);
            });
        });
    }

    function remove(url, heatId) {
        return mutateQueue(function (queue) {
            return queue.filter(function (item) {
                return !(
                    String(item.url || '') === String(url || '') &&
                    Number(item.heat_id || 0) === Number(heatId || 0)
                );
            });
        }, function (committed) {
            return !committed.some(function (item) {
                return String(item.url || '') === String(url || '') &&
                    Number(item.heat_id || 0) === Number(heatId || 0);
            });
        });
    }

    function removeByRequestId(requestId) {
        return mutateQueue(function (queue) {
            return queue.filter(function (item) {
                return String(item.request_id || '') !== String(requestId || '');
            });
        }, function (committed) {
            return !committed.some(function (item) {
                return String(item.request_id || '') === String(requestId || '');
            });
        });
    }

    function removeTournament(tournamentId) {
        return mutateQueue(function (queue) {
            return queue.filter(function (item) {
                return Number(item.tournament_id || 0) !== Number(tournamentId || 0);
            });
        }, function (committed) {
            return !committed.some(function (item) {
                return Number(item.tournament_id || 0) === Number(tournamentId || 0);
            });
        });
    }

    function merge(entries) {
        return mutateQueue(function (queue) {
            var known = {};
            queue.forEach(function (item, index) {
                known[entryKey(item, index)] = true;
            });
            (Array.isArray(entries) ? entries : []).forEach(function (entry, index) {
                var key = entryKey(entry, index);
                if (!known[key]) {
                    known[key] = true;
                    queue.push(entry);
                }
            });
            return queue;
        }, function (committed) {
            return (Array.isArray(entries) ? entries : []).every(function (entry, index) {
                var key = entryKey(entry, index);
                return committed.some(function (candidate, candidateIndex) {
                    return entryKey(candidate, candidateIndex) === key;
                });
            });
        });
    }

    function find(url, heatId) {
        return read().find(function (item) {
            return String(item.url || '') === String(url || '') &&
                Number(item.heat_id || 0) === Number(heatId || 0);
        });
    }

    function findByRequestId(requestId) {
        return read().find(function (item) {
            return String(item.request_id || '') === String(requestId || '');
        });
    }

    function byTournament(tournamentId) {
        return read().filter(function (item) {
            return Number(item.tournament_id || 0) === Number(tournamentId || 0);
        });
    }

    function ageState(entry, now) {
        var queuedAt = Date.parse(entry.queued_at || '');
        if (!Number.isFinite(queuedAt)) return 'queued_at_invalid';
        if ((now || Date.now()) - queuedAt > MAX_AGE_MS) {
            return 'manual_reconciliation_required';
        }
        return 'ready';
    }

    function bindingState(entry, context) {
        if (!entry.issuer_user_id || !entry.issuer_role || !entry.schedule_fingerprint) {
            return 'legacy_unbound';
        }
        if (Number(entry.tournament_id) !== Number(context.tournament_id)) {
            return 'tournament_mismatch';
        }
        if (Number(entry.issuer_user_id) !== Number(context.issuer_user_id)) {
            return 'issuer_mismatch';
        }
        if (String(entry.issuer_role) !== String(context.issuer_role)) {
            return 'role_mismatch';
        }
        if (String(entry.schedule_fingerprint) !== String(context.schedule_fingerprint)) {
            return 'stale_manifest';
        }
        return 'ready';
    }

    function verifyReceipt(entry, receipt) {
        return Boolean(
            receipt && receipt.accepted === true &&
            String(receipt.request_id || '') === String(entry.request_id || '') &&
            Number(receipt.tournament_id || 0) === Number(entry.tournament_id || 0) &&
            Number(receipt.heat_id || 0) === Number(entry.heat_id || 0) &&
            Number(receipt.issuing_user_id || 0) === Number(entry.issuer_user_id || 0) &&
            String(receipt.payload_sha256 || '') === String(entry.payload_sha256 || '')
        );
    }

    async function removeVerified(entry, receipt) {
        if (!verifyReceipt(entry, receipt)) {
            throw new QueueError(
                'receipt_mismatch',
                'The server receipt does not match this queued score.'
            );
        }
        return removeByRequestId(entry.request_id);
    }

    function responseCode(data, fallback) {
        return data && data.error && data.error.code ? data.error.code : fallback;
    }

    async function verifiedJson(response, entry) {
        if (response.redirected) {
            throw new QueueError(
                'session_required',
                'Sign in as the queue issuer before syncing.',
                response.status
            );
        }
        var contentType = response.headers.get('Content-Type') || '';
        if (contentType.toLowerCase().indexOf('application/json') === -1) {
            throw new QueueError(
                'unexpected_response',
                'The server returned HTML instead of a score receipt.',
                response.status
            );
        }
        var data;
        try {
            data = await response.json();
        } catch (_error) {
            throw new QueueError(
                'invalid_json',
                'The server response was not valid JSON.',
                response.status
            );
        }
        if (!response.ok || !data.ok) {
            throw new QueueError(
                responseCode(data, 'server_rejected_' + response.status),
                data.message || 'The server rejected this queued score.',
                response.status,
                data
            );
        }
        if (!verifyReceipt(entry, data.receipt)) {
            throw new QueueError(
                'receipt_mismatch',
                'The server accepted a different score request.',
                response.status,
                data
            );
        }
        return data;
    }

    function formBody(payload) {
        var body = new FormData();
        Object.keys(payload || {}).forEach(function (key) {
            body.append(key, payload[key]);
        });
        return body;
    }

    async function post(url, payload) {
        return fetch(url, {
            method: 'POST',
            body: formBody(payload),
            headers: {
                'Accept': 'application/json',
                'X-Requested-With': 'XMLHttpRequest'
            },
            credentials: 'same-origin',
            redirect: 'follow'
        });
    }

    async function renewReplayToken(entry, context) {
        if (bindingState(entry, context) !== 'ready') {
            throw new QueueError(
                'authorization_denied',
                'Only the original authorized user can renew replay authority.'
            );
        }
        var response = await fetch('/scoring/api/replay-token', {
            headers: {'Accept': 'application/json'},
            credentials: 'same-origin'
        });
        if (response.redirected ||
                (response.headers.get('Content-Type') || '').indexOf('application/json') === -1) {
            throw new QueueError('session_required', 'Sign in before syncing.', response.status);
        }
        var data = await response.json();
        if (!response.ok || !data.ok ||
                Number(data.issuer_user_id) !== Number(entry.issuer_user_id) ||
                String(data.issuer_role) !== String(entry.issuer_role)) {
            throw new QueueError(
                responseCode(data, 'authorization_denied'),
                data.message || 'Replay authority could not be renewed.',
                response.status,
                data
            );
        }
        entry.payload.replay_token = data.replay_token;
        await upsert(entry);
        return entry;
    }

    async function replay(entry, context) {
        if (!entry.payload.replay_token) await renewReplayToken(entry, context);
        var response = await post('/scoring/api/replay', entry.payload);
        try {
            return await verifiedJson(response, entry);
        } catch (error) {
            if (error.code !== 'replay_token_expired' &&
                    error.code !== 'replay_token_required') throw error;
            await renewReplayToken(entry, context);
            response = await post('/scoring/api/replay', entry.payload);
            return verifiedJson(response, entry);
        }
    }

    async function syncEntry(rawEntry, context) {
        var age = ageState(rawEntry);
        if (age !== 'ready') {
            throw new QueueError(age, 'This queued score requires manual reconciliation.');
        }
        var binding = bindingState(rawEntry, context);
        if (binding !== 'ready') {
            throw new QueueError(binding, 'This queued score cannot sync in the current context.');
        }
        var entry = await prepareEntry(rawEntry, context);
        await upsert(entry);
        var response = await post(entry.url, entry.payload);
        try {
            return {entry: entry, data: await verifiedJson(response, entry)};
        } catch (error) {
            if (error.code !== 'csrf_expired') throw error;
            return {entry: entry, data: await replay(entry, context)};
        }
    }

    window.ProAmOfflineQueue = {
        key: KEY,
        schemaVersion: SCHEMA_VERSION,
        maxAgeMs: MAX_AGE_MS,
        read: read,
        upsert: upsert,
        merge: merge,
        remove: remove,
        removeByRequestId: removeByRequestId,
        removeTournament: removeTournament,
        removeVerified: removeVerified,
        find: find,
        findByRequestId: findByRequestId,
        byTournament: byTournament,
        ageState: ageState,
        bindingState: bindingState,
        prepareEntry: prepareEntry,
        payloadFingerprint: payloadFingerprint,
        verifyReceipt: verifyReceipt,
        mutateQueue: mutateQueue,
        syncEntry: syncEntry,
        QueueError: QueueError
    };
}());
