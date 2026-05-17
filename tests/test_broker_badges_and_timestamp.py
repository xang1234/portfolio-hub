"""Slice 8 cycle 5: broker connection badges row + Updated-X-ago timestamp.

Header strip rows from the plan:
  Row 1: totals (slice 3)
  Row 2: broker connection badges  ← this cycle
  Row 3: filter chips (cycles 1, 2, slice 7)
  Row 4 (optional): per-account summary (slice 7)

Each known broker gets a small badge:
  - Enabled + connected:    🟢 IBKR
  - Enabled + disconnected: 🔴 IBKR
  - Disabled (future):      ⚫ Futu  (grayed)

Plus a tiny "Updated 0:02 ago" text in a corner — client-side JS reads
data-updated-at and renders a relative time, refreshed every few seconds.
"""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


class _FakeAdapter:
    name = "IBKR"

    def __init__(self, positions, state=ConnectionState.CONNECTED):
        self._positions = positions
        self._state = state

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return self._state is ConnectionState.CONNECTED
    async def get_connection_state(self): return self._state
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


def _client(positions, state=ConnectionState.CONNECTED):
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=positions, state=state))
    return TestClient(app)


# Broker badges row -----------------------------------------------------


def test_broker_badges_row_present():
    """A nav-level element with every broker's badge must render."""
    response = _client([_pos()]).get("/")

    assert "broker-badges" in response.text


def test_enabled_connected_broker_shows_green_circle():
    response = _client([_pos()], ConnectionState.CONNECTED).get("/")

    text = response.text
    # Some marker that the IBKR badge is in the connected state
    assert "🟢" in text and "IBKR" in text


def test_disabled_brokers_render_as_grayed_badges():
    response = _client([_pos()]).get("/")

    text = response.text
    # All four brokers visible
    for b in ("IBKR", "Futu", "Tiger", "Longbridge"):
        assert b in text
    # The disabled ones carry the same class as the disabled chips
    assert "broker-badge--disabled" in text


def test_disconnected_enabled_broker_shows_red_circle():
    """When IBKR is disconnected, the badge must reflect that."""
    response = _client([_pos()], ConnectionState.DISCONNECTED).get("/")

    text = response.text
    # We already use 🔴 elsewhere for disconnected; reuse for the badge
    assert "🔴" in text


# Updated-X-ago timestamp ----------------------------------------------


def test_updated_timestamp_slot_present():
    """A small element with a data-updated-at attribute that JS
    converts to 'Updated 0:02 ago'."""
    response = _client([_pos()]).get("/")

    text = response.text
    assert "data-updated-at" in text
    assert "updated-timestamp" in text


def test_app_js_renders_relative_updated_timestamp():
    from pathlib import Path
    js = (Path("app/static") / "app.js").read_text()

    # JS must read data-updated-at and write a relative-time string
    assert "updatedAt" in js or "data-updated-at" in js
    assert "Updated" in js
