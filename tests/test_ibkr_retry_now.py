"""Operator-triggered immediate IBKR reconnect retry."""

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
    def __init__(self, *, emit_disconnect_event_on_disconnect: bool = False):
        self._connected = False
        self.connect_attempts = 0
        self.disconnectedEvent = _Event()
        self._emit_disconnect_event_on_disconnect = emit_disconnect_event_on_disconnect

    async def connectAsync(self, host, port, clientId):
        self.connect_attempts += 1
        self._connected = True

    def disconnect(self):
        self._connected = False
        if self._emit_disconnect_event_on_disconnect:
            self.disconnectedEvent.emit()

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
        reconnect_delays=reconnect_delays or [60.0, 60.0, 60.0],
    )


async def wait_until(predicate, *, timeout: float = 0.25, interval: float = 0.001):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if predicate():
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(interval)


async def test_retry_now_wakes_reconnecting_loop_without_full_backoff():
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib, reconnect_delays=[60.0])
    await adapter.connect()
    attempts_after_initial_connect = fake_ib.connect_attempts

    fake_ib.simulate_disconnect()
    await asyncio.sleep(0)
    assert await adapter.get_connection_state() == ConnectionState.RECONNECTING
    assert adapter.current_backoff_delay() == 60.0

    await adapter.retry_now()
    await wait_until(
        lambda: fake_ib.connect_attempts == attempts_after_initial_connect + 1
    )

    assert await adapter.get_connection_state() == ConnectionState.CONNECTED
    assert fake_ib.connect_attempts == attempts_after_initial_connect + 1


async def test_retry_now_on_disconnected_starts_reconnect_flow():
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib, reconnect_delays=[60.0])

    assert await adapter.get_connection_state() == ConnectionState.DISCONNECTED
    await adapter.retry_now()
    await wait_until(lambda: fake_ib.connect_attempts == 1)

    assert await adapter.get_connection_state() == ConnectionState.CONNECTED
    assert fake_ib.connect_attempts == 1


async def test_retry_now_on_connected_is_noop():
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib, reconnect_delays=[0.01])
    await adapter.connect()
    attempts_after_initial_connect = fake_ib.connect_attempts

    await adapter.retry_now()
    await asyncio.sleep(0.01)

    assert await adapter.get_connection_state() == ConnectionState.CONNECTED
    assert fake_ib.connect_attempts == attempts_after_initial_connect


async def test_retry_now_during_disconnect_does_not_spawn_reconnect_loop():
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib, reconnect_delays=[0.02])
    await adapter.connect()
    attempts_after_initial_connect = fake_ib.connect_attempts
    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()

    async def slow_reconnect_wait(delay):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            cancel_started.set()
            await release_cancel.wait()
            raise

    adapter._wait_for_reconnect_delay = slow_reconnect_wait

    fake_ib.simulate_disconnect()
    await asyncio.sleep(0)
    assert await adapter.get_connection_state() == ConnectionState.RECONNECTING

    disconnect_task = asyncio.create_task(adapter.disconnect())
    await wait_until(cancel_started.is_set)

    await adapter.retry_now()
    release_cancel.set()
    await disconnect_task

    await asyncio.sleep(0.05)
    assert fake_ib.connect_attempts == attempts_after_initial_connect
    assert await adapter.get_connection_state() == ConnectionState.DISCONNECTED


async def test_disconnect_ignores_disconnect_event_emitted_by_ib_disconnect():
    fake_ib = FakeIB(emit_disconnect_event_on_disconnect=True)
    adapter = make_adapter(fake_ib, reconnect_delays=[0.02])
    await adapter.connect()
    attempts_after_initial_connect = fake_ib.connect_attempts

    await adapter.disconnect()

    await asyncio.sleep(0.05)
    assert fake_ib.connect_attempts == attempts_after_initial_connect
    assert await adapter.get_connection_state() == ConnectionState.DISCONNECTED


async def test_reconnect_loop_cleans_up_when_state_becomes_connected_while_waiting():
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib, reconnect_delays=[0.01])
    await adapter.connect()

    fake_ib.simulate_disconnect()
    await wait_until(lambda: adapter.current_backoff_delay() == 0.01)
    task = adapter._reconnect_task
    assert task is not None

    adapter._connection_state = ConnectionState.CONNECTED
    await wait_until(task.done)

    assert adapter._reconnect_wakeup is None
    assert adapter.current_backoff_delay() is None
