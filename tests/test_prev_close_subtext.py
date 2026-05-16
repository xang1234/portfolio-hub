"""Row template shows 'prev close' subtext when last_price was filled
from historical data rather than a live tick."""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


def _make_position(*, last_price_is_previous_close: bool) -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key="14016494",
        canonical_symbol="6315.JP", native_symbol="6315",
        exchange="TSEJ", currency="JPY",
        name_en="TOYO ENGINEERING CORP", asset_class="STK",
        quantity=200.0, avg_cost=1000.0, last_price=1100.0,
        market_value_native=220000.0, market_value_usd=1408.0,
        unrealized_pnl_native=20000.0, unrealized_pnl_usd=128.0,
        last_price_is_previous_close=last_price_is_previous_close,
    )


class FakeAdapter:
    def __init__(self, positions):
        self.name = "IBKR"
        self._positions = positions
    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self): return []


def make_client(positions):
    from app.main import create_app
    return TestClient(create_app(broker=FakeAdapter(positions)))


def test_prev_close_subtext_appears_when_flag_set():
    pos = _make_position(last_price_is_previous_close=True)
    client = make_client([pos])

    response = client.get("/")

    assert "prev close" in response.text.lower()


def test_no_prev_close_subtext_for_live_price():
    pos = _make_position(last_price_is_previous_close=False)
    client = make_client([pos])

    response = client.get("/")

    assert "prev close" not in response.text.lower()
