"""IBKR concrete Broker adapter.

Slice 1 scope: connection lifecycle only. Adapter wraps an ib_async.IB instance
created via a factory so tests can inject a FakeIB without touching the real
gateway.

Out of scope for this slice: position fetching, name resolution, FX, reconnection.
"""

from typing import Callable, Protocol


class _IBLike(Protocol):
    """Minimal subset of ib_async.IB the adapter relies on in slice 1."""

    async def connectAsync(self, host: str, port: int, clientId: int) -> None: ...

    def disconnect(self) -> None: ...

    def isConnected(self) -> bool: ...


def _default_ib_factory() -> _IBLike:
    from ib_async import IB

    return IB()


class IbkrAdapter:
    name = "IBKR"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        client_id: int,
        ib_factory: Callable[[], _IBLike] = _default_ib_factory,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib_factory = ib_factory
        self._ib: _IBLike | None = None

    async def connect(self) -> None:
        ib = self._ib_factory()
        await ib.connectAsync(self._host, self._port, clientId=self._client_id)
        self._ib = ib

    async def disconnect(self) -> None:
        if self._ib is None:
            return
        self._ib.disconnect()
        self._ib = None

    async def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()
