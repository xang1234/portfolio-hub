"""Slice 11 cycle 3: execDetailsEvent wiring lifecycle.

Contract:
- At startup (after `connect()`), the adapter registers a handler on
  `ib.execDetailsEvent`. Each fired event lands as one row in the
  `fills` table.
- The handler is also re-registered on each successful auto-reconnect
  (uses slice 9's `on_reconnected` hook) — otherwise the daily IB Gateway
  restart silently drops every execDetailsEvent until the dashboard is
  manually restarted.
- After N reconnects, the same fill produces exactly ONE row (not N+1).
- A bug here was the original motivation for the on_reconnected hook
  design in slice 9.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

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
    secType: str = "STK"
    currency: str = "USD"
    exchange: str = "SMART"
    primaryExchange: str = ""


@dataclass
class FakeExecution:
    execId: str
    acctNumber: str
    side: str  # "BOT" / "SLD"
    shares: float
    price: float
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc))


@dataclass
class FakeCommissionReport:
    execId: str
    commission: float = 1.0
    currency: str = "USD"


@dataclass
class FakeFill:
    contract: FakeContract
    execution: FakeExecution
    commissionReport: FakeCommissionReport
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc))


@dataclass
class FakeContractDetails:
    contract: FakeContract
    longName: str = ""


@dataclass
class FakeIBPosition:
    account: str
    contract: FakeContract
    position: float
    avgCost: float


class FakeTicker:
    def __init__(self, contract, last=None):
        self.contract = contract; self.last = last; self.close = None
        self.updateEvent = _Event()
    def marketPrice(self): return self.last


class FakeIB:
    def __init__(self):
        self._connected = False
        self.disconnectedEvent = _Event()
        self.execDetailsEvent = _Event()
        self.connect_attempts = 0

    async def connectAsync(self, host, port, clientId):
        self.connect_attempts += 1
        self._connected = True

    def disconnect(self): self._connected = False
    def isConnected(self): return self._connected
    def reqMarketDataType(self, mdt): pass

    async def reqPositionsAsync(self): return []
    async def reqContractDetailsAsync(self, contract): return []
    async def reqTickersAsync(self, *contracts):
        return [FakeTicker(c) for c in contracts]
    def reqMktData(self, contract, generic="", snapshot=False, regulatory_snapshot=False):
        return FakeTicker(contract)
    def cancelMktData(self, contract): pass

    def simulate_disconnect(self):
        self._connected = False
        self.disconnectedEvent.emit()

    def fire_fill(self, fill):
        """Mimic ib_async's execDetailsEvent firing (trade, fill)."""
        self.execDetailsEvent.emit(None, fill)


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store
    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _aapl_fill(exec_id="exec-1", shares=10.0, price=180.0):
    contract = FakeContract(
        conId=265598, symbol="AAPL", secType="STK", currency="USD",
        primaryExchange="NASDAQ",
    )
    execution = FakeExecution(
        execId=exec_id, acctNumber="U1", side="BOT",
        shares=shares, price=price,
    )
    return FakeFill(
        contract=contract, execution=execution,
        commissionReport=FakeCommissionReport(execId=exec_id, commission=1.0),
    )


# --- Startup wiring ----------------------------------------------------------


async def test_exec_details_event_fired_at_startup_inserts_a_fill(store):
    """The handler is wired during connect(); a fill emitted on the live IB
    instance lands in the fills table."""
    from app.adapters.ibkr import IbkrAdapter

    fake_ib = FakeIB()
    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, live_positions=live,
    )
    await adapter.connect()

    fake_ib.fire_fill(_aapl_fill(exec_id="e-startup"))
    # Handler runs synchronously inside emit(), but we may schedule a task
    # for the INSERT. Yield to the loop.
    await asyncio.sleep(0.05)

    rows = await store.get_fills_since(
        broker="IBKR", account_id="U1",
        since=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0]["execution_id"] == "e-startup"


# --- Reconnect re-wires -------------------------------------------------------


async def test_exec_details_event_resurvives_one_reconnect(store):
    """After a disconnect → reconnect, a fill emitted on the FRESH IB
    instance still lands in the table."""
    from app.adapters.ibkr import IbkrAdapter

    ibs = [FakeIB(), FakeIB()]
    iterator = iter(ibs)
    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib", port=4003, client_id=1,
        ib_factory=lambda: next(iterator), store=store, live_positions=live,
        reconnect_delays=[0.01, 0.01],
    )
    await adapter.connect()

    ibs[0].simulate_disconnect()
    await asyncio.sleep(0.1)  # let the reconnect loop run

    # Fire fill on the SECOND (post-reconnect) IB instance.
    ibs[1].fire_fill(_aapl_fill(exec_id="e-post-reconnect"))
    await asyncio.sleep(0.05)

    rows = await store.get_fills_since(
        broker="IBKR", account_id="U1",
        since=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    exec_ids = {r["execution_id"] for r in rows}
    assert "e-post-reconnect" in exec_ids, (
        f"post-reconnect fill should be persisted; got {exec_ids}"
    )


# --- Idempotency across many reconnects --------------------------------------


async def test_fills_handler_does_not_double_register_across_reconnects(store):
    """5 disconnect/reconnect cycles, then fire ONE fill. The fill must
    insert exactly once — if the handler accumulated across reconnects,
    the second INSERT OR IGNORE would silently swallow but the handler
    would have run N times and our test would still pass. To really catch
    double-registration, count handler invocations.

    Strategy: each reconnect creates a fresh FakeIB. Each FakeIB's
    execDetailsEvent has its own _callbacks list. After 5 reconnects we
    fire on the LATEST IB and assert its event has exactly 1 handler
    (the adapter's). Otherwise the slice-9 hook is re-registering itself
    on every cycle, which would cause N callbacks on the SAME event.
    """
    from app.adapters.ibkr import IbkrAdapter

    ibs = [FakeIB() for _ in range(6)]  # initial + 5 reconnects
    iterator = iter(ibs)
    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib", port=4003, client_id=1,
        ib_factory=lambda: next(iterator), store=store, live_positions=live,
        reconnect_delays=[0.01, 0.01],
    )
    await adapter.connect()
    for i in range(5):
        ibs[i].simulate_disconnect()
        await asyncio.sleep(0.05)

    latest = ibs[-1]
    # Latest IB's execDetailsEvent should have exactly one handler — the
    # adapter's fill-capture closure. If the hook is misregistered, this
    # number would be > 1.
    assert len(latest.execDetailsEvent._callbacks) == 1, (
        f"latest IB has {len(latest.execDetailsEvent._callbacks)} "
        f"execDetailsEvent handlers after 5 reconnects; expected 1"
    )

    # Fire one fill and assert one row.
    latest.fire_fill(_aapl_fill(exec_id="e-final"))
    await asyncio.sleep(0.05)
    rows = await store.get_fills_since(
        broker="IBKR", account_id="U1",
        since=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert len([r for r in rows if r["execution_id"] == "e-final"]) == 1


# --- No-store path is a graceful no-op ---------------------------------------


async def test_fills_handler_silent_when_no_store_configured(tmp_path):
    """Adapter without a Store should not crash when execDetailsEvent fires
    (fills capture is opt-in via Store wiring)."""
    from app.adapters.ibkr import IbkrAdapter

    fake_ib = FakeIB()
    adapter = IbkrAdapter(
        host="ib", port=4003, client_id=1,
        ib_factory=lambda: fake_ib,  # no store, no live_positions
    )
    await adapter.connect()
    # Should not raise.
    fake_ib.fire_fill(_aapl_fill())
    await asyncio.sleep(0.05)
