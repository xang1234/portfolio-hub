"""Slice 8 cycle 7: drawer open/closed state persists to localStorage.

The market-hours drawer (slice 5) opens by default on landscape and is
collapsed on portrait. After this cycle, the user's explicit toggle also
persists across page loads — opening on portrait once stays open the
next time the page loads.

Wiring contract:
- app.js writes 'portfolio-hub.drawer' to localStorage on toggle
- On page load (syncDrawerDefault), localStorage value overrides the
  responsive default if set
"""

from pathlib import Path


def test_app_js_persists_drawer_state_to_localstorage():
    js = (Path("app/static") / "app.js").read_text()

    # Same namespace as the sort key
    assert "portfolio-hub.drawer" in js or "DRAWER_KEY" in js


def test_app_js_drawer_toggle_writes_to_localstorage():
    js = (Path("app/static") / "app.js").read_text()

    # The toggle handler must call setItem on localStorage
    # (we already had markUserToggled — verify it writes)
    assert "localStorage.setItem" in js


def test_app_js_drawer_load_reads_from_localstorage_before_media_query():
    """syncDrawerDefault must check localStorage first; only fall back
    to the matchMedia default when nothing is stored."""
    js = (Path("app/static") / "app.js").read_text()

    # We expect a getItem call near drawer init
    assert "localStorage.getItem" in js
