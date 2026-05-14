"""Tests for the three-state ConnectionState on the Broker Protocol.

States:
  - CONNECTED: gateway is responsive, market data flowing
  - RECONNECTING: we lost the gateway but are auto-retrying with backoff
  - DISCONNECTED: not connected (initial state, or after backoff exhausted)

The state is exposed via Broker.get_connection_state() — an async method
so adapters that need to check live IB status can do so without blocking.
"""

import pytest


class FakeIB:
    """Test double for ib_async.IB with enough surface for slice 9."""

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

    def __init__(self):
        self._connected = False
        self.disconnectedEvent = FakeIB._Event()

    async def connectAsync(self, host, port, clientId):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isConnected(self):
        return self._connected

    def reqMarketDataType(self, mdt):
        pass


def make_adapter(*, fake_ib=None, live_positions=None, store=None):
    from app.adapters.ibkr import IbkrAdapter

    ib = fake_ib or FakeIB()
    return IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: ib,
        store=store,
        live_positions=live_positions,
    )


# ConnectionState enum value -------------------------------------------------


def test_connection_state_has_three_values():
    from app.core.broker import ConnectionState

    assert ConnectionState.CONNECTED.value == "CONNECTED"
    assert ConnectionState.RECONNECTING.value == "RECONNECTING"
    assert ConnectionState.DISCONNECTED.value == "DISCONNECTED"


# Broker Protocol surface ----------------------------------------------------


async def test_connection_state_starts_disconnected():
    from app.core.broker import ConnectionState

    adapter = make_adapter()
    assert await adapter.get_connection_state() == ConnectionState.DISCONNECTED


async def test_connection_state_after_successful_connect_is_connected():
    from app.core.broker import ConnectionState

    adapter = make_adapter()
    await adapter.connect()

    assert await adapter.get_connection_state() == ConnectionState.CONNECTED


async def test_connection_state_after_clean_disconnect_is_disconnected():
    from app.core.broker import ConnectionState

    adapter = make_adapter()
    await adapter.connect()
    await adapter.disconnect()

    assert await adapter.get_connection_state() == ConnectionState.DISCONNECTED


# is_connected() backward compat ---------------------------------------------


async def test_is_connected_still_returns_true_when_state_is_connected():
    """Slice 1/2 tests rely on is_connected() — must keep working."""
    adapter = make_adapter()
    await adapter.connect()

    assert await adapter.is_connected() is True
