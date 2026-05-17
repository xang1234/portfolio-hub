"""Slice 9 cycle 7: header badge displays the current backoff delay.

When the adapter is RECONNECTING and currently sleeping the 5s window, the
badge must read '🟡 IBKR reconnecting (5s)'. When it moves to the 15s window,
the badge updates on the next 5s poll to '🟡 IBKR reconnecting (15s)'. The
poll frequency comes from the existing /healthz HTMX wiring.

Two layers tested:
- Adapter API: `current_backoff_delay()` returns the active sleep delay
  while RECONNECTING, else None.
- Template: `partials/status_badge.html` includes the delay when present.
"""

import asyncio

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState


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
        self.next_failures = 0

    async def connectAsync(self, host, port, clientId):
        self.connect_attempts += 1
        if self.next_failures > 0:
            self.next_failures -= 1
            raise ConnectionRefusedError("simulated")
        self._connected = True

    def disconnect(self): self._connected = False
    def isConnected(self): return self._connected
    def reqMarketDataType(self, mdt): pass

    def simulate_disconnect(self):
        self._connected = False
        self.disconnectedEvent.emit()


def _make_adapter(fake_ib, *, reconnect_delays):
    from app.adapters.ibkr import IbkrAdapter
    return IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, reconnect_delays=reconnect_delays,
    )


# Adapter API ---------------------------------------------------------------


async def test_current_backoff_delay_is_none_when_connected():
    fake_ib = FakeIB()
    adapter = _make_adapter(fake_ib, reconnect_delays=[5.0, 15.0, 60.0])
    await adapter.connect()
    assert adapter.current_backoff_delay() is None


async def test_current_backoff_delay_reports_first_window():
    """Right after disconnect fires, the loop is sleeping its first delay."""
    fake_ib = FakeIB()
    adapter = _make_adapter(fake_ib, reconnect_delays=[5.0, 15.0, 60.0])
    await adapter.connect()

    fake_ib.simulate_disconnect()
    # Give the loop a chance to enter the first sleep but not finish it.
    await asyncio.sleep(0.01)
    assert await adapter.get_connection_state() == ConnectionState.RECONNECTING
    assert adapter.current_backoff_delay() == 5.0

    await adapter.disconnect()


async def test_current_backoff_delay_progresses_on_failed_attempts():
    """After the first attempt fails, the loop sleeps the second delay."""
    fake_ib = FakeIB()
    # Short delays so the test runs in ms but still uses the real loop progression.
    adapter = _make_adapter(fake_ib, reconnect_delays=[0.05, 0.20, 0.05])
    await adapter.connect()

    fake_ib.next_failures = 2  # both retries fail; third succeeds
    fake_ib.simulate_disconnect()

    # First sleep window: 0.05s
    await asyncio.sleep(0.01)
    assert adapter.current_backoff_delay() == 0.05

    # After 0.07s total, attempt 1 has fired and failed; loop is now sleeping
    # the second delay (0.20s).
    await asyncio.sleep(0.07)
    assert adapter.current_backoff_delay() == 0.20, (
        f"expected loop to have progressed to delay[1]=0.20, got "
        f"{adapter.current_backoff_delay()}"
    )

    await adapter.disconnect()


# Template wiring -----------------------------------------------------------


class _StubAdapter:
    name = "IBKR"

    def __init__(self, state, delay=None):
        self._state = state
        self._delay = delay

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return self._state == ConnectionState.CONNECTED
    async def get_connection_state(self): return self._state
    async def get_positions(self): return []
    async def get_account_summary(self): return []

    def current_backoff_delay(self): return self._delay


def _client(adapter):
    from app.main import create_app
    return TestClient(create_app(broker=adapter))


def test_badge_includes_delay_when_reconnecting():
    """When the adapter reports a 5s delay, the /healthz HTMX response
    (rendered as the status_badge partial) shows '(5s)'."""
    adapter = _StubAdapter(ConnectionState.RECONNECTING, delay=5.0)
    response = _client(adapter).get("/healthz", headers={"HX-Request": "true"})
    assert response.status_code == 200
    text = response.text
    assert "reconnecting" in text.lower()
    assert "(5s)" in text


def test_badge_includes_15s_delay_after_first_retry():
    adapter = _StubAdapter(ConnectionState.RECONNECTING, delay=15.0)
    response = _client(adapter).get("/healthz", headers={"HX-Request": "true"})
    assert "(15s)" in response.text


def test_badge_60s_delay_at_cap():
    adapter = _StubAdapter(ConnectionState.RECONNECTING, delay=60.0)
    response = _client(adapter).get("/healthz", headers={"HX-Request": "true"})
    assert "(60s)" in response.text


def test_badge_connected_has_no_countdown():
    adapter = _StubAdapter(ConnectionState.CONNECTED)
    response = _client(adapter).get("/healthz", headers={"HX-Request": "true"})
    assert "(5s)" not in response.text
    assert "(15s)" not in response.text
