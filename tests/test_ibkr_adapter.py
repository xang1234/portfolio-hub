"""Tests for IbkrAdapter — the IBKR concrete Broker implementation.

Slice 1 scope: connection lifecycle only (connect / disconnect / is_connected).
Position fetching, name resolution, FX, market hours, and reconnection arrive
in later slices.

We use a FakeIB test double (not a Mock) injected via a factory parameter. The
shape mirrors ib_async.IB's surface that the adapter touches:
    - connectAsync(host, port, clientId) -> coroutine
    - disconnect() -> None
    - isConnected() -> bool
"""

import pytest

from app.core.broker import Broker


class FakeIB:
    """Test double for ib_async.IB exposing only what IbkrAdapter uses in slice 1."""

    def __init__(self, *, connect_should_fail: bool = False) -> None:
        self._connected = False
        self._connect_should_fail = connect_should_fail
        self.last_connect_args: tuple[str, int, int] | None = None

    async def connectAsync(self, host: str, port: int, clientId: int) -> None:  # noqa: N802 — matches ib_async
        if self._connect_should_fail:
            raise ConnectionRefusedError("simulated gateway unreachable")
        self.last_connect_args = (host, port, clientId)
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def isConnected(self) -> bool:  # noqa: N802 — matches ib_async
        return self._connected


def make_adapter(ib: FakeIB | None = None):
    from app.adapters.ibkr import IbkrAdapter

    ib = ib or FakeIB()
    return IbkrAdapter(
        host="ib-gateway",
        port=4001,
        client_id=1,
        ib_factory=lambda: ib,
    )


def test_ibkr_adapter_satisfies_broker_protocol():
    adapter = make_adapter()
    assert isinstance(adapter, Broker)


def test_ibkr_adapter_has_name_ibkr():
    adapter = make_adapter()
    assert adapter.name == "IBKR"


async def test_is_connected_returns_false_before_connect():
    adapter = make_adapter()
    assert await adapter.is_connected() is False


async def test_is_connected_returns_true_after_successful_connect():
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib)

    await adapter.connect()

    assert await adapter.is_connected() is True


async def test_connect_passes_host_port_client_id_to_ib():
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib)

    await adapter.connect()

    assert fake_ib.last_connect_args == ("ib-gateway", 4001, 1)


async def test_is_connected_returns_false_after_disconnect():
    fake_ib = FakeIB()
    adapter = make_adapter(fake_ib)
    await adapter.connect()

    await adapter.disconnect()

    assert await adapter.is_connected() is False


async def test_connect_failure_leaves_adapter_disconnected():
    fake_ib = FakeIB(connect_should_fail=True)
    adapter = make_adapter(fake_ib)

    with pytest.raises(ConnectionRefusedError):
        await adapter.connect()

    assert await adapter.is_connected() is False


async def test_disconnect_is_safe_when_never_connected():
    adapter = make_adapter()

    # Should not raise
    await adapter.disconnect()
    assert await adapter.is_connected() is False
