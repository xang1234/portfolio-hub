"""When the gateway is slow to boot, the dashboard's first connect()
attempt may time out before IB Gateway has finished 2FA and opened its
API socket. Slice 9's disconnectedEvent-driven reconnect doesn't help
there — disconnectedEvent only fires AFTER a successful connection.

So we need a separate startup-path entry into the reconnect loop:
boot, try to connect, and if that fails, transition to RECONNECTING
and let the backoff loop keep trying.
"""

import asyncio

from app.core.broker import ConnectionState


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


class FakeIB:
    def __init__(self, *, connect_fail_for_first_n: int = 0):
        self._connected = False
        self._fail_remaining = connect_fail_for_first_n
        self.connect_attempts = 0
        self.disconnectedEvent = _Event()

    async def connectAsync(self, host, port, clientId):
        self.connect_attempts += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise ConnectionRefusedError("simulated")
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isConnected(self):
        return self._connected

    def reqMarketDataType(self, mdt):
        pass


def make_adapter(fake_ib, *, reconnect_delays=None):
    from app.adapters.ibkr import IbkrAdapter

    return IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: fake_ib,
        reconnect_delays=reconnect_delays or [0.01, 0.01, 0.01],
    )


async def test_start_with_initial_connect_failure_enters_reconnecting():
    """When boot-time connect() fails, the adapter should not be stuck
    at DISCONNECTED — it should transition to RECONNECTING so the loop
    keeps trying. (Otherwise the user has to manually restart whenever
    the gateway is slow to boot.)"""
    fake_ib = FakeIB(connect_fail_for_first_n=2)
    adapter = make_adapter(fake_ib, reconnect_delays=[0.01, 0.01, 0.01])

    await adapter.start()

    # Right after start() returns, we should be in RECONNECTING (not stuck
    # at DISCONNECTED). The connect() call inside start() raised, but
    # start() swallowed it and kicked off the loop.
    assert await adapter.get_connection_state() == ConnectionState.RECONNECTING


async def test_start_eventually_connects_when_gateway_becomes_available():
    """Same scenario as above, but verify the loop actually succeeds once
    the gateway stops refusing connections."""
    fake_ib = FakeIB(connect_fail_for_first_n=2)
    adapter = make_adapter(fake_ib, reconnect_delays=[0.01, 0.01, 0.01])

    await adapter.start()
    await asyncio.sleep(0.1)

    assert await adapter.get_connection_state() == ConnectionState.CONNECTED
    assert fake_ib.connect_attempts >= 3  # 1 failed initial + 2 reconnect attempts


async def test_start_never_raises_even_on_persistent_failure():
    """start() is the boot-path entry point — it must never raise, otherwise
    the FastAPI lifespan crashes and the dashboard won't serve /healthz at all.
    Persistent failure should leave us at DISCONNECTED, not propagate the
    exception."""
    fake_ib = FakeIB(connect_fail_for_first_n=99)
    adapter = make_adapter(fake_ib, reconnect_delays=[0.01])

    # Should not raise
    await adapter.start()
    await asyncio.sleep(0.1)

    # After exhausting the (1-element) backoff, we end up DISCONNECTED
    assert await adapter.get_connection_state() == ConnectionState.DISCONNECTED


async def test_start_succeeds_normally_when_gateway_is_already_ready():
    """If the gateway IS ready at boot, start() should behave like a normal
    connect() and leave us in CONNECTED — no reconnect loop spawned, no
    extra latency."""
    fake_ib = FakeIB()  # no failures
    adapter = make_adapter(fake_ib)

    await adapter.start()

    assert await adapter.get_connection_state() == ConnectionState.CONNECTED
    assert fake_ib.connect_attempts == 1
