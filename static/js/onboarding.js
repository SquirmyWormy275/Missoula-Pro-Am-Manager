/**
 * ProAmOnboarding — lightweight first-time onboarding modal.
 * Uses localStorage so the popup only shows once per guide key.
 * Requires Bootstrap 5 (already on page).
 */
(function () {
    'use strict';

    var STORAGE_PREFIX = 'proam_onboarding_v1_';
    var MODAL_ID       = 'proamOnboardingModal';

    /**
     * Show the onboarding guide if it has not been seen before.
     * @param {string} guideKey  - unique key, e.g. 'spectator', 'pro', 'captain'
     * @param {Array}  steps     - array of {icon, title, body} objects
     * @param {number} [delay]   - ms before the modal appears (default 600)
     */
    function show(guideKey, steps, delay) {
        var key = STORAGE_PREFIX + guideKey;
        if (localStorage.getItem(key)) return;
        setTimeout(function () { _open(guideKey, steps, key); }, delay != null ? delay : 600);
    }

    /**
     * Force-open the guide regardless of localStorage (for "?" re-open buttons).
     */
    function reopen(guideKey, steps) {
        _open(guideKey, steps, null);
    }

    function _open(guideKey, steps, storageKey) {
        // Remove any pre-existing instance
        var old = document.getElementById(MODAL_ID);
        if (old) old.remove();

        if (!steps || steps.length === 0) return;

        var current = 0;

        // ── Build modal DOM ────────────────────────────────────────
        var el = document.createElement('div');
        el.className = 'modal fade';
        el.id = MODAL_ID;
        el.setAttribute('tabindex', '-1');
        el.setAttribute('aria-modal', 'true');
        el.setAttribute('role', 'dialog');
        el.innerHTML = _buildHTML(steps);
        document.body.appendChild(el);

        var modal = new bootstrap.Modal(el, { backdrop: 'static', keyboard: false });

        // ── Wire up controls ───────────────────────────────────────
        var prevBtn  = el.querySelector('.ob-prev');
        var nextBtn  = el.querySelector('.ob-next');
        var skipBtn  = el.querySelector('.ob-skip');
        var dotsWrap = el.querySelector('.ob-dots');
        var slides   = el.querySelectorAll('.ob-slide');
        var dots     = dotsWrap ? dotsWrap.querySelectorAll('.ob-dot') : [];

        function goTo(idx) {
            slides[current].classList.remove('ob-active');
            dots[current] && dots[current].classList.remove('ob-dot-active');
            current = idx;
            slides[current].classList.add('ob-active');
            dots[current] && dots[current].classList.add('ob-dot-active');

            prevBtn.style.visibility = current === 0 ? 'hidden' : 'visible';

            if (current === steps.length - 1) {
                nextBtn.textContent = 'Got it!';
                nextBtn.className = nextBtn.className.replace('btn-primary', 'btn-success');
            } else {
                nextBtn.textContent = 'Next →';
                nextBtn.className = nextBtn.className.replace('btn-success', 'btn-primary');
            }
        }

        prevBtn.addEventListener('click', function () {
            if (current > 0) goTo(current - 1);
        });

        nextBtn.addEventListener('click', function () {
            if (current < steps.length - 1) {
                goTo(current + 1);
            } else {
                _dismiss(modal, el, storageKey);
            }
        });

        skipBtn.addEventListener('click', function () {
            _dismiss(modal, el, storageKey);
        });

        el.addEventListener('hidden.bs.modal', function () {
            if (storageKey) localStorage.setItem(storageKey, '1');
            el.remove();
        });

        // Initialise first slide
        goTo(0);
        modal.show();
    }

    function _dismiss(modal, el, storageKey) {
        if (storageKey) localStorage.setItem(storageKey, '1');
        modal.hide();
    }

    // ── HTML builder ───────────────────────────────────────────────
    function _buildHTML(steps) {
        var slidesHTML = steps.map(function (s, i) {
            return '<div class="ob-slide' + (i === 0 ? ' ob-active' : '') + '" aria-hidden="' + (i !== 0) + '">' +
                   '  <div class="ob-icon">' + (s.icon || '<i class="bi bi-info-circle-fill"></i>') + '</div>' +
                   '  <h5 class="ob-title">' + _esc(s.title) + '</h5>' +
                   '  <p class="ob-body">' + s.body + '</p>' +
                   '</div>';
        }).join('');

        var dotsHTML = steps.map(function (_, i) {
            return '<span class="ob-dot' + (i === 0 ? ' ob-dot-active' : '') + '"></span>';
        }).join('');

        return [
            '<div class="modal-dialog modal-dialog-centered" style="max-width:520px;">',
            '  <div class="modal-content ob-modal-content">',
            '    <div class="ob-header">',
            '      <span class="ob-brand"><i class="bi bi-mortarboard-fill me-1"></i>Quick Guide</span>',
            '      <button type="button" class="ob-skip">Skip</button>',
            '    </div>',
            '    <div class="ob-body-wrap">',
            '      <div class="ob-slides">',
                     slidesHTML,
            '      </div>',
            '    </div>',
            '    <div class="ob-footer">',
            '      <div class="ob-dots">' + dotsHTML + '</div>',
            '      <div class="ob-nav">',
            '        <button class="btn btn-sm btn-outline-secondary ob-prev" style="visibility:hidden;">← Back</button>',
            '        <button class="btn btn-sm btn-primary ob-next">Next →</button>',
            '      </div>',
            '    </div>',
            '  </div>',
            '</div>',
        ].join('\n');
    }

    function _esc(str) {
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    // Inject stylesheet
    var style = document.createElement('style');
    style.textContent = [
        '.ob-modal-content {',
        '  background: var(--sx-surface, #13161d);',
        '  border: 1px solid var(--sx-border-bright, #363e52);',
        '  border-radius: 14px;',
        '  overflow: hidden;',
        '  color: var(--sx-text, #ece8e0);',
        '}',
        '.ob-header {',
        '  display: flex;',
        '  align-items: center;',
        '  justify-content: space-between;',
        '  padding: 14px 20px 10px;',
        '  border-bottom: 1px solid var(--sx-border, #252a38);',
        '}',
        '.ob-brand {',
        '  font-size: .78rem;',
        '  font-weight: 600;',
        '  letter-spacing: .05em;',
        '  text-transform: uppercase;',
        '  color: var(--sx-text-2, #8c95aa);',
        '}',
        '.ob-skip {',
        '  background: none;',
        '  border: none;',
        '  font-size: .78rem;',
        '  color: var(--sx-text-2, #8c95aa);',
        '  cursor: pointer;',
        '  padding: 4px 8px;',
        '  border-radius: 6px;',
        '  transition: color .15s;',
        '}',
        '.ob-skip:hover { color: var(--sx-text, #ece8e0); }',
        '.ob-body-wrap { padding: 28px 28px 12px; min-height: 180px; }',
        '.ob-slides { position: relative; }',
        '.ob-slide { display: none; text-align: center; }',
        '.ob-slide.ob-active { display: block; animation: obFadeIn .22s ease; }',
        '@keyframes obFadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }',
        '.ob-icon { font-size: 2.2rem; margin-bottom: 12px; color: var(--sx-fire, #e8391f); }',
        '.ob-title { font-size: 1.05rem; font-weight: 700; margin-bottom: 8px; color: var(--sx-text, #ece8e0); }',
        '.ob-body { font-size: .88rem; color: var(--sx-text-2, #8c95aa); line-height: 1.6; margin: 0; }',
        '.ob-footer {',
        '  display: flex;',
        '  align-items: center;',
        '  justify-content: space-between;',
        '  padding: 14px 20px 18px;',
        '  border-top: 1px solid var(--sx-border, #252a38);',
        '}',
        '.ob-dots { display: flex; gap: 6px; align-items: center; }',
        '.ob-dot {',
        '  width: 7px; height: 7px;',
        '  border-radius: 50%;',
        '  background: var(--sx-border-bright, #363e52);',
        '  transition: background .2s, transform .2s;',
        '}',
        '.ob-dot.ob-dot-active {',
        '  background: var(--sx-fire, #e8391f);',
        '  transform: scale(1.25);',
        '}',
        '.ob-nav { display: flex; gap: 8px; }',
    ].join('\n');
    document.head.appendChild(style);

    // Expose globally
    window.ProAmOnboarding = { show: show, reopen: reopen };
})();
