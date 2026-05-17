"""Slice 9 cycle 5: stale-data flagging during reconnect window.

While the adapter is RECONNECTING, the SSE pipeline keeps pushing the
last-known position snapshot — but each row's price hasn't actually
ticked since the disconnect. The renderer needs a signal so it can
add ⚠️ next to last_price and dim the whole row.

Design: a new Position field `last_price_is_stale: bool` is set by the
adapter when disconnect fires (it walks LivePositions and re-publishes
each row with the flag flipped). When ticks resume after reconnect,
`_on_ticker_update` replaces the row and the flag clears naturally.
"""

import asyncio
from dataclasses import dataclass

import pytest

from app.core.broker import ConnectionState, Position
from app.core.live_positions import LivePositions


class _Event:
    def __init__(self): self._callbacks = []
    def __iadd__(self, cb): self._callbacks.append(cb); return self
    def __isub__(self, cb):
        if cb in self._callbacks: self._callbacks.remove(cb)
        return self
    def emit(self, *a, **kw):
        for cb in list(self._callbacks): cb(*a, **kw)


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
    def marketPrice(self): return self.last


class FakeIB:
    def __init__(self, positions, contract_details, last_prices=None):
        self._positions = positions
        self._contract_details = contract_details
        self._last_prices = last_prices or {}
        self._connected = False
        self.disconnectedEvent = _Event()
        self.connect_attempts = 0

    async def connectAsync(self, host, port, clientId):
        self.connect_attempts += 1
        self._connected = True

    def disconnect(self): self._connected = False
    def isConnected(self): return self._connected
    def reqMarketDataType(self, mdt): pass

    async def reqPositionsAsync(self): return self._positions

    async def reqContractDetailsAsync(self, contract):
        d = self._contract_details.get(contract.conId)
        return [d] if d is not None else []

    async def reqTickersAsync(self, *contracts):
        return [FakeTicker(c, last=self._last_prices.get(c.conId)) for c in contracts]

    def reqMktData(self, contract, generic="", snapshot=False, regulatory_snapshot=False):
        return FakeTicker(contract, last=self._last_prices.get(getattr(contract, "conId", -1)))

    def cancelMktData(self, contract): pass

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


def _tencent():
    contract = FakeContract(conId=76792991, symbol="700", secType="STK", currency="HKD")
    details = FakeContractDetails(
        contract=FakeContract(
            conId=76792991, symbol="700", secType="STK", currency="HKD",
            primaryExchange="SEHK",
        ),
        longName="TENCENT HOLDINGS LTD",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=100.0, avgCost=400.0)
    return pos, details


# Dataclass field --------------------------------------------------------


def test_position_has_last_price_is_stale_field_default_false():
    """The new field exists, defaults to False, and is part of Position equality."""
    p = Position(
        broker="IBKR", account_id="U1", native_key="1",
        canonical_symbol="700.HK", native_symbol="700",
        exchange="SEHK", currency="HKD", name_en="TENCENT",
        asset_class="STK", quantity=100.0, avg_cost=400.0, last_price=420.0,
        market_value_native=42000.0, market_value_usd=5388.6,
        unrealized_pnl_native=2000.0, unrealized_pnl_usd=256.2,
    )
    assert p.last_price_is_stale is False


# Disconnect marks live positions stale ----------------------------------


async def test_disconnect_marks_live_positions_as_stale(store):
    from app.adapters.ibkr import IbkrAdapter

    pos, details = _tencent()
    fake_ib = FakeIB(
        positions=[pos], contract_details={76792991: details},
        last_prices={76792991: 420.0},
    )
    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, live_positions=live,
        reconnect_delays=[10.0, 10.0],  # long delay so we observe the stale window
    )
    await adapter.connect()
    assert all(not p.last_price_is_stale for p in live.get_all())

    fake_ib.simulate_disconnect()
    # State must be RECONNECTING before we assert (the handler runs synchronously)
    assert await adapter.get_connection_state() == ConnectionState.RECONNECTING

    rows = live.get_all()
    assert rows, "live positions should still be present during reconnect"
    assert all(p.last_price_is_stale for p in rows), (
        "every live position should be marked stale while RECONNECTING"
    )

    # Clean up the in-flight reconnect task
    await adapter.disconnect()


# Tick after reconnect clears the stale flag -----------------------------


async def test_tick_after_reconnect_clears_stale_flag(store):
    """The natural-clearing semantics: when a real tick arrives, the
    replaced Position has stale=False. No explicit clearing needed."""
    from app.adapters.ibkr import IbkrAdapter

    pos, details = _tencent()
    fake_ib = FakeIB(
        positions=[pos], contract_details={76792991: details},
        last_prices={76792991: 420.0},
    )
    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, live_positions=live,
        reconnect_delays=[0.01, 0.01],
    )
    await adapter.connect()

    fake_ib.simulate_disconnect()
    await asyncio.sleep(0.1)

    # Adapter reconnected and re-seeded live_positions from get_positions().
    assert await adapter.get_connection_state() == ConnectionState.CONNECTED
    rows = live.get_all()
    assert rows
    assert all(not p.last_price_is_stale for p in rows), (
        "fresh seed after reconnect should produce stale=False rows"
    )
