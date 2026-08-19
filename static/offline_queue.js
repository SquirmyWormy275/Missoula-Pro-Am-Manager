/** Global service-worker registration and offline state surface. */
(function () {
    'use strict';

    function controllerMessage(message) {
        if (navigator.serviceWorker && navigator.serviceWorker.controller) {
            navigator.serviceWorker.controller.postMessage(message);
        }
    }

    function validatePreparedPackage() {
        if (!window.ProAmOfflineContext) return;
        controllerMessage({
            type: 'validate-prepared-package',
            context: window.ProAmOfflineContext
        });
    }

    function registerSW() {
        if (!('serviceWorker' in navigator)) return;
        navigator.serviceWorker.register('/sw.js', {scope: '/'}).then(function () {
            return navigator.serviceWorker.ready;
        }).then(function () {
            validatePreparedPackage();
            controllerMessage({type: 'legacy-queue-status'});
        }).catch(function (error) {
            console.warn('[ProAm SW] Registration failed:', error);
        });

        navigator.serviceWorker.addEventListener('controllerchange', function () {
            validatePreparedPackage();
        });
        navigator.serviceWorker.addEventListener('message', function (event) {
            var data = event.data || {};
            if (data.type === 'legacy-sync-complete' && data.success > 0) {
                showSyncBanner(data.success);
            }
            if (data.type === 'legacy-sync-complete' && data.failed > 0) {
                showReplayFailedBanner(data.failed, data.reasons || []);
            }
        });
    }

    function createBanner(id, html, background, color) {
        var banner = document.createElement('div');
        banner.id = id;
        banner.innerHTML = html;
        banner.style.cssText = [
            'position:fixed', 'top:0', 'left:0', 'right:0',
            'z-index:9999', 'background:' + background, 'color:' + color,
            'text-align:center', 'padding:8px 16px', 'font-size:14px',
            'font-weight:600', 'box-shadow:0 2px 8px rgba(0,0,0,.3)'
        ].join(';');
        return banner;
    }

    function showOfflineBanner() {
        if (document.getElementById('proam-offline-banner')) return;
        var banner = createBanner(
            'proam-offline-banner',
            '<i class="bi bi-wifi-off"></i> &nbsp;Offline mode - score submissions stay on this device until verified.',
            '#e89012',
            '#000'
        );
        document.body.insertBefore(banner, document.body.firstChild);
        document.body.style.paddingTop = (
            parseInt(document.body.style.paddingTop || '0', 10) + 40
        ) + 'px';
    }

    function hideOfflineBanner() {
        var banner = document.getElementById('proam-offline-banner');
        if (!banner) return;
        banner.remove();
        document.body.style.paddingTop = Math.max(
            0,
            parseInt(document.body.style.paddingTop || '0', 10) - 40
        ) + 'px';
    }

    function showSyncBanner(count) {
        var message = count === 1 ?
            '1 legacy queued score was verified and removed.' :
            count + ' legacy queued scores were verified and removed.';
        var flash = document.createElement('div');
        flash.className = 'alert alert-success position-fixed shadow';
        flash.style.cssText = 'top:70px;right:20px;z-index:9998;max-width:360px;';
        flash.innerHTML = '<i class="bi bi-check-circle-fill"></i> ' + message;
        document.body.appendChild(flash);
        setTimeout(function () { flash.remove(); }, 6000);
    }

    function showReplayFailedBanner(count, reasons) {
        var existing = document.getElementById('proam-replay-failed-banner');
        if (existing) existing.remove();
        var needsManual = reasons.indexOf('manual_reconciliation_required') >= 0;
        var message = count === 1 ?
            '1 legacy queued score remains on this device.' :
            count + ' legacy queued scores remain on this device.';
        if (needsManual) message += ' An old entry requires manual reconciliation.';
        var banner = document.createElement('div');
        banner.id = 'proam-replay-failed-banner';
        banner.className = 'alert alert-warning position-fixed shadow';
        banner.style.cssText = 'top:70px;right:20px;z-index:9998;max-width:420px;';
        banner.innerHTML = '<i class="bi bi-exclamation-triangle-fill"></i> ' + message;
        document.body.appendChild(banner);
    }

    if (!navigator.onLine) showOfflineBanner();
    window.addEventListener('online', function () {
        hideOfflineBanner();
        validatePreparedPackage();
    });
    window.addEventListener('offline', showOfflineBanner);
    registerSW();
}());
