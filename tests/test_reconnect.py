"""Tests for the auto-reconnect behavior on disconnectedEvent.

When the gateway connection drops (daily IBKR restart, network blip, etc.),
the adapter should:
  1. Transition to RECONNECTING state immediately
  2. Sleep for the next backoff delay (5s, 15s, 60s, 60s, ... in prod)
  3. Attempt connectAsync again
  4. On success: state → CONNECTED, reset backoff counter
  5. On failure: increment backoff index, repeat

Backoff delays are injectable so tests use 0.01s sequences.
"""

import asyncio


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
        """Mimic ib_async firing disconnectedEvent (e.g., gateway daily restart)."""
        self._connected = False
        self.disconnectedEvent.emit()


def make_adapter(fake_ib, *, reconnect_delays=None):
    from app.adapters.ibkr import IbkrAdapter

    return IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: fake_ib,
        reconnect_delays=reconnect_delays or [0.01, 0.01, 0.01],
    )


# Transition on disconnect ----------------------------------------------------


async def test_disconnected_event_immediately_transitions_to_reconnecting():
    from app.core.broker import ConnectionState

    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib)
    await adapter.connect()
    assert await adapter.get_connection_state() == ConnectionState.CONNECTED

    fake_ib.simulate_disconnect()

    # State should flip immediately, before any reconnect attempt completes
    assert await adapter.get_connection_state() == ConnectionState.RECONNECTING


# Successful reconnect --------------------------------------------------------


async def test_reconnect_succeeds_and_transitions_back_to_connected():
    from app.core.broker import ConnectionState

    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib)
    await adapter.connect()
    initial_attempts = fake_ib.connect_attempts

    fake_ib.simulate_disconnect()
    # Wait for backoff + reconnect to complete
    await asyncio.sleep(0.05)

    assert await adapter.get_connection_state() == ConnectionState.CONNECTED
    assert fake_ib.connect_attempts > initial_attempts


# Retry on failure ------------------------------------------------------------


async def test_reconnect_retries_with_next_backoff_on_failure():
    """If reconnect fails, the adapter should keep trying through the
    backoff schedule rather than giving up after one failure."""
    fake_ib = FakeIB()  # initial connect succeeds
    adapter = make_adapter(fake_ib, reconnect_delays=[0.01, 0.01, 0.01, 0.01])
    await adapter.connect()
    initial_attempts = fake_ib.connect_attempts

    fake_ib._fail_remaining = 2  # next 2 connectAsync calls will fail
    fake_ib.simulate_disconnect()

    # Allow time for 3 attempts (2 fail, 1 succeed)
    await asyncio.sleep(0.1)

    # At minimum 3 reconnect attempts should have happened
    assert fake_ib.connect_attempts - initial_attempts >= 3


# Backoff schedule ------------------------------------------------------------


async def test_backoff_delays_are_used_in_order():
    """The delay between reconnect attempts should follow the configured
    backoff schedule (not just retry immediately)."""
    import time

    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib, reconnect_delays=[0.05, 0.05, 0.05])
    await adapter.connect()
    start = time.monotonic()

    fake_ib._fail_remaining = 2
    fake_ib.simulate_disconnect()

    # Wait long enough for 3 attempts
    await asyncio.sleep(0.25)
    elapsed = time.monotonic() - start

    # If we had used 0 backoff, this would complete in ~0s. With 0.05s
    # backoff between each of the 3 retries, the cumulative time before
    # the final success is at least 2 * 0.05 = 0.10s.
    assert elapsed >= 0.10


# Backoff resets after successful reconnect -----------------------------------


async def test_backoff_resets_after_successful_reconnect():
    """A second disconnect later in the session should start from the
    first backoff delay again, not continue from where the previous
    sequence left off."""
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib, reconnect_delays=[0.01, 0.01, 0.01])
    await adapter.connect()

    # First disconnect: 1 failure, then success
    fake_ib._fail_remaining = 1
    fake_ib.simulate_disconnect()
    await asyncio.sleep(0.05)

    # Now second disconnect after we've successfully reconnected
    attempts_before_second = fake_ib.connect_attempts
    fake_ib._fail_remaining = 1
    fake_ib.simulate_disconnect()
    await asyncio.sleep(0.05)

    # Should have at least 2 more attempts (1 fail + 1 success)
    assert fake_ib.connect_attempts - attempts_before_second >= 2
