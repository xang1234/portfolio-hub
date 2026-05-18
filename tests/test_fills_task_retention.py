"""Slice 11 review fix C1: in-flight fill-INSERT tasks must be retained.

Python's asyncio loop only holds a weak reference to running tasks.
`asyncio.create_task(coro)` without retaining the return value can be
garbage-collected mid-execution, silently dropping the work. For fills
that means: every captured execDetailsEvent COULD be lost under GC
pressure — exactly the "occasional missing fill" scenario the reconcile
backstop is designed to catch, but should never happen in the first place.

Contract:
- The adapter holds a strong reference (a set) to every in-flight
  _persist_fill task.
- When a task completes, it's removed from the set (no unbounded growth).
- adapter.disconnect() awaits in-flight writes so a graceful shutdown
  doesn't orphan them mid-INSERT.
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
    conId: int = 265598
    symbol: str = "AAPL"
    secType: str = "STK"
    currency: str = "USD"
    exchange: str = "SMART"
    primaryExchange: str = "NASDAQ"


@dataclass
class FakeExecution:
    execId: str
    acctNumber: str = "U1"
    side: str = "BOT"
    shares: float = 10.0
    price: float = 180.0
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc))


@dataclass
class FakeCommissionReport:
    execId: str
    commission: float = 1.0
    currency: str = "USD"


@dataclass
class FakeFill:
    contract: FakeContract = field(default_factory=FakeContract)
    execution: FakeExecution = field(default_factory=lambda: FakeExecution(execId="e-1"))
    commissionReport: FakeCommissionReport = field(default_factory=lambda: FakeCommissionReport(execId="e-1"))
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc))


class FakeIB:
    def __init__(self):
        self._connected = False
        self.disconnectedEvent = _Event()
        self.execDetailsEvent = _Event()

    async def connectAsync(self, host, port, clientId): self._connected = True
    def disconnect(self): self._connected = False
    def isConnected(self): return self._connected
    def reqMarketDataType(self, mdt): pass
    async def reqPositionsAsync(self): return []
    async def reqContractDetailsAsync(self, contract): return []
    async def reqTickersAsync(self, *contracts): return []
    def reqMktData(self, contract, generic="", snapshot=False, regulatory_snapshot=False): return None
    def cancelMktData(self, contract): pass

    def fire_fill(self, fill):
        self.execDetailsEvent.emit(None, fill)


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store
    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _make_fill(exec_id):
    return FakeFill(
        execution=FakeExecution(execId=exec_id),
        commissionReport=FakeCommissionReport(execId=exec_id),
    )


# Retain the task reference --------------------------------------------------


async def test_pending_writes_set_holds_in_flight_task(store):
    """At the moment a fill is fired, the adapter's pending-writes set
    must contain the spawned task. This is the property that prevents
    GC from reaping it under memory pressure."""
    from app.adapters.ibkr import IbkrAdapter

    fake_ib = FakeIB()
    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, live_positions=live,
    )
    await adapter.connect()

    fake_ib.fire_fill(_make_fill("e-1"))

    # Adapter exposes _pending_writes — assert non-empty BEFORE we yield.
    assert hasattr(adapter, "_pending_writes"), (
        "adapter must expose _pending_writes set so we can prove the task is held"
    )
    assert len(adapter._pending_writes) >= 1, (
        f"expected ≥1 in-flight task; got {len(adapter._pending_writes)}"
    )

    # Let the task run, then assert the set was cleaned up.
    await asyncio.sleep(0.05)
    assert len(adapter._pending_writes) == 0, (
        f"pending-writes set should be drained after completion; "
        f"still has {len(adapter._pending_writes)} (unbounded growth)"
    )


async def test_disconnect_awaits_in_flight_writes(store):
    """A graceful shutdown must NOT orphan in-flight fill writes; otherwise
    the last few fills before shutdown can be lost while the asyncio loop
    is torn down."""
    from app.adapters.ibkr import IbkrAdapter

    fake_ib = FakeIB()
    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, live_positions=live,
    )
    await adapter.connect()

    # Fire 3 fills back-to-back (their writes are still in-flight).
    for i in range(3):
        fake_ib.fire_fill(_make_fill(f"e-{i}"))

    # disconnect() should drain them before returning.
    await adapter.disconnect()

    rows = await store.get_fills_since(
        broker="IBKR", account_id="U1",
        since=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert len(rows) == 3, (
        f"all 3 fills should be persisted after disconnect awaits in-flight "
        f"writes; got {len(rows)}"
    )
