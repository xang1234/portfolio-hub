// Live countdown for market-hours cards.
//
// Each card carries `data-transition-iso="2026-05-20T08:00:00+00:00"`
// (UTC moment of the next state change) and a `data-countdown` slot.
// We tick once a minute and fill the slot with "· in 1h 23m" — fast
// enough to feel current, slow enough to skip seconds-level churn.
(function () {
    'use strict';

    // ──────────────────────────────────────────────────────────────────
    // Top-of-IIFE `const`s. JavaScript hoists `const` declarations but
    // throws (TDZ) on access before initialization, so anything used
    // by a function that may run during IIFE setup must be declared
    // here — NOT at the point where the feature block begins.
    // ──────────────────────────────────────────────────────────────────

    // Drawer + sort persistence (localStorage keys)
    const DRAWER_KEY = 'portfolio-hub.drawer';
    const SORT_KEY = 'portfolio-hub.sort';
    const DEFAULT_SORT = { key: 'mv_usd', dir: 'desc' };

    // Pull-to-refresh threshold (px) — ≥ 50 by review rule so casual
    // scroll-bounce doesn't trigger a reload.
    const PULL_REFRESH_THRESHOLD = 70;
    let pullStartY = null;
    let pullDelta = 0;

    // Long-press threshold (ms)
    const LONG_PRESS_MS = 500;
    let longPressTimer = null;

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
    // localStorage override wins so a returning user lands in their
    // last explicit state regardless of orientation. (DRAWER_KEY
    // declared at the top of the IIFE — see TDZ note.)

    function syncDrawerDefault() {
        const drawer = document.querySelector('.market-drawer');
        if (!drawer) return;
        if (drawer.dataset.userToggled === 'true') return;
        let stored = null;
        try { stored = localStorage.getItem(DRAWER_KEY); } catch (e) {}
        if (stored === 'open') { drawer.open = true; return; }
        if (stored === 'closed') { drawer.open = false; return; }
        drawer.open = window.matchMedia(
            '(min-width: 768px) and (orientation: landscape)'
        ).matches;
    }

    function markUserToggled(e) {
        // Any user toggle disables the responsive default for this load
        // AND persists the new state to localStorage for next time.
        e.currentTarget.dataset.userToggled = 'true';
        try {
            localStorage.setItem(DRAWER_KEY, e.currentTarget.open ? 'open' : 'closed');
        } catch (err) { /* ignore */ }
    }

    function attachDrawerHandlers() {
        const drawer = document.querySelector('.market-drawer');
        if (!drawer || drawer.dataset.bound) return;
        drawer.dataset.bound = 'true';
        drawer.addEventListener('toggle', markUserToggled);
    }

    // Pull-to-refresh ---------------------------------------------
    //
    // Tracks vertical drag while the page is scrolled to the top; once
    // the drag exceeds PULL_REFRESH_THRESHOLD the indicator commits and
    // we force a full reload (which re-opens the SSE connection and
    // requests a fresh snapshot). Threshold ≥ 50px so scroll-bounce
    // doesn't accidentally trigger.

    // (PULL_REFRESH_THRESHOLD + pullStartY + pullDelta declared at the
    // top of the IIFE — see TDZ note.)

    function setPullIndicator(label, visible) {
        const el = document.querySelector('[data-pull-indicator]');
        if (!el) return;
        el.textContent = label;
        el.classList.toggle('pull-refresh-indicator--visible', visible);
    }

    function onPullStart(e) {
        if (window.scrollY > 0) { pullStartY = null; return; }
        pullStartY = e.touches[0].clientY;
        pullDelta = 0;
    }

    function onPullMove(e) {
        if (pullStartY === null) return;
        pullDelta = e.touches[0].clientY - pullStartY;
        if (pullDelta <= 0) { setPullIndicator('', false); return; }
        setPullIndicator(
            pullDelta >= PULL_REFRESH_THRESHOLD ? '↻ Release to refresh' : '↓ Pull to refresh',
            true,
        );
    }

    function onPullEnd() {
        if (pullStartY !== null && pullDelta >= PULL_REFRESH_THRESHOLD) {
            setPullIndicator('↻ Refreshing…', true);
            window.location.reload();
            return;
        }
        setPullIndicator('', false);
        pullStartY = null;
        pullDelta = 0;
    }

    document.addEventListener('touchstart', onPullStart, { passive: true });
    document.addEventListener('touchmove', onPullMove, { passive: true });
    document.addEventListener('touchend', onPullEnd);
    document.addEventListener('touchcancel', onPullEnd);

    // Relative "Updated 0:02 ago" timestamp -------------------------
    //
    // Server emits an ISO timestamp into data-updated-at; we render a
    // short human-readable offset every few seconds. Keeps the same
    // semantics as the countdown JS — formatting on the client means
    // we don't push small-text-only updates from the server.
    function fmtAge(ms) {
        if (!Number.isFinite(ms) || ms < 0) return '';
        const seconds = Math.floor(ms / 1000);
        if (seconds < 5) return 'just now';
        if (seconds < 60) return `Updated ${seconds}s ago`;
        const mins = Math.floor(seconds / 60);
        const remSec = seconds % 60;
        if (mins < 60) return `Updated ${mins}:${String(remSec).padStart(2, '0')} ago`;
        const hours = Math.floor(mins / 60);
        return `Updated ${hours}h ago`;
    }

    function tickTimestamps() {
        const now = Date.now();
        document.querySelectorAll('[data-updated-at]').forEach(el => {
            const iso = el.getAttribute('data-updated-at');
            if (!iso) return;
            const ts = Date.parse(iso);
            if (!Number.isNaN(ts)) el.textContent = fmtAge(now - ts);
        });
    }

    function resetTimestampsToNow() {
        // Called on every SSE / HTMX swap so "Updated 0:02 ago" reflects
        // the freshness of the latest event, not the initial page-load
        // time. The header strip lives outside the SSE swap surface,
        // so without this the timestamp would creep up forever even as
        // ticks flowed in.
        const iso = new Date().toISOString();
        document.querySelectorAll('[data-updated-at]').forEach(el => {
            el.setAttribute('data-updated-at', iso);
        });
        tickTimestamps();
    }

    setInterval(tickTimestamps, 5_000);

    // Long-press handler — dispatches the same row-detail event the
    // Alpine.js contextmenu handler fires, so desktop right-click and
    // mobile touch-hold share one code path.
    // (LONG_PRESS_MS + longPressTimer declared at the top of the IIFE
    // — see TDZ note.)

    function clearLongPress() {
        if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    }

    // Right-click on a row dispatches the same row-detail event Alpine
    // listens for. Delegated at the document level so SSE-swapped rows
    // are covered automatically — Alpine 3 doesn't auto-init swapped
    // DOM, so an inline @contextmenu binding would silently break the
    // moment the first SSE tick replaces a row.
    document.addEventListener('contextmenu', function (e) {
        const tr = e.target && e.target.closest && e.target.closest('[data-row-detail]');
        if (!tr) return;
        e.preventDefault();
        tr.dispatchEvent(new CustomEvent('row-detail', {
            detail: { ...tr.dataset }, bubbles: true,
        }));
    });

    function attachLongPressHandlers() {
        document.querySelectorAll('[data-row-detail]').forEach(tr => {
            if (tr.dataset.longPressBound) return;
            tr.dataset.longPressBound = 'true';
            tr.addEventListener('touchstart', e => {
                clearLongPress();
                longPressTimer = setTimeout(() => {
                    tr.dispatchEvent(new CustomEvent('row-detail', {
                        detail: { ...tr.dataset }, bubbles: true,
                    }));
                }, LONG_PRESS_MS);
            }, { passive: true });
            tr.addEventListener('touchend', clearLongPress);
            tr.addEventListener('touchmove', clearLongPress);
            tr.addEventListener('touchcancel', clearLongPress);
        });
    }

    function onReady() {
        tick();
        tickTimestamps();
        syncDrawerDefault();
        attachDrawerHandlers();
        initSort();
        attachLongPressHandlers();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', onReady);
    } else {
        onReady();
    }
    // Re-run on every minute boundary so the displayed offset stays fresh.
    setInterval(tick, 60_000);
    // Column-header sort cycling -------------------------------------
    //
    // [unset] → desc → asc → unset, persisted to localStorage under
    // 'portfolio-hub.sort'. Pure DOM reorder; sort comparison reads
    // data-<key> attributes off each <tr> (numeric).

    // (SORT_KEY + DEFAULT_SORT declared at the top of the IIFE — see
    // TDZ note.)

    function loadSort() {
        try {
            const raw = localStorage.getItem(SORT_KEY);
            if (!raw) return DEFAULT_SORT;
            const parsed = JSON.parse(raw);
            if (parsed && parsed.key && (parsed.dir === 'asc' || parsed.dir === 'desc' || parsed.dir === null)) {
                return parsed;
            }
        } catch (e) { /* ignore */ }
        return DEFAULT_SORT;
    }

    function saveSort(s) {
        try { localStorage.setItem(SORT_KEY, JSON.stringify(s)); } catch (e) {}
    }

    function dataKeyAttr(key) {
        return 'data-' + key.replace(/_/g, '-');
    }

    function applySort(state) {
        const tbody = document.querySelector('#positions-tbody');
        if (!tbody) return;
        // Clear all indicators
        document.querySelectorAll('[data-sort-indicator]').forEach(el => { el.textContent = ''; });
        // Update active indicator
        document.querySelectorAll('th.sortable').forEach(th => {
            th.classList.remove('sort-active');
            if (th.dataset.sortKey === state.key && state.dir) {
                th.classList.add('sort-active');
                const ind = th.querySelector('[data-sort-indicator]');
                if (ind) ind.textContent = state.dir === 'desc' ? '▼' : '▲';
            }
        });
        if (!state.dir) return;  // unsorted — leave row order as-is
        const rows = Array.from(tbody.querySelectorAll('tr'));
        const attr = dataKeyAttr(state.key);
        rows.sort((a, b) => {
            const av = parseFloat(a.getAttribute(attr)) || 0;
            const bv = parseFloat(b.getAttribute(attr)) || 0;
            return state.dir === 'desc' ? bv - av : av - bv;
        });
        rows.forEach(r => tbody.appendChild(r));
    }

    function cycleSort(key) {
        const current = loadSort();
        let next;
        if (current.key !== key) {
            next = { key, dir: 'desc' };
        } else if (current.dir === 'desc') {
            next = { key, dir: 'asc' };
        } else if (current.dir === 'asc') {
            next = { key: null, dir: null };
        } else {
            next = { key, dir: 'desc' };
        }
        saveSort(next);
        applySort(next.dir ? next : DEFAULT_SORT);
        // When user cycles to unsorted, we still apply the default visual
        // ordering so the table doesn't jump randomly.
        if (!next.dir) {
            document.querySelectorAll('[data-sort-indicator]').forEach(el => { el.textContent = ''; });
        }
    }

    function attachSortHandlers() {
        document.querySelectorAll('th.sortable').forEach(th => {
            if (th.dataset.sortBound) return;
            th.dataset.sortBound = 'true';
            th.addEventListener('click', () => cycleSort(th.dataset.sortKey));
            th.addEventListener('keydown', e => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    cycleSort(th.dataset.sortKey);
                }
            });
        });
    }

    function initSort() {
        attachSortHandlers();
        applySort(loadSort());
    }

    // HTMX swaps replace nodes; rerun after each swap so newly inserted
    // cards get a countdown immediately. Attach to `document` (not body)
    // because the body itself may be the swap target — a body-scoped
    // listener disappears with the old body element.
    function postSwapRehydrate() {
        tick();
        attachDrawerHandlers();
        initSort();
        attachLongPressHandlers();
        resetTimestampsToNow();
    }

    document.addEventListener('htmx:afterSwap', postSwapRehydrate);
    // htmx-ext-sse fires a separate event on incoming SSE messages.
    // Hook it so SSE-only ticks also refresh the "Updated 0:02 ago".
    document.addEventListener('htmx:sseMessage', resetTimestampsToNow);
})();
