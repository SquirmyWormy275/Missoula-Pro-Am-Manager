'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const {webcrypto} = require('crypto');

const source = fs.readFileSync('static/sw.js', 'utf8') + `
globalThis.__swTest = {
    sha256Bytes,
    verifyFetchedRow,
    renewLegacyReplayToken,
    replayLegacyEntry,
    setHooks(hooks) {
        if (hooks.prepareLegacyEntry) prepareLegacyEntry = hooks.prepareLegacyEntry;
        if (hooks.postLegacy) postLegacy = hooks.postLegacy;
        if (hooks.updateLegacyEntry) updateLegacyEntry = hooks.updateLegacyEntry;
        if (hooks.removeLegacyEntry) removeLegacyEntry = hooks.removeLegacyEntry;
    }
};`;

const context = vm.createContext({
    URL,
    URLSearchParams,
    Request,
    Response,
    TextEncoder,
    crypto: webcrypto,
    console,
    setTimeout,
    clearTimeout,
    self: {
        location: {origin: 'https://proam.test'},
        addEventListener() {},
        skipWaiting: async () => {},
        clients: {claim: async () => {}, matchAll: async () => []}
    },
    caches: {
        delete: async () => true,
        keys: async () => [],
        open: async () => ({match: async () => null, put: async () => {}})
    },
    indexedDB: {}
});
context.globalThis = context;
vm.runInContext(source, context, {filename: 'static/sw.js'});

async function expectRejects(promise, expected) {
    let error;
    try {
        await promise;
    } catch (caught) {
        error = caught;
    }
    assert(error, 'expected promise to reject');
    assert.strictEqual(error.code || error.message, expected);
}

