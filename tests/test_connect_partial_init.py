"""Slice 9 review fix: connect() must not leave the adapter wedged in
CONNECTED state if _start_streaming raises after the initial connection.

Scenario:
- ib_async accepts the TCP handshake (gateway port is up, but the gateway
  is still completing 2FA / loading account data)
- connect() sets self._connection_state = CONNECTED and then calls
  _start_streaming, which fails (e.g. reqPositionsAsync raises because
  IB hasn't loaded accounts yet)

Pre-fix behavior:
- The exception bubbles up to the reconnect loop's `except Exception`
- The loop logs "Reconnect attempt N failed: ..." and continues
- BUT on the next iteration, the early-return `if state == CONNECTED`
  fires immediately because we never reset state — leaving the adapter
  with state=CONNECTED but no streaming subscriptions and hooks never
  fired. Slice 11's fills handler would silently miss events.

Post-fix behavior:
- _start_streaming failure resets state and the loop retries cleanly.
- Hooks eventually fire when streaming setup succeeds.
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


class FlakyStreamingIB:
    """Connect succeeds, but reqPositionsAsync raises the first N times.

    Models the 'gateway accepts TCP before it's fully booted' scenario.
    """

    def __init__(self, positions, details, *, fail_streaming_for_first_n=0):
        self._positions = positions
        self._details = details
        self._fail_remaining = fail_streaming_for_first_n
        self._connected = False
        self.disconnectedEvent = _Event()
        self.connect_attempts = 0
        self.req_positions_calls = 0

    async def connectAsync(self, host, port, clientId):
        self.connect_attempts += 1
        self._connected = True

    def disconnect(self): self._connected = False
    def isConnected(self): return self._connected
    def reqMarketDataType(self, mdt): pass

    async def reqPositionsAsync(self):
        self.req_positions_calls += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise RuntimeError("gateway still booting")
        return self._positions

    async def reqContractDetailsAsync(self, contract):
        d = self._details.get(contract.conId)
        return [d] if d is not None else []

    async def reqTickersAsync(self, *contracts):
        return [FakeTicker(c, last=420.0) for c in contracts]

    def reqMktData(self, contract, generic="", snapshot=False, regulatory_snapshot=False):
        return FakeTicker(contract, last=420.0)

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


async def test_reconnect_wedges_when_streaming_fails_after_tcp_handshake(store):
    """The latent-wedge scenario the reviewer flagged.

    1. Initial connect() succeeds normally.
    2. Gateway restarts → disconnectedEvent fires → reconnect loop spawns.
    3. Reconnect attempt: ib.connectAsync succeeds (TCP up). Adapter sets
       state=CONNECTED, then calls _start_streaming() which raises (gateway
       still loading accounts, reqPositionsAsync errors).
    4. The loop's except catches the raise and continues. Next iteration's
       early-return `if state == CONNECTED: return` triggers immediately
       because state was set to CONNECTED before _start_streaming failed.
    5. Result: state=CONNECTED, no streaming subscriptions, hooks never
       fired. Slice 11's fills handler silently misses execDetailsEvent.

    With the fix, the streaming failure resets state and the loop retries
    cleanly until streaming succeeds.
    """
    from app.adapters.ibkr import IbkrAdapter

    pos, details = _tencent()
    fake_ib = FlakyStreamingIB(
        positions=[pos], details=details,
        # 0 on initial connect, 1 fail on the first reconnect retry,
        # 0 on the second retry → second retry should fully succeed.
        fail_streaming_for_first_n=0,
    )
    live = LivePositions()

    hooks_fired: list[object] = []

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, live_positions=live,
        reconnect_delays=[0.01, 0.01, 0.01, 0.01],
    )
    adapter.on_reconnected(lambda ib: hooks_fired.append(ib))

    # Healthy initial connect.
    await adapter.connect()
    assert await adapter.get_connection_state() == ConnectionState.CONNECTED

    # Arm streaming to fail on the FIRST reconnect attempt only.
    fake_ib._fail_remaining = 1

    # Simulate a gateway restart.
    fake_ib.simulate_disconnect()
    # Reconnect loop runs: first retry's streaming fails, second succeeds.
    await asyncio.sleep(0.1)

    final_state = await adapter.get_connection_state()
    assert final_state == ConnectionState.CONNECTED, (
        f"after a streaming-failure on first retry, adapter should keep "
        f"retrying and eventually settle CONNECTED. Got {final_state}. "
        f"connect_attempts={fake_ib.connect_attempts}, "
        f"req_positions_calls={fake_ib.req_positions_calls}"
    )
    assert len(live.get_all()) == 1, (
        "live_positions should be re-seeded after streaming setup recovers"
    )
    assert len(hooks_fired) >= 1, (
        f"reconnect hooks must fire when streaming finally succeeds; "
        f"got {len(hooks_fired)} firings"
    )

    await adapter.disconnect()
