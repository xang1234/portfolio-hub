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

    def __init__(self, contract: FakeContract, last: float | None = None) -> None:
        self.contract = contract
        self.last = last
        self.close = None
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
    ) -> None:
        self._positions = positions
        self._contract_details = contract_details
        self._last_prices = last_prices or {}
        self._connected = False
        self.req_mkt_data_calls: list[int] = []
        self.cancel_mkt_data_calls: list[int] = []
        self.tickers_by_conid: dict[int, FakeTicker] = {}

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


def make_adapter(fake_ib, store, *, live_positions=None):
    from app.adapters.ibkr import IbkrAdapter

    return IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: fake_ib,
        store=store,
        live_positions=live_positions,
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
