"""Tests for IbkrAdapter's streaming market-data integration.

When the adapter is given a LivePositions, connect() should:
  1. Seed LivePositions with the initial snapshot (from reqPositionsAsync)
  2. Subscribe to streaming reqMktData for each STK contract
  3. Update LivePositions whenever a Ticker fires updateEvent

Backward compat: slice 2 tests don't pass a live_positions and still work.
"""

import asyncio
from dataclasses import dataclass

import pytest

from app.core.live_positions import LivePositions


# Test doubles -----------------------------------------------------------------


@dataclass
class FakeContract:
    conId: int
    symbol: str
    secType: str
    currency: str
    exchange: str = "SMART"
    primaryExchange: str = ""


@dataclass
class FakeContractDetails:
    contract: FakeContract
    longName: str


@dataclass
class FakeIBPosition:
    account: str
    contract: FakeContract
    position: float
    avgCost: float


@dataclass
class FakePortfolioItem:
    account: str
    contract: FakeContract
    position: float
    marketPrice: float
    marketValue: float
    averageCost: float
    unrealizedPNL: float


class _Event:
    """Mimics ib_async's Event object that supports `event += callback`."""

    def __init__(self):
        self._callbacks: list = []

    def __iadd__(self, callback):
        self._callbacks.append(callback)
        return self

    def __isub__(self, callback):
        if callback in self._callbacks:
            self._callbacks.remove(callback)
        return self

    def emit(self, *args, **kwargs):
        for cb in list(self._callbacks):
            cb(*args, **kwargs)


class FakeTicker:
    """Mimics ib_async's Ticker for testing."""

    def __init__(
        self,
        contract: FakeContract,
        last: float | None = None,
        marketDataType: int = 1,
    ) -> None:
        self.contract = contract
        self.last = last
        self.close = None
        self.marketDataType = marketDataType
        self.updateEvent = _Event()

    def marketPrice(self):
        return self.last

    def fire_tick(self, new_last: float) -> None:
        self.last = new_last
        self.updateEvent.emit(self)


class FakeIB:
    def __init__(
        self,
        positions: list[FakeIBPosition],
        contract_details: dict[int, FakeContractDetails],
        last_prices: dict[int, float] | None = None,
        portfolio_items: list[FakePortfolioItem] | None = None,
    ) -> None:
        self._positions = positions
        self._contract_details = contract_details
        self._last_prices = last_prices or {}
        self._portfolio_items = portfolio_items or []
        self._connected = False
        self.req_mkt_data_calls: list[int] = []
        self.cancel_mkt_data_calls: list[int] = []
        self.tickers_by_conid: dict[int, FakeTicker] = {}
        self.updatePortfolioEvent = _Event()

    async def connectAsync(self, host, port, clientId):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isConnected(self):
        return self._connected

    def reqMarketDataType(self, market_data_type: int) -> None:
        pass

    async def reqPositionsAsync(self):
        return self._positions

    async def reqContractDetailsAsync(self, contract):
        details = self._contract_details.get(contract.conId)
        return [details] if details is not None else []

    async def reqTickersAsync(self, *contracts):
        # Snapshot mode — return tickers populated from last_prices
        return [FakeTicker(c, last=self._last_prices.get(c.conId)) for c in contracts]

    def portfolio(self, account: str = ""):
        if account:
            return [p for p in self._portfolio_items if p.account == account]
        return list(self._portfolio_items)

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False):
        self.req_mkt_data_calls.append(contract.conId)
        ticker = FakeTicker(contract, last=self._last_prices.get(contract.conId))
        self.tickers_by_conid[contract.conId] = ticker
        return ticker

    def cancelMktData(self, contract):
        self.cancel_mkt_data_calls.append(contract.conId)


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _tencent_contract():
    return FakeContract(conId=76792991, symbol="700", secType="STK", currency="HKD")


def _tencent_details():
    return FakeContractDetails(
        contract=FakeContract(
            conId=76792991, symbol="700", secType="STK", currency="HKD", primaryExchange="SEHK"
        ),
        longName="TENCENT HOLDINGS LTD",
    )


def _tsej_contract():
    return FakeContract(
        conId=14016494, symbol="6315", secType="STK", currency="JPY",
        exchange="TSEJ",
    )


def _tsej_details():
    return FakeContractDetails(
        contract=FakeContract(
            conId=14016494, symbol="6315", secType="STK", currency="JPY",
            primaryExchange="TSEJ",
        ),
        longName="TOYO ENGINEERING CORP",
    )


def _lse_contract():
    return FakeContract(
        conId=14075064, symbol="IQE", secType="STK", currency="GBP",
        exchange="LSE", primaryExchange="LSE",
    )


