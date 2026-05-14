"""Tests for the SSE-related markup on the index page.

For HTMX SSE to wire up the live updates:
  - The htmx-ext-sse extension must be loaded
  - The tbody must have hx-ext="sse", sse-connect="/stream/holdings",
    sse-swap listening for "snapshot,positions"
  - Each row must have a stable, unique id so HTMX can target it later
    (slice 8 will add OOB swaps; the id is harmless extra data for now)
"""

from fastapi.testclient import TestClient

from app.core.broker import Position


def _new_position(**overrides) -> Position:
    base = dict(
        broker="IBKR",
        account_id="U7575980",
        native_key="76792991",
        canonical_symbol="700.HK",
        native_symbol="700",
        exchange="SEHK",
        currency="HKD",
        name_en="TENCENT",
        asset_class="STK",
        quantity=100.0,
        avg_cost=400.0,
        last_price=420.0,
        market_value_native=42000.0,
        market_value_usd=0.0,
        unrealized_pnl_native=2000.0,
        unrealized_pnl_usd=0.0,
    )
    base.update(overrides)
    return Position(**base)


class FakeAdapter:
    def __init__(self, *, positions=None):
        self.name = "IBKR"
        self._positions = positions or []

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self):
        from app.core.broker import ConnectionState
        return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self): return []


def make_client(positions=None) -> TestClient:
    from app.main import create_app

    app = create_app(broker=FakeAdapter(positions=positions or []))
    return TestClient(app)


# Extension and connection -----------------------------------------------------


def test_index_loads_htmx_sse_extension():
    client = make_client()

    response = client.get("/")

    # htmx-ext-sse must be present as a script reference
    assert "ext/sse" in response.text or "htmx-ext-sse" in response.text


def test_index_tbody_declares_sse_extension_and_connect_url():
    client = make_client(positions=[_new_position()])

    response = client.get("/")

    assert 'hx-ext="sse"' in response.text
    assert 'sse-connect="/stream/holdings"' in response.text


def test_index_tbody_swaps_on_both_snapshot_and_positions_events():
    client = make_client(positions=[_new_position()])

    response = client.get("/")

    # HTMX's sse-swap takes a comma-separated event list; either order is fine.
    assert 'sse-swap="snapshot,positions"' in response.text or 'sse-swap="positions,snapshot"' in response.text


# Row identity ----------------------------------------------------------------


def test_each_position_row_has_a_stable_unique_id():
    client = make_client(
        positions=[
            _new_position(canonical_symbol="700.HK"),
            _new_position(
                canonical_symbol="AAPL.US",
                native_key="2",
                native_symbol="AAPL",
                exchange="NASDAQ",
                currency="USD",
                name_en="APPLE INC",
            ),
        ]
    )

    response = client.get("/")

    # ids should incorporate broker, account_id, and canonical_symbol so
    # multi-broker / multi-account scenarios stay distinct
    assert 'id="row-IBKR-U7575980-700.HK"' in response.text
    assert 'id="row-IBKR-U7575980-AAPL.US"' in response.text