async function run() {
    const api = context.__swTest;
    const assetBody = 'verified asset bytes';
    const assetDigest = await api.sha256Bytes(
        new TextEncoder().encode(assetBody).buffer
    );
    const manifest = {
        application_build: 'build-123',
        schedule_fingerprint: 'schedule-456',
        tournament_id: 9,
        issuer_user_id: 7,
        issuer_role: 'judge'
    };
    const asset = {
        kind: 'asset',
        url: '/static/app.js',
        content_sha256: assetDigest
    };
    assert.strictEqual(
        await api.verifyFetchedRow(manifest, asset, new Response(assetBody)),
        assetDigest
    );
    await expectRejects(
        api.verifyFetchedRow(
            manifest,
            {...asset, content_sha256: '0'.repeat(64)},
            new Response(assetBody)
        ),
        'content_digest_mismatch'
    );

    const pageBody = '<html>prepared heat</html>';
    const pageDigest = await api.sha256Bytes(
        new TextEncoder().encode(pageBody).buffer
    );
    const pageHeaders = {
        'Content-Type': 'text/html; charset=utf-8',
        'X-ProAm-Offline-Build': manifest.application_build,
        'X-ProAm-Offline-Schedule': manifest.schedule_fingerprint,
        'X-ProAm-Offline-Tournament': String(manifest.tournament_id),
        'X-ProAm-Offline-Issuer': String(manifest.issuer_user_id),
        'X-ProAm-Offline-Role': manifest.issuer_role,
        'X-ProAm-Offline-Content-SHA256': pageDigest
    };
    assert.strictEqual(
        await api.verifyFetchedRow(
            manifest,
            {kind: 'page', url: '/scoring/9/heat/4/enter'},
            new Response(pageBody, {headers: pageHeaders})
        ),
        pageDigest
    );
    await expectRejects(
        api.verifyFetchedRow(
            manifest,
            {kind: 'page', url: '/scoring/9/heat/4/enter'},
            new Response(pageBody, {
                headers: {...pageHeaders, 'X-ProAm-Offline-Build': 'other-build'}
            })
        ),
        'prepared_context_mismatch'
    );

    let updatedEntry;
    context.fetch = async () => new Response(JSON.stringify({
        ok: true,
        replay_token: 'fresh-token',
        issuer_user_id: 7,
        issuer_role: 'judge'
    }), {headers: {'Content-Type': 'application/json'}});
    vm.runInContext(
        'updateLegacyEntry = async (_database, entry) => { globalThis.updatedEntry = entry; };',
        context
    );
    const entry = {id: 3, issuer_user_id: 7, issuer_role: 'judge', body: ''};
    const params = new URLSearchParams('request_id=request-1');
    await api.renewLegacyReplayToken({}, entry, params);
    updatedEntry = context.updatedEntry;
    assert.strictEqual(params.get('replay_token'), 'fresh-token');
    assert(updatedEntry.body.includes('replay_token=fresh-token'));

    context.fetch = async () => new Response(JSON.stringify({
        ok: true,
        replay_token: 'wrong-session-token',
        issuer_user_id: 8,
        issuer_role: 'judge'
    }), {headers: {'Content-Type': 'application/json'}});
    await expectRejects(
        api.renewLegacyReplayToken({}, entry, params),
        'legacy_issuer_session_mismatch'
    );

    const agedEntry = {
        id: 4,
        url: '/scoring/9/heat/4/enter',
        timestamp: Date.now() - (8 * 24 * 60 * 60 * 1000),
        request_id: 'request-aged',
        payload_sha256: 'a'.repeat(64),
        tournament_id: 9,
        heat_id: 4,
        issuer_user_id: 7,
        issuer_role: 'judge'
    };
    const agedParams = new URLSearchParams({
        request_id: agedEntry.request_id,
        payload_sha256: agedEntry.payload_sha256,
        tournament_id: '9',
        heat_id: '4',
        issuer_user_id: '7',
        issuer_role: 'judge',
        replay_token: 'expired-token'
    });
    let postCount = 0;
    let removed = false;
    api.setHooks({
        prepareLegacyEntry: async () => ({
            entry: agedEntry,
            params: agedParams,
            replayable: true
        }),
        updateLegacyEntry: async (_database, changed) => {
            updatedEntry = changed;
        },
        removeLegacyEntry: async (_database, id) => {
            removed = id === agedEntry.id;
        },
        postLegacy: async () => {
            postCount += 1;
            if (postCount === 1) {
                return new Response(JSON.stringify({
                    ok: false,
                    error: {code: 'csrf_expired'}
                }), {status: 400, headers: {'Content-Type': 'application/json'}});
            }
            if (postCount === 2) {
                return new Response(JSON.stringify({
                    ok: false,
                    error: {code: 'replay_token_expired'}
                }), {status: 403, headers: {'Content-Type': 'application/json'}});
            }
            return new Response(JSON.stringify({
                ok: true,
                receipt: {
                    accepted: true,
                    request_id: agedEntry.request_id,
                    payload_sha256: agedEntry.payload_sha256,
                    tournament_id: 9,
                    heat_id: 4,
                    issuing_user_id: 7
                }
            }), {status: 200, headers: {'Content-Type': 'application/json'}});
        }
    });
    context.fetch = async () => new Response(JSON.stringify({
        ok: true,
        replay_token: 'renewed-after-eight-days',
        issuer_user_id: 7,
        issuer_role: 'judge'
    }), {headers: {'Content-Type': 'application/json'}});

    assert.strictEqual(await api.replayLegacyEntry({}, agedEntry), true);
    assert.strictEqual(postCount, 3);
    assert.strictEqual(agedParams.get('replay_token'), 'renewed-after-eight-days');
    assert.strictEqual(removed, true);

    const overAgeEntry = {
        ...agedEntry,
        id: 5,
        timestamp: Date.now() - (31 * 24 * 60 * 60 * 1000)
    };
    api.setHooks({
        prepareLegacyEntry: async () => ({
            entry: overAgeEntry,
            params: new URLSearchParams(agedParams),
            replayable: true
        })
    });
    context.fetch = async () => {
        throw new Error('over-age entry attempted replay-token renewal');
    };
    await expectRejects(
        api.replayLegacyEntry({}, overAgeEntry),
        'manual_reconciliation_required'
    );
    assert.strictEqual(postCount, 3);
}

run().then(() => {
    process.stdout.write('service worker offline tests passed\n');
}).catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
