'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const {webcrypto} = require('crypto');

const source = fs.readFileSync('static/sw.js', 'utf8') + `
globalThis.__swTest = {
    sha256Bytes,
    networkFirstPreparedFallback,
    handleLogout,
    writePreparedManifest,
    saveAccount,
    readPreparedManifest,
    readAccount,
    prepareOfflinePackage,
    verifyFetchedRow,
    renewLegacyReplayToken,
    replayLegacyEntry,
    setHooks(hooks) {
        if (hooks.verifyManifestRows) verifyManifestRows = hooks.verifyManifestRows;
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
    const stores = new Map();
    let beforeKeys = null;
    let beforeMatch = null;
    const cacheKey = value => typeof value === 'string' ? value : value.url;
    context.caches = {
        keys: async () => {
            if (beforeKeys) await beforeKeys();
            return Array.from(stores.keys());
        },
        delete: async name => stores.delete(name),
        open: async name => {
            if (!stores.has(name)) stores.set(name, new Map());
            const rows = stores.get(name);
            return {
                match: async key => {
                    const response = rows.get(cacheKey(key))?.clone() || null;
                    if (beforeMatch) await beforeMatch(name, key);
                    return response;
                },
                put: async (key, response) => rows.set(cacheKey(key), response.clone())
            };
        }
    };
    const scoreRequest = new Request('https://proam.test/scoring/1/heat/2/enter');
    const fallback = await context.caches.open('proam-scoring-v3');
    await fallback.put('/static/offline.html', new Response('Prepare this account online first'));
    context.fetch = async () => new Response('Private scorer A page', {
        headers: {'Content-Type': 'text/html', 'X-ProAm-Account': '11:scorer'}
    });
    await api.networkFirstPreparedFallback(scoreRequest);
    assert.equal(await fallback.match(scoreRequest), null, 'live authenticated HTML is never generally cached');
    async function seedPrepared() {
        await api.saveAccount('11:scorer');
        const cacheName = 'proam-prepared-pages-v3-account11';
        const cache = await context.caches.open(cacheName);
        await cache.put(scoreRequest, new Response('Private scorer A page'));
        await api.writePreparedManifest({cache_name: cacheName, issuer_user_id: 11, issuer_role: 'scorer'});
    }
    await seedPrepared();
    context.fetch = async () => { throw new Error('offline'); };
    assert.equal(await (await api.networkFirstPreparedFallback(scoreRequest)).text(), 'Private scorer A page');
    context.fetch = async () => new Response('Signed out', {headers: {'X-ProAm-Account': 'anonymous'}});
    await api.handleLogout(new Request('https://proam.test/auth/logout', {method: 'POST'}));
    assert.equal(await api.readPreparedManifest(), null);
    context.fetch = async () => { throw new Error('offline'); };
    assert.equal(await (await api.networkFirstPreparedFallback(scoreRequest)).text(), 'Prepare this account online first');
    await seedPrepared();
    context.fetch = async () => new Response('Scorer B dashboard', {headers: {'X-ProAm-Account': '22:scorer'}});
    await api.networkFirstPreparedFallback(new Request('https://proam.test/'));
    assert.equal(await api.readPreparedManifest(), null, 'account switch invalidates prepared HTML');
    // An old authenticated response cannot restore its account after a newer login.
    await api.saveAccount('11:scorer');
    let deliverOldPage;
    context.fetch = () => new Promise(resolve => { deliverOldPage = resolve; });
    const oldNetworkPage = api.networkFirstPreparedFallback(scoreRequest);
    context.fetch = async () => new Response('Scorer B', {headers: {'X-ProAm-Account': '22:scorer'}});
    await api.networkFirstPreparedFallback(new Request('https://proam.test/'));
    deliverOldPage(new Response('Private old account A page', {headers: {'X-ProAm-Account': '11:scorer'}}));
    const rejectedOldPage = await oldNetworkPage;
    assert.equal(rejectedOldPage.status, 503);
    assert.equal(await api.readAccount(), '22:scorer');
    assert(!String(await rejectedOldPage.text()).includes('Private'));
    context.fetch = () => new Promise(resolve => { deliverOldPage = resolve; });
    const pageBeforeLogout = api.networkFirstPreparedFallback(scoreRequest);
    context.fetch = async () => new Response('Signed out', {headers: {'X-ProAm-Account': 'anonymous'}});
    await api.handleLogout(new Request('https://proam.test/auth/logout', {method: 'POST'}));
    deliverOldPage(new Response('Private B', {headers: {'X-ProAm-Account': '22:scorer'}}));
    assert.equal((await pageBeforeLogout).status, 503);
    assert.equal(await api.readAccount(), 'anonymous');


    // Pause after preparation's last admission check, during final publication.
    let releaseCommit;
    let reachedCommit;
    const commitReached = new Promise(resolve => { reachedCommit = resolve; });
    const commitBlocked = new Promise(resolve => { releaseCommit = resolve; });
    beforeKeys = async () => {
        beforeKeys = null;
        reachedCommit();
        await commitBlocked;
    };
    api.setHooks({verifyManifestRows: async () => true});
    const preparing = api.prepareOfflinePackage({schema_version: 2, pages: [], assets: [],
        issuer_user_id: 11, issuer_role: 'scorer', tournament_id: 1,
        application_build: 'build', schedule_fingerprint: 'schedule'}, null);
    await commitReached;
    const logoutDuringCommit = api.handleLogout(new Request('https://proam.test/auth/logout', {method: 'POST'}));
    releaseCommit();
    assert.equal((await preparing).state, 'incomplete');
    await logoutDuringCommit;
    assert.equal(await api.readPreparedManifest(), null, 'logout cannot be undone by an in-flight prepare commit');
    assert.equal(await api.readAccount(), 'anonymous');

    // A cache read already in flight must not release private HTML after logout starts.
    await seedPrepared();
    let releaseRead;
    let reachedRead;
    const readReached = new Promise(resolve => { reachedRead = resolve; });
    const readBlocked = new Promise(resolve => { releaseRead = resolve; });
    beforeMatch = async (name, key) => {
        if (!name.startsWith('proam-prepared-pages-') || cacheKey(key) !== scoreRequest.url) return;
        beforeMatch = null;
        reachedRead();
        await readBlocked;
    };
    context.fetch = async () => { throw new Error('offline'); };
    const pendingPage = api.networkFirstPreparedFallback(scoreRequest);
    await readReached;
    context.fetch = async () => new Response('Signed out');
    const logoutDuringRead = api.handleLogout(new Request('https://proam.test/auth/logout', {method: 'POST'}));
    releaseRead();
    assert.equal(await (await pendingPage).text(), 'Prepare this account online first');
    await logoutDuringRead;


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
