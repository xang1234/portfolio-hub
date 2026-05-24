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

    // Sort + theme persistence (localStorage keys)
    const SORT_KEY = 'portfolio-hub.sort';
    const DEFAULT_SORT = { key: 'mv_usd', dir: 'desc' };
    const THEME_KEY = 'portfolio-hub.theme';

    // Map<row-id, last-seen-price> snapshot taken in htmx:beforeSwap so
    // afterSwap can diff and tick-flash only the rows whose price changed.
    let _pendingPriceSnapshot = null;

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

    // Manual theme toggle. Cycle: auto (no attr override) → dark → light → auto.
    // The base.html ships <html data-theme="auto">; we override on user intent
    // and persist so the choice survives reloads. When the user clears their
    // override (cycles back to auto), we restore the auto value so the CSS
    // media query takes over again.
    function readStoredTheme() {
        try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
    }
    function writeStoredTheme(t) {
        try {
            if (t === null) localStorage.removeItem(THEME_KEY);
            else localStorage.setItem(THEME_KEY, t);
        } catch (e) { /* ignore */ }
    }
    function applyTheme(t) {
        document.documentElement.setAttribute(
            'data-theme', (t === 'light' || t === 'dark') ? t : 'auto'
        );
    }
    function cycleTheme() {
        const cur = readStoredTheme();
        let next;
        if (cur === null)         next = 'dark';
        else if (cur === 'dark')  next = 'light';
        else                      next = null;  // 'light' → null (back to auto)
        writeStoredTheme(next);
        applyTheme(next);
    }
    function attachThemeToggle() {
        document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
            if (btn.dataset.themeBound) return;
            btn.dataset.themeBound = 'true';
            btn.addEventListener('click', cycleTheme);
        });
    }

    // Holdings client-side search. Hides rows whose name/ticker don't match
    // the (case-insensitive) substring. Pure DOM — does not touch the SSE
    // subscription so live ticks keep flowing into hidden rows.
    function applySearchToRows() {
        const input = document.querySelector('[data-holdings-search]');
        if (!input) return;
        const q = input.value.trim().toLowerCase();
        const rows = document.querySelectorAll('#positions-tbody .position-row');
        rows.forEach(r => {
            if (!q) { r.hidden = false; return; }
            const name = (r.getAttribute('data-name-en') || '').toLowerCase();
            const sym = (r.getAttribute('data-canonical-symbol') || '').toLowerCase();
            r.hidden = !(name.includes(q) || sym.includes(q));
        });
    }
    function attachSearchHandler() {
        document.querySelectorAll('[data-holdings-search]').forEach(input => {
            if (input.dataset.searchBound) return;
            input.dataset.searchBound = 'true';
            input.addEventListener('input', applySearchToRows);
        });
    }

    // Hero sparkline. Fetches /api/equity-history (filtered by the active
    // account in the URL) and renders a stroke+fill SVG path inside the
    // [data-spark] slot. Color follows first-vs-last sign. The endpoint
    // returns [] when no snapshots exist or when the app has no Store
    // attached (tests skip lifespan), in which case .spark:empty in CSS
    // hides the slot so the hero doesn't keep a 220px hole.
    function _sparkPath(data, w, h) {
        const padX = 1, padY = 3;
        const xs = data.map(d => Date.parse(d.t));
        const ys = data.map(d => d.v);
        const xMin = xs[0], xMax = xs[xs.length - 1];
        const yMin = Math.min.apply(null, ys);
        const yMax = Math.max.apply(null, ys);
        const xSpan = (xMax - xMin) || 1;
        const ySpan = (yMax - yMin) || 1;
        const px = i => padX + (xs[i] - xMin) / xSpan * (w - 2 * padX);
        const py = i => h - padY - (ys[i] - yMin) / ySpan * (h - 2 * padY);
        let d = '';
        for (let i = 0; i < data.length; i++) {
            d += (i === 0 ? 'M' : 'L') + px(i).toFixed(1) + ',' + py(i).toFixed(1) + ' ';
        }
        const lastX = px(data.length - 1);
        const fill = d + 'L' + lastX.toFixed(1) + ',' + h + ' L' + padX + ',' + h + ' Z';
        const isUp = ys[ys.length - 1] >= ys[0];
        return { stroke: d.trim(), fill, isUp };
    }
    function _renderSpark(svg, data) {
        if (!data || data.length < 2) { svg.innerHTML = ''; return; }
        const vb = svg.viewBox && svg.viewBox.baseVal;
        const w = (vb && vb.width)  || 220;
        const h = (vb && vb.height) ||  48;
        const { stroke, fill, isUp } = _sparkPath(data, w, h);
        const colorVar = isUp ? '--ph-pos' : '--ph-neg';
        // Random gradient id so multiple sparklines on one page (a future
        // detail panel could embed another) can't collide.
        const gid = 'sparkfill-' + Math.random().toString(36).slice(2, 8);
        // Build with raw HTML — innerHTML on an SVG creates SVG children in
        // the right namespace in every modern browser.
        svg.innerHTML =
            '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
            '<stop offset="0" stop-color="var(' + colorVar + ')" stop-opacity="0.30"/>' +
            '<stop offset="1" stop-color="var(' + colorVar + ')" stop-opacity="0"/>' +
            '</linearGradient></defs>' +
            '<path d="' + fill + '" fill="url(#' + gid + ')"/>' +
            '<path d="' + stroke + '" fill="none" stroke="var(' + colorVar + ')" ' +
            'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>';
    }
    async function _fetchSparkData(account) {
        const params = new URLSearchParams({ days: '60' });
        if (account && account !== 'All') params.set('account', account);
        try {
            const res = await fetch('/api/equity-history?' + params.toString(),
                                    { headers: { 'Accept': 'application/json' } });
            if (!res.ok) return [];
            return await res.json();
        } catch (e) { return []; }
    }
    function attachSparkline() {
        const svg = document.querySelector('[data-spark]');
        if (!svg) return;
        const account = new URLSearchParams(window.location.search).get('account');
        _fetchSparkData(account).then(data => _renderSpark(svg, data));
    }

    // Tick-flash for SSE price updates. We snapshot prices in htmx:beforeSwap
    // (rows are about to be replaced), then in afterSwap compare each new row
    // to its previous value and animate it green/red briefly. Skips rows that
    // didn't change and rows seen for the first time. Direction is taken from
    // the absolute price delta, not the day-change, so an intraday gainer
    // that ticks down still flashes red on the dip.
    function snapshotPricesInto(map) {
        const rows = document.querySelectorAll('#positions-tbody .position-row');
        rows.forEach(r => {
            map.set(r.id, parseFloat(r.getAttribute('data-last-price')) || 0);
        });
    }
    function captureBeforeSwap(e) {
        const tgt = e && e.detail && e.detail.target;
        if (!tgt || tgt.id !== 'positions-tbody') return;
        _pendingPriceSnapshot = new Map();
        snapshotPricesInto(_pendingPriceSnapshot);
    }
    function flashTickedRows() {
        if (!_pendingPriceSnapshot) return;
        const snap = _pendingPriceSnapshot;
        _pendingPriceSnapshot = null;
        const rows = document.querySelectorAll('#positions-tbody .position-row');
        rows.forEach(r => {
            const prev = snap.get(r.id);
            const next = parseFloat(r.getAttribute('data-last-price')) || 0;
            if (prev == null || !prev || !next || next === prev) return;
            const cls = next > prev ? 'tick-flash-up' : 'tick-flash-down';
            r.classList.remove('tick-flash-up', 'tick-flash-down');
            // Force reflow so the keyframe animation restarts from 0%.
            void r.offsetWidth;
            r.classList.add(cls);
            setTimeout(() => r.classList.remove(cls), 700);
        });
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
        applyTheme(readStoredTheme());
        attachThemeToggle();
        attachSearchHandler();
        attachSparkline();
        tick();
        tickTimestamps();
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
    function postSwapRehydrate(event) {
        tick();
        flashTickedRows();
        initSort();
        attachLongPressHandlers();
        attachThemeToggle();
        attachSearchHandler();
        applySearchToRows();
        // Refresh the sparkline only on swaps that could change its inputs
        // (full-body swap from chip filters). SSE row deltas tick many times
        // per minute and don't move equity-snapshot data, so refetching there
        // is wasteful and causes constant SVG repaints.
        const swapTarget = event && event.detail && event.detail.target;
        if (!swapTarget || swapTarget.id !== 'positions-tbody') {
            attachSparkline();
        }
        resetTimestampsToNow();
    }

    document.addEventListener('htmx:beforeSwap', captureBeforeSwap);
    document.addEventListener('htmx:afterSwap', postSwapRehydrate);
    // htmx-ext-sse fires a separate event on incoming SSE messages.
    // Hook it so SSE-only ticks also refresh the "Updated 0:02 ago".
    document.addEventListener('htmx:sseMessage', resetTimestampsToNow);
})();
