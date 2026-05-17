"""Slice 9 acceptance criterion: '3+ disconnect/reconnect cycles' must not
leak handlers, accumulate state, or grow unbounded.

This was an explicit acceptance criterion on issue #9 but had no test.
The risks are:
  - `_streaming` dict accumulates across cycles (positions multiplied).
  - Old `updateEvent` callbacks pile up on long-lived tickers (memory leak +
    eventual handler-storm when the ticker fires).
  - `_reconnect_hooks` registry double-registers across cycles.
  - `_reconnect_task` references aren't released after completion.
"""

import asyncio
from dataclasses import dataclass

import pytest

from app.core.broker import ConnectionState
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
    conId: int; symbol: str; secType: str; currency: str
    exchange: str = "SMART"; primaryExchange: str = ""


@dataclass
class FakeContractDetails:
    contract: FakeContract; longName: str


@dataclass
class FakeIBPosition:
    account: str; contract: FakeContract; position: float; avgCost: float


class FakeTicker:
    def __init__(self, contract, last=None):
        self.contract = contract; self.last = last; self.close = None
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
        # Every ticker handed out — so the test can assert no handler
        # accumulation on any one of them.
        self.tickers_issued: list[FakeTicker] = []

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
        t = FakeTicker(contract, last=self._last_prices.get(getattr(contract, "conId", -1)))
        self.tickers_issued.append(t)
        return t

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
    return pos, {76792991: details}


async def test_five_disconnect_reconnect_cycles_dont_leak_state(store):
    """Run 5 disconnect→reconnect cycles. Verify:
    - Streaming dict has 1 entry per cycle's connect (bounded).
    - Each issued ticker has at most 1 registered updateEvent handler.
    - Reconnect hook fires exactly 5 times (once per successful reconnect).
    - Adapter ends in CONNECTED, live_positions has 1 entry.
    """
    from app.adapters.ibkr import IbkrAdapter

    pos, details = _tencent()
    fake_ib = FakeIB(
        positions=[pos], contract_details=details,
        last_prices={76792991: 420.0},
    )
    live = LivePositions()
    hook_fires: list[object] = []
    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, live_positions=live,
        reconnect_delays=[0.01, 0.01],
    )
    adapter.on_reconnected(lambda ib: hook_fires.append(ib))

    await adapter.connect()
    assert await adapter.get_connection_state() == ConnectionState.CONNECTED

    for cycle in range(5):
        fake_ib.simulate_disconnect()
        await asyncio.sleep(0.05)
        state = await adapter.get_connection_state()
        assert state == ConnectionState.CONNECTED, (
            f"cycle {cycle}: expected CONNECTED, got {state}"
        )

    # Bounded streaming state: one entry (the single position), not 6.
    assert len(adapter._streaming) == 1, (
        f"streaming dict grew unbounded: {len(adapter._streaming)} entries "
        f"after 5 cycles (expected 1)"
    )

    # No handler accumulation on any ticker — old tickers should have their
    # updateEvent handlers deregistered before being abandoned. We check the
    # CURRENT tickers (last 6 issued): each should have exactly 1 handler.
    for t in fake_ib.tickers_issued[:-1]:
        assert len(t.updateEvent._callbacks) == 0, (
            f"old ticker for conId={t.contract.conId} still has "
            f"{len(t.updateEvent._callbacks)} handler(s) — leak"
        )
    # The currently-live ticker should have exactly 1 handler.
    live_ticker = fake_ib.tickers_issued[-1]
    assert len(live_ticker.updateEvent._callbacks) == 1, (
        f"current ticker has {len(live_ticker.updateEvent._callbacks)} "
        f"handlers; expected 1"
    )

    # Hooks fired once per reconnect (5 times, not 0, not 10).
    assert len(hook_fires) == 5, (
        f"on_reconnected hook fired {len(hook_fires)} times after 5 cycles; "
        f"expected exactly 5"
    )

    # Live positions still has 1 entry (the same position, re-seeded each cycle).
    assert len(live.get_all()) == 1
    assert live.get_all()[0].canonical_symbol == "700.HK"
    # And it's NOT stale at the end — last cycle's tick-equivalent seed cleared it.
    assert not live.get_all()[0].last_price_is_stale

    await adapter.disconnect()


async def test_repeated_hook_registration_is_idempotent_across_cycles(store):
    """Re-registering the same hook on every cycle (a hook might be wired
    inside on_reconnected itself) should not double-fire it."""
    from app.adapters.ibkr import IbkrAdapter

    pos, details = _tencent()
    fake_ib = FakeIB(
        positions=[pos], contract_details=details,
        last_prices={76792991: 420.0},
    )
    live = LivePositions()
    fires: list[object] = []
    cb = lambda ib: fires.append(ib)

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, live_positions=live,
        reconnect_delays=[0.01, 0.01],
    )
    # Register the same callback every cycle — simulates a slice-11 caller
    # that defensively re-registers on every reconnect.
    def re_register(_ib): adapter.on_reconnected(cb)
    adapter.on_reconnected(re_register)
    adapter.on_reconnected(cb)

    await adapter.connect()
    for _ in range(3):
        fake_ib.simulate_disconnect()
        await asyncio.sleep(0.05)

    # cb fired once per successful reconnect = 3 times, not 6 or 9.
    assert len(fires) == 3, (
        f"cb fired {len(fires)} times; expected exactly 3 (idempotent registration)"
    )
    assert len(adapter._reconnect_hooks) == 2, (
        f"hook list size = {len(adapter._reconnect_hooks)}; expected 2 "
        f"(re_register + cb)"
    )

    await adapter.disconnect()
