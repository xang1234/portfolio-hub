"""Slice 9 cycle 4: reconnect lifecycle hooks.

Slice 11 (fills log) needs to re-register its `execDetailsEvent` handler
on the fresh IB instance after every reconnect — otherwise the daily-restart
gap silently drops execution events. Rather than couple slice 11 to the
adapter's internals, the adapter exposes a callback registry.

Contract:
- `adapter.on_reconnected(cb)` registers an idempotent callable. Calling
  it again with the same cb is a no-op (so a feature registering at startup
  AND on reload doesn't get fired twice).
- After a successful reconnect, every registered cb is invoked once with the
  fresh IB instance (so the cb can wire events without reaching into private
  adapter state).
- Callbacks run once per *successful* reconnect, not per attempt.
- Exceptions in one cb don't break the chain — others still fire and the
  reconnect itself is considered successful.
"""

import asyncio


class _Event:
    def __init__(self): self._callbacks = []
    def __iadd__(self, cb): self._callbacks.append(cb); return self
    def __isub__(self, cb):
        if cb in self._callbacks: self._callbacks.remove(cb)
        return self
    def emit(self, *a, **kw):
        for cb in list(self._callbacks): cb(*a, **kw)


class FakeIB:
    def __init__(self):
        self._connected = False
        self.disconnectedEvent = _Event()
        self.connect_attempts = 0

    async def connectAsync(self, host, port, clientId):
        self.connect_attempts += 1
        self._connected = True

    def disconnect(self): self._connected = False
    def isConnected(self): return self._connected
    def reqMarketDataType(self, mdt): pass

    def simulate_disconnect(self):
        self._connected = False
        self.disconnectedEvent.emit()


def _make_adapter(ib_instances=None):
    """Use a factory that hands out a different FakeIB per call so we can
    distinguish 'fresh IB' from 'original IB'."""
    from app.adapters.ibkr import IbkrAdapter

    ibs = ib_instances if ib_instances is not None else [FakeIB(), FakeIB()]
    iterator = iter(ibs)
    return IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: next(iterator),
        reconnect_delays=[0.01, 0.01, 0.01],
    ), ibs


# Registration --------------------------------------------------------------


async def test_on_reconnected_fires_callback_after_successful_reconnect():
    ibs = [FakeIB(), FakeIB()]
    adapter, _ = _make_adapter(ib_instances=ibs)

    fired_with: list[object] = []
    adapter.on_reconnected(lambda ib: fired_with.append(ib))

    await adapter.connect()
    assert fired_with == [], "callback should not fire on initial connect"

    ibs[0].simulate_disconnect()
    await asyncio.sleep(0.05)

    assert len(fired_with) == 1, (
        f"expected exactly 1 callback invocation after one reconnect, got {len(fired_with)}"
    )
    # The callback should receive the FRESH IB instance, not the dead one.
    assert fired_with[0] is ibs[1]


async def test_on_reconnected_idempotent_registration():
    """Registering the same callable twice should not double-fire it."""
    ibs = [FakeIB(), FakeIB()]
    adapter, _ = _make_adapter(ib_instances=ibs)

    counter = {"n": 0}
    def cb(_ib): counter["n"] += 1
    adapter.on_reconnected(cb)
    adapter.on_reconnected(cb)  # second registration must be a no-op

    await adapter.connect()
    ibs[0].simulate_disconnect()
    await asyncio.sleep(0.05)

    assert counter["n"] == 1, f"callback fired {counter['n']} times; expected 1"


async def test_on_reconnected_does_not_fire_on_failed_attempts():
    """The hook is post-success only — it must not fire on each backoff retry,
    only after the connection is actually back."""
    # The factory hands out the SAME shared IB each call so each retry attempt
    # rebinds the adapter to the same fake. The shared IB fails the first 2
    # post-disconnect attempts and succeeds on the third.
    class ReconfigurableIB(FakeIB):
        def __init__(self): super().__init__(); self.next_failures = 0
        async def connectAsync(self, host, port, clientId):
            self.connect_attempts += 1
            if self.next_failures > 0:
                self.next_failures -= 1
                raise ConnectionRefusedError("simulated")
            self._connected = True

    shared = ReconfigurableIB()
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: shared,
        reconnect_delays=[0.01, 0.01, 0.01, 0.01],
    )

    fired: list[object] = []
    adapter.on_reconnected(lambda ib: fired.append(ib))

    await adapter.connect()
    shared.next_failures = 2  # next 2 reconnect attempts fail; third succeeds
    shared.simulate_disconnect()
    await asyncio.sleep(0.1)

    assert len(fired) == 1, (
        f"expected exactly 1 fire (after the successful retry), got {len(fired)}"
    )


async def test_on_reconnected_exception_does_not_block_other_callbacks():
    """A buggy registered callback must not prevent others from firing.
    Slice 11's fills handler MUST run even if (say) slice 12's metrics
    handler raises."""
    ibs = [FakeIB(), FakeIB()]
    adapter, _ = _make_adapter(ib_instances=ibs)

    fired_b: list[object] = []
    def cb_a(_ib): raise RuntimeError("boom")
    def cb_b(ib): fired_b.append(ib)
    adapter.on_reconnected(cb_a)
    adapter.on_reconnected(cb_b)

    await adapter.connect()
    ibs[0].simulate_disconnect()
    await asyncio.sleep(0.05)

    assert len(fired_b) == 1, (
        "cb_b should have fired even though cb_a raised"
    )
