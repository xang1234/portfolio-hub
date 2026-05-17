"""Slice 8 cycle 6: pull-to-refresh on mobile.

A custom touch handler tracks the gesture and triggers a full SSE
reconnect + snapshot fetch when the user pulls down past a threshold
while scrolled to the top of the page.

Wiring contract (full UX behavior verified manually):
- A small `pull-refresh-indicator` element exists in the page so the
  JS has somewhere to render the spinner/text.
- app.js binds touchstart / touchmove / touchend handlers and uses
  `htmx.trigger` (or the equivalent fetch) to force a snapshot reload.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


class _FakeAdapter:
    name = "IBKR"

    def __init__(self, positions):
        self._positions = positions

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self): return []


def _pos():
    return Position(
        broker="IBKR", account_id="U1", native_key="1",
        canonical_symbol="AAPL.US", native_symbol="AAPL",
        exchange="NYSE", currency="USD",
        name_en="APPLE", asset_class="STK",
        quantity=10.0, avg_cost=100.0, last_price=110.0,
        market_value_native=1100.0, market_value_usd=1100.0,
        unrealized_pnl_native=100.0, unrealized_pnl_usd=100.0,
    )


def _client(positions):
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=positions))
    return TestClient(app)


def test_pull_refresh_indicator_element_exists():
    """A pull-refresh indicator slot must exist for the JS to populate."""
    response = _client([_pos()]).get("/")

    assert "pull-refresh-indicator" in response.text


def test_app_js_implements_pull_to_refresh():
    js = (Path("app/static") / "app.js").read_text()

    # The handler tracks touch deltaY against a threshold and forces
    # a full snapshot reload when it crosses
    assert "touchstart" in js
    assert "touchmove" in js
    # Some kind of refresh / reload trigger
    assert "refresh" in js.lower() or "reload" in js.lower()


def test_app_js_pull_threshold_is_reasonable():
    """Avoid accidental triggers — threshold must be at least 50px so
    casual scroll-bounce doesn't fire a refresh."""
    js = (Path("app/static") / "app.js").read_text()

    # Look for a numeric threshold constant
    import re
    thresholds = re.findall(r'PULL_(?:THRESHOLD|REFRESH)_?\w*\s*=\s*(\d+)', js)
    assert thresholds, "No PULL_THRESHOLD constant found"
    assert all(int(t) >= 50 for t in thresholds)
