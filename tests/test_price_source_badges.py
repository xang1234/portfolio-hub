from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


def _position(**overrides) -> Position:
    base = dict(
        broker="IBKR",
        account_id="U1",
        native_key="14016494",
        canonical_symbol="6315.JP",
        native_symbol="6315",
        exchange="TSEJ",
        currency="JPY",
        name_en="TOYO ENGINEERING CORP",
        asset_class="STK",
        quantity=200.0,
        avg_cost=1000.0,
        last_price=1100.0,
        market_value_native=220000.0,
        market_value_usd=1408.0,
        unrealized_pnl_native=20000.0,
        unrealized_pnl_usd=128.0,
    )
    base.update(overrides)
    return Position(**base)


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


def _client(positions):
    from app.main import create_app

    return TestClient(create_app(broker=_FakeAdapter(positions)))


def test_broker_mark_badge_highlights_portfolio_valuation_source():
    response = _client([_position(last_price_is_broker_mark=True)]).get("/")

    assert "broker mark" in response.text.lower()
    assert "IB portfolio valuation" in response.text


def test_delayed_badge_highlights_delayed_or_frozen_market_data():
    response = _client([_position(last_price_is_delayed=True)]).get("/")

    assert "delayed" in response.text.lower()
    assert "Delayed or frozen market data" in response.text


def test_live_price_has_no_price_source_badge():
    response = _client([_position()]).get("/")

    assert "broker mark" not in response.text.lower()
    assert "Delayed or frozen market data" not in response.text
