// Live countdown for market-hours cards.
//
// Each card carries `data-transition-iso="2026-05-20T08:00:00+00:00"`
// (UTC moment of the next state change) and a `data-countdown` slot.
// We tick once a minute and fill the slot with "· in 1h 23m" — fast
// enough to feel current, slow enough to skip seconds-level churn.
(function () {
    'use strict';

    function formatDelta(ms) {
        if (!Number.isFinite(ms)) return '';
        if (ms <= 0) return '· now';
        const totalMinutes = Math.floor(ms / 60000);
        if (totalMinutes < 1) return '· in <1m';
        const days = Math.floor(totalMinutes / 1440);
        const hours = Math.floor((totalMinutes % 1440) / 60);
        const minutes = totalMinutes % 60;
        if (days > 0) return `· in ${days}d ${hours}h`;
        if (hours > 0) return `· in ${hours}h ${minutes}m`;
        return `· in ${minutes}m`;
    }

    function tick() {
        const now = Date.now();
        const cards = document.querySelectorAll('[data-transition-iso]');
        cards.forEach(function (el) {
            const iso = el.getAttribute('data-transition-iso');
            const slot = el.querySelector('[data-countdown]');
            if (!slot) return;
            if (!iso) { slot.textContent = ''; return; }
            const ts = Date.parse(iso);
            if (Number.isNaN(ts)) { slot.textContent = ''; return; }
            slot.textContent = formatDelta(ts - now);
        });
    }

    // Plan default: collapsed on portrait/narrow, expanded on desktop.
    // Run once on first paint; user gestures override and persist via
    // the native <details> element.
    function syncDrawerDefault() {
        const drawer = document.querySelector('.market-drawer');
        if (!drawer) return;
        if (drawer.dataset.userToggled === 'true') return;
        drawer.open = window.matchMedia(
            '(min-width: 768px) and (orientation: landscape)'
        ).matches;
    }

    function markUserToggled(e) {
        // Any user toggle disables the responsive default until reload.
        e.currentTarget.dataset.userToggled = 'true';
    }

    function attachDrawerHandlers() {
        const drawer = document.querySelector('.market-drawer');
        if (!drawer || drawer.dataset.bound) return;
        drawer.dataset.bound = 'true';
        drawer.addEventListener('toggle', markUserToggled);
    }

    function onReady() {
        tick();
        syncDrawerDefault();
        attachDrawerHandlers();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', onReady);
    } else {
        onReady();
    }
    // Re-run on every minute boundary so the displayed offset stays fresh.
    setInterval(tick, 60_000);
    // HTMX swaps replace nodes; rerun after each swap so newly inserted
    // cards get a countdown immediately.
    document.body.addEventListener('htmx:afterSwap', function () {
        tick();
        attachDrawerHandlers();
    });
})();
