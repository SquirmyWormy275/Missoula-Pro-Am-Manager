'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(
    require('node:path').join(__dirname, '..', '..', 'static', 'js', 'offline_queue_shared.js'),
    'utf8'
);

function sharedStorage() {
    const values = new Map();
    let afterSet = null;
    return {
        get length() {
            return values.size;
        },
        key(index) {
            return Array.from(values.keys())[index] || null;
        },
        getItem(key) {
            return values.has(key) ? values.get(key) : null;
        },
        setItem(key, value) {
            values.set(key, String(value));
            if (afterSet) {
                const hook = afterSet;
                afterSet = null;
                hook(key, values);
            }
        },
        removeItem(key) {
            values.delete(key);
        },
        interfereOnce(hook) {
            afterSet = hook;
        }
    };
}

function lockManager() {
    const tails = new Map();
    return {
        request(name, _options, callback) {
            const prior = tails.get(name) || Promise.resolve();
            const current = prior.then(callback, callback);
            tails.set(name, current.catch(() => {}));
            return current;
        }
    };
}

function loadTab(storage, locks, owner = {issuer_user_id: 11, issuer_role: 'scorer'}) {
    const window = {
        ProAmCurrentUser: owner,
        crypto: {
            randomUUID: () => `00000000-0000-4000-8000-${String(Math.random()).slice(2, 14).padEnd(12, '0')}`,
            getRandomValues: (bytes) => bytes,
            subtle: {digest: async () => new Uint8Array(32)}
        }
    };
    const context = vm.createContext({
        FormData: class FormData {},
        TextEncoder,
        URLSearchParams,
        clearTimeout,
        console,
        fetch: async () => { throw new Error('unexpected fetch'); },
        localStorage: storage,
        navigator: {locks},
        Promise,
        setTimeout,
        Uint8Array,
        window
    });
    storage.setItem('proam_active_queue_owner_v1', `${owner.issuer_user_id}:${owner.issuer_role}`);
    vm.runInContext(SOURCE, context, {filename: 'offline_queue_shared.js'});
    return window.ProAmOfflineQueue;
}

function entry(id, heatId) {
    return {
        request_id: id,
        tournament_id: 7,
        heat_id: heatId,
        issuer_user_id: 11,
        issuer_role: 'scorer',
        payload_sha256: `sha-${id}`
    };
}

function receipt(item) {
    return {
        accepted: true,
        request_id: item.request_id,
        tournament_id: item.tournament_id,
        heat_id: item.heat_id,
        issuing_user_id: item.issuer_user_id,
        payload_sha256: item.payload_sha256
    };
}

async function main() {
    const storage = sharedStorage();
    const locks = lockManager();
    const firstTab = loadTab(storage, locks);
    const secondTab = loadTab(storage, locks);
    const alpha = entry('alpha', 1);
    const beta = entry('beta', 2);

    await Promise.all([firstTab.upsert(alpha), secondTab.upsert(beta)]);
    assert.deepEqual(
        Array.from(firstTab.read(), (row) => row.request_id).sort(),
        ['alpha', 'beta']
    );

    const gamma = entry('gamma', 3);
    await Promise.all([
        firstTab.removeVerified(alpha, receipt(alpha)),
        secondTab.upsert(gamma)
    ]);
    assert.deepEqual(
        Array.from(firstTab.read(), (row) => row.request_id).sort(),
        ['beta', 'gamma']
    );

    const delta = entry('delta', 4);
    const external = entry('external', 5);
    storage.interfereOnce((key, values) => {
        if (key === firstTab.key) values.set(key, JSON.stringify([beta, gamma, external]));
    });
    await firstTab.upsert(delta);
    assert.deepEqual(
        Array.from(firstTab.read(), (row) => row.request_id).sort(),
        ['beta', 'delta', 'external', 'gamma']
    );

    const late = entry('late', 8);
    storage.interfereOnce((key, values) => {
        if (key === firstTab.key) {
            values.set(key, JSON.stringify([beta, delta, external, gamma, late]));
        }
    });
    await firstTab.removeVerified(gamma, receipt(gamma));
    assert.deepEqual(
        Array.from(firstTab.read(), (row) => row.request_id).sort(),
        ['beta', 'delta', 'external', 'late']
    );

    assert.equal(firstTab.verifyReceipt(delta, receipt(delta)), true);
    assert.equal(
        firstTab.verifyReceipt(delta, {...receipt(delta), issuing_user_id: 99}),
        false
    );

    const context = {
        tournament_id: 7,
        issuer_user_id: 11,
        issuer_role: 'admin',
        schedule_fingerprint: 'schedule-1',
        application_build: 'build-1'
    };
    const cachedFormPayload = {
        request_id: '00000000-0000-4000-8000-cached000000',
        heat_id: '9',
        tournament_id: '7',
        result_22: '14'
    };
    const firstAction = await firstTab.prepareEntry({
        heat_id: 9,
        payload: cachedFormPayload
    }, context);
    const secondAction = await firstTab.prepareEntry({
        heat_id: 9,
        payload: cachedFormPayload
    }, context);
    assert.notEqual(firstAction.request_id, cachedFormPayload.request_id);
    assert.notEqual(firstAction.request_id, secondAction.request_id);
    const retriedAction = await firstTab.prepareEntry(firstAction, context);
    assert.equal(retriedAction.request_id, firstAction.request_id);

    // Switching accounts hides the previous queue without deleting any scores.
    const retainedIds = Array.from(firstTab.read(), row => row.request_id).sort();
    const otherTab = loadTab(storage, locks, {issuer_user_id: 22, issuer_role: 'judge'});
    assert.equal(otherTab.read().length, 0);
    assert.equal(firstTab.read().length, 0, 'an already-open tab is locked after account switch');
    const otherScore = {...entry('other-owner', 3), issuer_user_id: 22, issuer_role: 'judge'};
    await otherTab.upsert(otherScore);
    await assert.rejects(otherTab.merge([alpha]), error => error.code === 'issuer_mismatch');
    await otherTab.removeTournament(7);
    const restored = loadTab(storage, locks);
    assert.deepEqual(Array.from(restored.read(), row => row.request_id).sort(), retainedIds,
        'clearing one owner never deletes another owner scores');
    storage.setItem('proam_active_queue_owner_v1', '');
    assert.equal(restored.read().length, 0, 'logout hides scores without deleting raw data');
    await assert.rejects(restored.upsert(alpha), error => error.code === 'issuer_mismatch');

    const fallbackStorage = sharedStorage();
    const fallbackFirst = loadTab(fallbackStorage, undefined);
    const fallbackSecond = loadTab(fallbackStorage, undefined);
    await Promise.all([
        fallbackFirst.upsert(entry('fallback-one', 6)),
        fallbackSecond.upsert(entry('fallback-two', 7))
    ]);
    assert.deepEqual(
        Array.from(fallbackFirst.read(), (row) => row.request_id).sort(),
        ['fallback-one', 'fallback-two']
    );
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
