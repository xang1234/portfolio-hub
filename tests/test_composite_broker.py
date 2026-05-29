import pytest

from app.core.broker import AccountSummary, ConnectionState, Position


def _pos(broker: str, symbol: str) -> Position:
    return Position(
        broker=broker,
        account_id=f"{broker}-1",
        native_key=symbol,
        canonical_symbol=symbol,
        native_symbol=symbol.split(".")[0],
        exchange="NYSE",
        currency="USD",
        name_en=symbol,
        asset_class="STK",
        quantity=1.0,
        avg_cost=10.0,
        last_price=12.0,
        market_value_native=12.0,
        market_value_usd=12.0,
        unrealized_pnl_native=2.0,
        unrealized_pnl_usd=2.0,
    )


class _Adapter:
    def __init__(
        self,
        name,
        *,
        state=ConnectionState.CONNECTED,
        positions=None,
        summaries=None,
        start_state=None,
        backoff_delay=None,
    ):
        self.name = name
        self._state = state
        self._positions = positions or []
        self._summaries = summaries or []
        self._start_state = start_state
        self._backoff_delay = backoff_delay
        self.started = False
        self.disconnected = False

    async def start(self):
        self.started = True
        if self._start_state is not None:
            self._state = self._start_state

    async def connect(self):
        self.started = True

    async def disconnect(self):
        self.disconnected = True

    async def is_connected(self):
        return self._state is ConnectionState.CONNECTED

    async def get_connection_state(self):
        return self._state

    async def get_positions(self):
        return list(self._positions)

    async def get_account_summary(self):
        return list(self._summaries)

    def current_backoff_delay(self):
        return self._backoff_delay


@pytest.mark.asyncio
async def test_composite_broker_aggregates_positions_and_summaries():
    from app.core.composite_broker import CompositeBroker

    ibkr = _Adapter(
        "IBKR",
        positions=[_pos("IBKR", "AAPL.US")],
        summaries=[
            AccountSummary(
                broker="IBKR",
                account_id="U1",
                base_currency="USD",
                net_liquidation_usd=100.0,
                cash_usd=10.0,
                buying_power_usd=20.0,
            )
        ],
    )
    futu = _Adapter(
        "Futu",
        positions=[_pos("Futu", "1810.HK")],
        summaries=[
            AccountSummary(
                broker="Futu",
                account_id="281756479345015383",
                base_currency="USD",
                net_liquidation_usd=200.0,
                cash_usd=15.0,
                buying_power_usd=30.0,
            )
        ],
    )
    broker = CompositeBroker([ibkr, futu])

    assert [p.broker for p in await broker.get_positions()] == ["IBKR", "Futu"]
    assert [s.broker for s in await broker.get_account_summary()] == ["IBKR", "Futu"]


@pytest.mark.asyncio
async def test_composite_broker_reports_reconnecting_when_one_enabled_broker_is_down():
    from app.core.composite_broker import CompositeBroker

    broker = CompositeBroker([
        _Adapter("IBKR", state=ConnectionState.CONNECTED),
        _Adapter("Futu", state=ConnectionState.DISCONNECTED),
    ])

    assert await broker.get_connection_state() is ConnectionState.RECONNECTING
    assert await broker.get_connection_states() == {
        "IBKR": ConnectionState.CONNECTED,
        "Futu": ConnectionState.DISCONNECTED,
    }


def test_composite_broker_forwards_reconnecting_child_backoff_delay():
    from app.core.composite_broker import CompositeBroker

    broker = CompositeBroker([
        _Adapter("IBKR", state=ConnectionState.CONNECTED),
        _Adapter("Futu", state=ConnectionState.RECONNECTING, backoff_delay=15.0),
    ])

    assert broker.current_backoff_delay() == 15.0


@pytest.mark.asyncio
async def test_composite_broker_starts_and_disconnects_children():
    from app.core.composite_broker import CompositeBroker

    ibkr = _Adapter("IBKR")
    futu = _Adapter("Futu")
    broker = CompositeBroker([ibkr, futu])

    await broker.start()
    await broker.disconnect()

    assert ibkr.started is True
    assert futu.started is True
    assert ibkr.disconnected is True
    assert futu.disconnected is True


def test_healthz_reports_each_child_broker_for_composite_broker():
    from fastapi.testclient import TestClient

    from app.core.composite_broker import CompositeBroker
    from app.main import create_app

    app = create_app(
        broker=CompositeBroker([
            _Adapter("IBKR", state=ConnectionState.CONNECTED),
            _Adapter("Futu", state=ConnectionState.DISCONNECTED),
        ])
    )

    response = TestClient(app).get("/healthz")

    assert response.json() == {
        "ibkr": "connected",
        "futu": "disconnected",
    }


def test_healthz_retry_restarts_disconnected_child_in_composite_broker():
    from fastapi.testclient import TestClient

    from app.core.composite_broker import CompositeBroker
    from app.main import create_app

    ibkr = _Adapter("IBKR", state=ConnectionState.CONNECTED)
    futu = _Adapter(
        "Futu",
        state=ConnectionState.DISCONNECTED,
        start_state=ConnectionState.CONNECTED,
    )
    app = create_app(broker=CompositeBroker([ibkr, futu]))

    response = TestClient(app).post("/healthz/retry")

    assert response.status_code == 200
    assert ibkr.started is False
    assert futu.started is True
    assert "connected" in response.text
