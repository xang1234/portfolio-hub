"""Edge cases for the reconnect lifecycle.

These are the gaps the code reviewer flagged:
  1. disconnect() during a reconnect window should cancel the reconnect task
     (otherwise the loop later races and re-establishes a connection the
     caller intended to terminate).
  2. After the backoff schedule is exhausted with persistent failure, the
     adapter should settle in DISCONNECTED — not stay forever in RECONNECTING.
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

    def simulate_disconnect(self):
        self._connected = False
        self.disconnectedEvent.emit()


def make_adapter(fake_ib, *, reconnect_delays=None):
    from app.adapters.ibkr import IbkrAdapter

    return IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: fake_ib,
        reconnect_delays=reconnect_delays or [0.05, 0.05, 0.05],
    )


# Explicit disconnect during reconnect ----------------------------------------


async def test_explicit_disconnect_cancels_in_flight_reconnect_loop():
    """If the user (or FastAPI lifespan shutdown) calls disconnect() while
    the reconnect loop is sleeping, the loop must stop. Otherwise it will
    later fire connect() and reopen the connection the caller asked to close."""
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib, reconnect_delays=[0.2, 0.2, 0.2])
    await adapter.connect()
    attempts_after_initial_connect = fake_ib.connect_attempts

    fake_ib.simulate_disconnect()
    # We're now in RECONNECTING, sleeping on the first 0.2s backoff.
    assert await adapter.get_connection_state() == ConnectionState.RECONNECTING

    # Caller decides to give up — explicit disconnect.
    await adapter.disconnect()
    assert await adapter.get_connection_state() == ConnectionState.DISCONNECTED

    # Wait past the full backoff window. No further connect attempts should fire.
    await asyncio.sleep(0.7)
    assert fake_ib.connect_attempts == attempts_after_initial_connect, (
        f"reconnect loop kept firing connectAsync after disconnect(): "
        f"{fake_ib.connect_attempts - attempts_after_initial_connect} extra attempts"
    )
    assert await adapter.get_connection_state() == ConnectionState.DISCONNECTED


# Backoff exhaustion ----------------------------------------------------------


async def test_state_settles_in_disconnected_after_backoff_is_exhausted():
    """If every reconnect attempt fails, the loop terminates and the adapter
    ends up in DISCONNECTED rather than getting stuck in RECONNECTING forever.
    Operators need this to tell 'still trying' apart from 'gave up'."""
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib, reconnect_delays=[0.01, 0.01, 0.01])
    await adapter.connect()

    fake_ib._fail_remaining = 99  # every reconnect attempt will fail
    fake_ib.simulate_disconnect()

    # Wait for all 3 attempts at 0.01s each plus margin
    await asyncio.sleep(0.2)

    assert await adapter.get_connection_state() == ConnectionState.DISCONNECTED