def _lse_details():
    return FakeContractDetails(
        contract=FakeContract(
            conId=14075064, symbol="IQE", secType="STK", currency="GBP",
            primaryExchange="LSE",
        ),
        longName="IQE PLC",
    )


def make_adapter(fake_ib, store, *, live_positions=None, yahoo_quote_fetcher=None):
    from app.adapters.ibkr import IbkrAdapter

    kwargs = {}
    if yahoo_quote_fetcher is not None:
        kwargs["yahoo_quote_fetcher"] = yahoo_quote_fetcher

    return IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: fake_ib,
        store=store,
        live_positions=live_positions,
        **kwargs,
    )


# Tests ------------------------------------------------------------------------


async def test_connect_seeds_live_positions_with_initial_snapshot(store):
    live = LivePositions()
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U7575980", contract=_tencent_contract(), position=100.0, avgCost=400.0)],
        contract_details={76792991: _tencent_details()},
        last_prices={76792991: 420.0},
    )
    adapter = make_adapter(fake_ib, store, live_positions=live)

    await adapter.connect()

    snapshot = live.get_all()
    assert len(snapshot) == 1
    p = snapshot[0]
    assert p.canonical_symbol == "700.HK"
    assert p.name_en == "TENCENT HOLDINGS LTD"
    assert p.last_price == 420.0


async def test_connect_subscribes_reqmktdata_for_each_stk_contract(store):
    live = LivePositions()
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U7575980", contract=_tencent_contract(), position=100.0, avgCost=400.0)],
        contract_details={76792991: _tencent_details()},
        last_prices={76792991: 420.0},
    )
    adapter = make_adapter(fake_ib, store, live_positions=live)

    await adapter.connect()

    assert 76792991 in fake_ib.req_mkt_data_calls


async def test_connect_skips_streaming_subscription_for_gated_exchange(store):
    live = LivePositions()
    fake_ib = FakeIB(
        positions=[
            FakeIBPosition(
                account="U7575980",
                contract=_tsej_contract(),
                position=100.0,
                avgCost=1000.0,
            ),
        ],
        contract_details={14016494: _tsej_details()},
        last_prices={},
    )
    adapter = make_adapter(fake_ib, store, live_positions=live)

    await adapter.connect()

    assert 14016494 not in fake_ib.req_mkt_data_calls
    assert live.get_all()[0].canonical_symbol == "6315.JP"


async def test_portfolio_update_refreshes_gated_exchange_live_position(store):
    live = LivePositions()
    initial_item = FakePortfolioItem(
        account="U7575980",
        contract=_tsej_details().contract,
        position=100.0,
        marketPrice=1000.0,
        marketValue=100_000.0,
        averageCost=900.0,
        unrealizedPNL=10_000.0,
    )
    fake_ib = FakeIB(
        positions=[
            FakeIBPosition(
                account="U7575980",
                contract=_tsej_contract(),
                position=100.0,
                avgCost=900.0,
            ),
        ],
        contract_details={14016494: _tsej_details()},
        last_prices={},
        portfolio_items=[initial_item],
    )
    adapter = make_adapter(fake_ib, store, live_positions=live)
    await adapter.connect()
    await live.wait_for_change()

    updated_item = FakePortfolioItem(
        account="U7575980",
        contract=_tsej_details().contract,
        position=100.0,
        marketPrice=1100.0,
        marketValue=110_000.0,
        averageCost=900.0,
        unrealizedPNL=20_000.0,
    )
    fake_ib.updatePortfolioEvent.emit(updated_item)

    await asyncio.wait_for(live.wait_for_change(), timeout=1.0)
    p = live.get_all()[0]
    assert p.last_price == pytest.approx(1100.0)
    assert p.market_value_native == pytest.approx(110_000.0)
    assert p.unrealized_pnl_native == pytest.approx(20_000.0)
    assert p.last_price_is_broker_mark is True
    assert 14016494 not in fake_ib.req_mkt_data_calls


