import asyncio

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


class _RetryNowAdapter(_Adapter):
    def __init__(self, name, **kwargs):
        super().__init__(name, **kwargs)
        self.retry_now_calls = 0

    async def retry_now(self):
        self.retry_now_calls += 1
        self._state = ConnectionState.CONNECTED


class _FailingRetryNowAdapter(_Adapter):
    def __init__(self, name, **kwargs):
        super().__init__(name, **kwargs)
        self.retry_now_calls = 0

    async def retry_now(self):
        self.retry_now_calls += 1
        raise RuntimeError("retry unavailable")


class _CancellingRetryNowAdapter(_Adapter):
    def __init__(self, name, **kwargs):
        super().__init__(name, **kwargs)
        self.retry_now_calls = 0

    async def retry_now(self):
        self.retry_now_calls += 1
        raise asyncio.CancelledError()


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


def test_healthz_reports_longbridge_broker():
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(broker=_Adapter("Longbridge", state=ConnectionState.CONNECTED))

    response = TestClient(app).get("/healthz")

    assert response.json() == {"longbridge": "connected"}


def test_healthz_reports_tiger_broker():
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(broker=_Adapter("Tiger", state=ConnectionState.CONNECTED))

    response = TestClient(app).get("/healthz")

    assert response.json() == {"tiger": "connected"}


def test_build_production_broker_includes_longbridge_when_enabled(monkeypatch):
    from app.core.live_positions import LivePositions
    from app.main import _build_production_broker

    class _FakeLongbridgeAdapter(_Adapter):
        def __init__(self, **kwargs):
            super().__init__("Longbridge")
            self.kwargs = kwargs

    monkeypatch.setenv("BROKERS_ENABLED", "longbridge")
    monkeypatch.setenv("LONGBRIDGE_POSITION_CHANNEL", "FUND")
    monkeypatch.setattr(
        "app.adapters.longbridge.LongbridgeAdapter",
        _FakeLongbridgeAdapter,
    )

    broker = _build_production_broker(
        store=object(),
        live_positions=LivePositions(),
        fx_service=object(),
    )

    assert broker.name == "Longbridge"
    assert broker.kwargs["account_id"] == "Longbridge"
    assert broker.kwargs["position_channel"] == "FUND"


def test_build_production_broker_includes_tiger_when_enabled(monkeypatch):
    from app.core.live_positions import LivePositions
    from app.main import _build_production_broker

    class _FakeTigerAdapter(_Adapter):
        def __init__(self):
            super().__init__("Tiger")
            self.factory_kwargs = None

        @classmethod
        def from_env(cls, **kwargs):
            adapter = cls()
            adapter.factory_kwargs = kwargs
            return adapter

    monkeypatch.setenv("BROKERS_ENABLED", "tiger")
    monkeypatch.setenv("TIGER_CONFIG_DIR", "/secure/tiger")
    monkeypatch.setenv("TIGER_ACCOUNT", "TIGER-1")
    monkeypatch.setenv("TIGER_BASE_CURRENCY", "USD")
    monkeypatch.setenv("TIGER_MARKETS", "US,HK,SG")
    monkeypatch.setattr(
        "app.adapters.tiger.TigerAdapter",
        _FakeTigerAdapter,
    )

    broker = _build_production_broker(
        store=object(),
        live_positions=LivePositions(),
        fx_service=object(),
    )

    assert broker.name == "Tiger"
    assert broker.factory_kwargs["env"] is not None
    assert broker.factory_kwargs["env"]["TIGER_CONFIG_DIR"] == "/secure/tiger"
    assert broker.factory_kwargs["fx_service"] is not None
    assert isinstance(broker.factory_kwargs["live_positions"], LivePositions)


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


def test_healthz_retry_wakes_reconnecting_child_in_composite_broker():
    from fastapi.testclient import TestClient

    from app.core.composite_broker import CompositeBroker
    from app.main import create_app

    ibkr = _Adapter("IBKR", state=ConnectionState.CONNECTED)
    futu = _RetryNowAdapter("Futu", state=ConnectionState.RECONNECTING)
    app = create_app(broker=CompositeBroker([ibkr, futu]))

    response = TestClient(app).post("/healthz/retry")

    assert response.status_code == 200
    assert ibkr.started is False
    assert futu.retry_now_calls == 1
    assert futu.started is False
    assert "connected" in response.text


def test_healthz_retry_isolates_child_retry_failures_in_composite_broker(caplog):
    from fastapi.testclient import TestClient

    from app.core.composite_broker import CompositeBroker
    from app.main import create_app

    ibkr = _Adapter("IBKR", state=ConnectionState.CONNECTED)
    futu = _FailingRetryNowAdapter("Futu", state=ConnectionState.RECONNECTING)
    tiger = _Adapter(
        "Tiger",
        state=ConnectionState.DISCONNECTED,
        start_state=ConnectionState.CONNECTED,
    )
    app = create_app(broker=CompositeBroker([ibkr, futu, tiger]))

    with caplog.at_level("WARNING", logger="app.core.composite_broker"):
        response = TestClient(app).post("/healthz/retry")

    assert response.status_code == 200
    assert futu.retry_now_calls == 1
    assert tiger.started is True
    assert "Futu retry_now failed" in caplog.text


@pytest.mark.asyncio
async def test_composite_broker_retry_now_propagates_child_cancellation(caplog):
    from app.core.composite_broker import CompositeBroker

    futu = _CancellingRetryNowAdapter("Futu", state=ConnectionState.RECONNECTING)
    broker = CompositeBroker([futu])

    with caplog.at_level("WARNING", logger="app.core.composite_broker"):
        with pytest.raises(asyncio.CancelledError):
            await broker.retry_now()

    assert futu.retry_now_calls == 1
    assert "Futu retry_now failed" not in caplog.text
