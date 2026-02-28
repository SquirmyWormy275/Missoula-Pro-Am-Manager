/**
 * Offline Queue Client
 * - Registers the service worker
 * - Shows/hides the offline status banner
 * - Handles sync-complete notifications from the SW
 * - Intercepts the score entry form to display offline feedback
 */
(function () {
    'use strict';

    // ── Service Worker Registration ──────────────────────────────────────────
    function registerSW() {
        if (!('serviceWorker' in navigator)) return;
        navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function (err) {
            console.warn('[ProAm SW] Registration failed:', err);
        });

        // Listen for sync-complete messages from the SW
        navigator.serviceWorker.addEventListener('message', function (event) {
            if (event.data && event.data.type === 'sync-complete') {
                showSyncBanner(event.data.count || 0);
            }
        });
    }

    // ── Offline Banner ───────────────────────────────────────────────────────
    function createBanner(id, html, bg, color) {
        var banner = document.createElement('div');
        banner.id = id;
        banner.innerHTML = html;
        banner.style.cssText = [
            'position:fixed', 'top:0', 'left:0', 'right:0',
            'z-index:9999', 'background:' + bg, 'color:' + color,
            'text-align:center', 'padding:8px 16px', 'font-size:14px',
            'font-weight:600', 'box-shadow:0 2px 8px rgba(0,0,0,.3)',
        ].join(';');
        return banner;
    }

    function showOfflineBanner() {
        if (document.getElementById('proam-offline-banner')) return;
        var banner = createBanner(
            'proam-offline-banner',
            '<i class="bi bi-wifi-off"></i> &nbsp;Offline mode — scores will be queued and synced when connection restores.',
            '#e89012', '#000'
        );
        document.body.insertBefore(banner, document.body.firstChild);
        // Push content down so the banner doesn't cover the nav
        document.body.style.paddingTop = (
            parseInt(document.body.style.paddingTop || '0', 10) + 40
        ) + 'px';
    }

    function hideOfflineBanner() {
        var banner = document.getElementById('proam-offline-banner');
        if (banner) {
            banner.remove();
            document.body.style.paddingTop = Math.max(
                0,
                parseInt(document.body.style.paddingTop || '0', 10) - 40
            ) + 'px';
        }
    }

    function showSyncBanner(count) {
        var msg = count === 1
            ? '1 queued score has been synced successfully.'
            : count + ' queued scores have been synced successfully.';
        var flash = document.createElement('div');
        flash.className = 'alert alert-success position-fixed shadow';
        flash.style.cssText = 'top:70px;right:20px;z-index:9998;max-width:320px;';
        flash.innerHTML = '<i class="bi bi-check-circle-fill"></i> ' + msg;
        document.body.appendChild(flash);
        setTimeout(function () { flash.remove(); }, 6000);
    }

    // ── Score-entry form feedback when offline ───────────────────────────────
    function patchScoreForm() {
        // Only applies on the heat entry page
        if (!/\/scoring\/\d+\/heat\/\d+\/enter/.test(window.location.pathname)) return;

        document.addEventListener('submit', function (e) {
            var form = e.target;
            if (form.method && form.method.toLowerCase() !== 'post') return;
            if (!/\/scoring\/\d+\/heat\/\d+\/enter/.test(form.action)) return;

            if (!navigator.onLine) {
                // Let the SW handle it; intercept the response to show feedback
                e.preventDefault();
                var data = new URLSearchParams(new FormData(form)).toString();
                fetch(form.action, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: data,
                }).then(function (resp) {
                    return resp.json();
                }).then(function (json) {
                    if (json.offline) {
                        showOfflineQueuedAlert(json.message || 'Score queued for sync.');
                    }
                }).catch(function () {
                    showOfflineQueuedAlert('Score queued for sync when connection restores.');
                });
            }
        }, true);
    }

    function showOfflineQueuedAlert(msg) {
        var existing = document.getElementById('proam-offline-queued');
        if (existing) existing.remove();
        var alert = document.createElement('div');
        alert.id = 'proam-offline-queued';
        alert.className = 'alert alert-warning mt-3';
        alert.innerHTML = '<i class="bi bi-clock-history"></i> ' + msg;
        var container = document.querySelector('.container-fluid, .container, main');
        if (container) {
            container.insertBefore(alert, container.firstChild);
        } else {
            document.body.insertBefore(alert, document.body.firstChild);
        }
    }

    // ── Init ─────────────────────────────────────────────────────────────────
    if (!navigator.onLine) showOfflineBanner();
    window.addEventListener('online', function () {
        hideOfflineBanner();
        // Trigger manual replay in case Background Sync isn't supported
        if (navigator.serviceWorker && navigator.serviceWorker.controller) {
            navigator.serviceWorker.controller.postMessage({ type: 'manual-sync' });
        }
    });
    window.addEventListener('offline', showOfflineBanner);

    registerSW();
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', patchScoreForm);
    } else {
        patchScoreForm();
    }
})();
