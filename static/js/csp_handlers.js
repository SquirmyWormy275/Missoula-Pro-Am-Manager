/*
 * csp_handlers.js — Global event delegation for data-attribute-driven UI hooks.
 *
 * Replaces inline event handlers (onclick="...", onsubmit="return confirm(...)", etc.)
 * so the app can run under a strict Content-Security-Policy that drops 'unsafe-inline'
 * from script-src. Inline event handlers cannot be nonced and are blocked by such a CSP.
 *
 * Wired to: [data-print], [data-close-window], [data-reload], [data-confirm].
 * Per-template handlers (data-toggle-partner, data-action="copy-share-link", etc.)
 * live in their own templates' <script> blocks.
 *
 * SECURITY FIX (CSO #7): part of the migration to nonce-based CSP.
 */
(function () {
    'use strict';

    // ----- Click delegation -------------------------------------------------
    document.addEventListener('click', function (event) {
        var target = event.target;
        if (!target || typeof target.closest !== 'function') return;

        // [data-print] — invoke browser print
        var printer = target.closest('[data-print]');
        if (printer) {
            event.preventDefault();
            window.print();
            return;
        }

        // [data-close-window] — close the current window/tab
        var closer = target.closest('[data-close-window]');
        if (closer) {
            event.preventDefault();
            window.close();
            return;
        }

        // [data-reload] — full page reload
        var reloader = target.closest('[data-reload]');
        if (reloader) {
            event.preventDefault();
            window.location.reload();
            return;
        }

        // [data-confirm] on a button or link — show a native confirm() and
        // cancel the action if the user declines. For form-submit buttons we
        // also stop the form submission.
        var confirmer = target.closest('[data-confirm]');
        if (confirmer && confirmer.tagName !== 'FORM') {
            var msg = confirmer.getAttribute('data-confirm') || 'Are you sure?';
            if (!window.confirm(msg)) {
                event.preventDefault();
                event.stopPropagation();
                event.stopImmediatePropagation();
                return;
            }
        }
    }, true);

    // ----- Submit delegation ------------------------------------------------
    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (!form || form.tagName !== 'FORM') return;

        // [data-confirm] on a form — show confirm() before allowing submission
        if (form.hasAttribute('data-confirm')) {
            var msg = form.getAttribute('data-confirm') || 'Are you sure?';
            if (!window.confirm(msg)) {
                event.preventDefault();
                event.stopPropagation();
                event.stopImmediatePropagation();
                return;
            }
        }
    }, true);
})();
