// Synchronously apply a stored theme override (set by app.js theme toggle)
// before stylesheets paint, so users with a non-system preference don't see a
// one-frame flash of the wrong palette. Default <html data-theme="auto"> falls
// through to the prefers-color-scheme media query.
//
// Lives in its own file (not inline) so the page can ship a strict
// Content-Security-Policy with `script-src 'self'` — an inline <script> would
// require 'unsafe-inline', which defeats most of the XSS protection a CSP buys.
(function () {
    try {
        var t = localStorage.getItem('portfolio-hub.theme');
        if (t === 'dark' || t === 'light') {
            document.documentElement.setAttribute('data-theme', t);
        }
    } catch (e) { /* ignore */ }
})();
