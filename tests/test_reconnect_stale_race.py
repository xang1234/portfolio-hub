"""Slice 9 review fix: tick callback during disconnect must NOT clear the
stale flag.

The race: ib_async queues tick callbacks in its event loop. When the TCP
session drops, disconnectedEvent fires but a previously-queued tick may
still be pending. If we set state=RECONNECTING and mark positions stale
BEFORE deregistering tick handlers, that pending tick can race through
_on_ticker_update and replace the stale Position with a fresh one
(last_price_is_stale=False), masking the disconnect from the user.

Fix: _handle_disconnect deregisters tick handlers first, then marks stale.
This test simulates a tick fired immediately after disconnect and asserts
the stale flag survives.
"""

import asyncio
from dataclasses import dataclass

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
        # Save the ticker we hand out so the test can simulate a post-
        # disconnect tick coming through the same channel ib_async uses.
        self.live_ticker: FakeTicker | None = None

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
        self.live_ticker = t
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
    return pos, details


async def test_pending_tick_after_disconnect_cannot_clear_stale_flag(store):
    """The reconnect-race scenario.

    1. Adapter connects, subscribes to the position, ticker is registered.
    2. Gateway disconnects → _handle_disconnect marks position stale.
    3. A ticker callback fires AFTER the disconnect (pending in the event loop).
    4. The stale flag must survive. If the handler is still bound, the tick
       will produce a fresh non-stale Position and overwrite our stale one.
    """
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
        reconnect_delays=[10.0],  # long delay; we observe the stale window
    )
    await adapter.connect()
    ticker = fake_ib.live_ticker
    assert ticker is not None, "the adapter should have subscribed to a ticker"

    # Disconnect — handler should deregister tick handlers and mark stale.
    fake_ib.simulate_disconnect()

    # Now simulate a pending tick callback firing AFTER the disconnect.
    # The price changes (430 != 420) so the existing "no-op if same" guard
    # in _on_ticker_update cannot save us. The handler should NOT be
    # registered anymore, so the tick is dropped.
    ticker.last = 430.0
    ticker.updateEvent.emit(ticker)

    rows = live.get_all()
    assert len(rows) == 1
    assert rows[0].last_price_is_stale, (
        "stale flag should survive a tick that races the disconnect handler"
    )
    # And the post-disconnect tick must NOT update last_price either.
    assert rows[0].last_price == 420.0, (
        f"last_price was overwritten by a post-disconnect tick: {rows[0].last_price}"
    )

    await adapter.disconnect()