async def test_live_tick_clears_broker_mark_and_delayed_badges(store):
    from app.adapters.ibkr import IbkrAdapter
    from app.core.broker import Position

    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: None,
        store=store,
        live_positions=live,
    )
    seeded = Position(
        broker="IBKR", account_id="U1", native_key="76792991",
        canonical_symbol="700.HK", native_symbol="700",
        exchange="SEHK", currency="HKD", name_en="TENCENT",
        asset_class="STK", quantity=100.0, avg_cost=400.0,
        last_price=420.0, market_value_native=42_000.0,
        market_value_usd=42_000.0, unrealized_pnl_native=2_000.0,
        unrealized_pnl_usd=2_000.0, last_price_is_broker_mark=True,
        last_price_is_delayed=True,
    )
    ticker = FakeTicker(_tencent_contract(), last=425.0, marketDataType=1)
    adapter._streaming[76792991] = (seeded, _tencent_contract(), ticker)
    live.set_position(seeded)

    adapter._on_ticker_update(ticker)

    p = live.get_all()[0]
    assert p.last_price == pytest.approx(425.0)
    assert p.last_price_is_broker_mark is False
    assert p.last_price_is_delayed is False


async def test_delayed_tick_sets_delayed_badge(store):
    from app.adapters.ibkr import IbkrAdapter
    from app.core.broker import Position

    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: None,
        store=store,
        live_positions=live,
    )
    seeded = Position(
        broker="IBKR", account_id="U1", native_key="76792991",
        canonical_symbol="700.HK", native_symbol="700",
        exchange="SEHK", currency="HKD", name_en="TENCENT",
        asset_class="STK", quantity=100.0, avg_cost=400.0,
        last_price=420.0, market_value_native=42_000.0,
        market_value_usd=42_000.0, unrealized_pnl_native=2_000.0,
        unrealized_pnl_usd=2_000.0,
    )
    ticker = FakeTicker(_tencent_contract(), last=425.0, marketDataType=3)
    adapter._streaming[76792991] = (seeded, _tencent_contract(), ticker)
    live.set_position(seeded)

    adapter._on_ticker_update(ticker)

    p = live.get_all()[0]
    assert p.last_price == pytest.approx(425.0)
    assert p.last_price_is_delayed is True
    assert p.last_price_is_broker_mark is False


async def test_connect_skips_streaming_subscription_for_historical_only_gated_exchange(store):
    live = LivePositions()
    fake_ib = FakeIB(
        positions=[
            FakeIBPosition(
                account="U7575980",
                contract=_lse_contract(),
                position=500.0,
                avgCost=50.0,
            ),
        ],
        contract_details={14075064: _lse_details()},
        last_prices={14075064: 49.60},
    )
    async def fake_yahoo_price(_symbol: str) -> float | None:
        return 49.60

    adapter = make_adapter(
        fake_ib,
        store,
        live_positions=live,
        yahoo_quote_fetcher=fake_yahoo_price,
    )

    await adapter.connect()

    assert 14075064 not in fake_ib.req_mkt_data_calls
    assert live.get_all()[0].last_price == pytest.approx(49.60)


async def test_ticker_update_event_propagates_new_price_into_live_positions(store):
    live = LivePositions()
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U7575980", contract=_tencent_contract(), position=100.0, avgCost=400.0)],
        contract_details={76792991: _tencent_details()},
        last_prices={76792991: 420.0},
    )
    adapter = make_adapter(fake_ib, store, live_positions=live)
    await adapter.connect()

    # Consume the initial change event
    await live.wait_for_change()

    # Simulate a price tick
    ticker = fake_ib.tickers_by_conid[76792991]
    ticker.fire_tick(425.0)

    await asyncio.wait_for(live.wait_for_change(), timeout=1.0)

    p = live.get_all()[0]
    assert p.last_price == 425.0
    # market_value_native should also recompute: 100 * 425 = 42_500
    assert p.market_value_native == pytest.approx(42_500.0)
    # unrealized_pnl_native: (425 - 400) * 100 = 2_500
    assert p.unrealized_pnl_native == pytest.approx(2_500.0)


async def test_connect_does_not_subscribe_mkt_data_when_live_positions_is_none(store):
    """Backward-compat: slice 2 tests that don't pass live_positions still
    work without IbkrAdapter doing any streaming subscription."""
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U7575980", contract=_tencent_contract(), position=100.0, avgCost=400.0)],
        contract_details={76792991: _tencent_details()},
        last_prices={76792991: 420.0},
    )
    adapter = make_adapter(fake_ib, store)  # no live_positions

    await adapter.connect()

    assert fake_ib.req_mkt_data_calls == []


async def test_disconnect_cancels_mkt_data_subscriptions(store):
    """Avoid leaking ticker subscriptions when the adapter shuts down."""
    live = LivePositions()
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U7575980", contract=_tencent_contract(), position=100.0, avgCost=400.0)],
        contract_details={76792991: _tencent_details()},
        last_prices={76792991: 420.0},
    )
    adapter = make_adapter(fake_ib, store, live_positions=live)
    await adapter.connect()

    await adapter.disconnect()

    assert 76792991 in fake_ib.cancel_mkt_data_calls
