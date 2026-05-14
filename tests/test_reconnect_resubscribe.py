"""Tests that disconnect → reconnect re-subscribes all market data lines.

The full slice 9 acceptance: when the gateway connection drops and the
adapter auto-reconnects, all previously-subscribed reqMktData lines must
be re-established. Otherwise tickers stop flowing after the daily restart
even though the connection itself recovers.
"""

import asyncio
from dataclasses import dataclass

import pytest

from app.core.live_positions import LivePositions


class _Event:
    def __init__(self):
        self._callbacks = []
    def __iadd__(self, cb):
        self._callbacks.append(cb)
        return self
    def __isub__(self, cb):
        if cb in self._callbacks:
            self._callbacks.remove(cb)
        return self
    def emit(self, *args, **kwargs):
        for cb in list(self._callbacks):
            cb(*args, **kwargs)


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


class FakeTicker:
    def __init__(self, contract, last=None):
        self.contract = contract
        self.last = last
        self.close = None
        self.updateEvent = _Event()

    def marketPrice(self):
        return self.last


class FakeIB:
    def __init__(
        self,
        positions: list[FakeIBPosition],
        contract_details: dict[int, FakeContractDetails],
        last_prices: dict[int, float] | None = None,
    ):
        self._positions = positions
        self._contract_details = contract_details
        self._last_prices = last_prices or {}
        self._connected = False
        self.disconnectedEvent = _Event()
        self.req_mkt_data_calls: list[int] = []
        self.cancel_mkt_data_calls: list[int] = []
        self.connect_attempts = 0

    async def connectAsync(self, host, port, clientId):
        self.connect_attempts += 1
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isConnected(self):
        return self._connected

    def reqMarketDataType(self, mdt):
        pass

    async def reqPositionsAsync(self):
        return self._positions

    async def reqContractDetailsAsync(self, contract):
        details = self._contract_details.get(contract.conId)
        return [details] if details is not None else []

    async def reqTickersAsync(self, *contracts):
        return [FakeTicker(c, last=self._last_prices.get(c.conId)) for c in contracts]

    def reqMktData(self, contract, generic="", snapshot=False, regulatory_snapshot=False):
        self.req_mkt_data_calls.append(contract.conId)
        return FakeTicker(contract, last=self._last_prices.get(contract.conId))

    def cancelMktData(self, contract):
        self.cancel_mkt_data_calls.append(contract.conId)

    def simulate_disconnect(self):
        self._connected = False
        self.disconnectedEvent.emit()


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _tencent_position_and_details():
    contract = FakeContract(conId=76792991, symbol="700", secType="STK", currency="HKD")
    details = FakeContractDetails(
        contract=FakeContract(
            conId=76792991, symbol="700", secType="STK", currency="HKD", primaryExchange="SEHK"
        ),
        longName="TENCENT HOLDINGS LTD",
    )
    pos = FakeIBPosition(account="U7575980", contract=contract, position=100.0, avgCost=400.0)
    return pos, details


async def test_reconnect_resubscribes_all_market_data_lines(store):
    """After disconnect → backoff → reconnect, every position must have a
    fresh reqMktData subscription."""
    pos, details = _tencent_position_and_details()
    fake_ib = FakeIB(
        positions=[pos], contract_details={76792991: details}, last_prices={76792991: 420.0}
    )
    live = LivePositions()
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: fake_ib,
        store=store,
        live_positions=live,
        reconnect_delays=[0.01, 0.01, 0.01],
    )
    await adapter.connect()
    initial_subs = len(fake_ib.req_mkt_data_calls)
    assert initial_subs >= 1  # baseline: one sub from the initial connect

    # Simulate gateway drop
    fake_ib.simulate_disconnect()
    await asyncio.sleep(0.1)

    # After reconnect, there should be MORE reqMktData calls — one per
    # position that needed re-subscription.
    assert len(fake_ib.req_mkt_data_calls) > initial_subs, (
        f"expected re-subscription after reconnect; "
        f"reqMktData was only called {len(fake_ib.req_mkt_data_calls)} times total"
    )


async def test_reconnect_loop_eventually_settles_to_connected(store):
    """Sanity check that the full disconnect+reconnect dance reaches a stable
    CONNECTED state and live_positions has the position."""
    from app.core.broker import ConnectionState

    pos, details = _tencent_position_and_details()
    fake_ib = FakeIB(
        positions=[pos], contract_details={76792991: details}, last_prices={76792991: 420.0}
    )
    live = LivePositions()
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: fake_ib,
        store=store,
        live_positions=live,
        reconnect_delays=[0.01, 0.01, 0.01],
    )
    await adapter.connect()

    fake_ib.simulate_disconnect()
    await asyncio.sleep(0.1)

    assert await adapter.get_connection_state() == ConnectionState.CONNECTED
    # Position is still tracked in live_positions
    assert len(live.get_all()) == 1
    assert live.get_all()[0].canonical_symbol == "700.HK"
