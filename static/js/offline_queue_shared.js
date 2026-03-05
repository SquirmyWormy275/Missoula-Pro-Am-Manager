/* Shared client-side queue helpers for offline scoring pages. */
(function () {
    'use strict';

    var KEY = 'proam_heat_score_queue_v1';

    function read() {
        try {
            var parsed = JSON.parse(localStorage.getItem(KEY) || '[]');
            return Array.isArray(parsed) ? parsed : [];
        } catch (_err) {
            return [];
        }
    }

    function write(queue) {
        localStorage.setItem(KEY, JSON.stringify(queue));
    }

    function upsert(entry) {
        var queue = read().filter(function (item) {
            return !(item.heat_id === entry.heat_id && item.url === entry.url);
        });
        queue.push(entry);
        write(queue);
        return queue;
    }

    function remove(url, heatId) {
        var queue = read().filter(function (item) {
            return !(String(item.url || '') === String(url || '') && Number(item.heat_id || 0) === Number(heatId || 0));
        });
        write(queue);
        return queue;
    }

    function find(url, heatId) {
        return read().find(function (item) {
            return String(item.url || '') === String(url || '') && Number(item.heat_id || 0) === Number(heatId || 0);
        });
    }

    function byTournament(tournamentId) {
        var marker = '/scoring/' + tournamentId + '/';
        return read().filter(function (item) {
            return String(item.url || '').indexOf(marker) !== -1;
        });
    }

    window.ProAmOfflineQueue = {
        key: KEY,
        read: read,
        write: write,
        upsert: upsert,
        remove: remove,
        find: find,
        byTournament: byTournament
    };
})();
