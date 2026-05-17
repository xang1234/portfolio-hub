"""Slice 9 cycle 5: holdings_row template renders stale-data badge.

When `position.last_price_is_stale` is True, the row must:
- carry a CSS hook (`position-row--stale` class) for opacity styling
- render a ⚠️ next to the last_price cell so it's obvious at a glance
"""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


def _stale_pos():
    return Position(
        broker="IBKR", account_id="U1", native_key="76792991",
        canonical_symbol="700.HK", native_symbol="700",
        exchange="SEHK", currency="HKD", name_en="TENCENT",
        asset_class="STK", quantity=100.0, avg_cost=380.0, last_price=420.0,
        market_value_native=42000.0, market_value_usd=5388.6,
        unrealized_pnl_native=4000.0, unrealized_pnl_usd=512.5,
        last_price_is_stale=True,
    )


def _fresh_pos():
    return Position(
        broker="IBKR", account_id="U1", native_key="76792991",
        canonical_symbol="700.HK", native_symbol="700",
        exchange="SEHK", currency="HKD", name_en="TENCENT",
        asset_class="STK", quantity=100.0, avg_cost=380.0, last_price=420.0,
        market_value_native=42000.0, market_value_usd=5388.6,
        unrealized_pnl_native=4000.0, unrealized_pnl_usd=512.5,
    )


class _FakeAdapter:
    name = "IBKR"
    def __init__(self, positions): self._positions = positions
    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self): return []


def _client(positions):
    from app.main import create_app
    return TestClient(create_app(broker=_FakeAdapter(positions=positions)))


def test_stale_position_renders_stale_class_on_row():
    response = _client([_stale_pos()]).get("/")
    assert "position-row--stale" in response.text


def test_stale_position_renders_warning_icon():
    """⚠️ next to the price tells the user the number isn't ticking right now."""
    response = _client([_stale_pos()]).get("/")
    # The icon lives in the last-price cell, alongside the price.
    assert "⚠️" in response.text


def test_fresh_position_does_not_render_stale_markers():
    response = _client([_fresh_pos()]).get("/")
    assert "position-row--stale" not in response.text
