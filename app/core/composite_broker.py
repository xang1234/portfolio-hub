"""Composite Broker implementation for multi-broker deployments."""

import asyncio
import logging
from collections.abc import Iterable

from app.core.broker import AccountSummary, Broker, ConnectionState, Position


_LOG = logging.getLogger(__name__)


class CompositeBroker:
    """Aggregate several concrete brokers behind the existing Broker surface."""

    name = "Composite"

    def __init__(self, adapters: Iterable[Broker]) -> None:
        self._adapters: list[Broker] = list(adapters)
        if not self._adapters:
            raise ValueError("CompositeBroker requires at least one adapter")

    @property
    def adapters(self) -> tuple[Broker, ...]:
        return tuple(self._adapters)

    async def start(self) -> None:
        await asyncio.gather(*(self._start_one(adapter) for adapter in self._adapters))

    async def connect(self) -> None:
        await self.start()

    async def retry_disconnected(self) -> None:
        """Start only child adapters that are currently disconnected."""
        states = await self.get_connection_states()
        await asyncio.gather(
            *(
                self._start_one(adapter)
                for adapter in self._adapters
                if states.get(adapter.name) is ConnectionState.DISCONNECTED
            )
        )

    async def disconnect(self) -> None:
        await asyncio.gather(
            *(self._disconnect_one(adapter) for adapter in self._adapters),
            return_exceptions=True,
        )

    async def is_connected(self) -> bool:
        return (await self.get_connection_state()) is ConnectionState.CONNECTED

    async def get_connection_states(self) -> dict[str, ConnectionState]:
        pairs = await asyncio.gather(
            *(self._state_pair(adapter) for adapter in self._adapters),
        )
        return dict(pairs)

    async def get_connection_state(self) -> ConnectionState:
        states = list((await self.get_connection_states()).values())
        if not states:
            return ConnectionState.DISCONNECTED
        if all(state is ConnectionState.CONNECTED for state in states):
            return ConnectionState.CONNECTED
        if any(
            state in (ConnectionState.CONNECTED, ConnectionState.RECONNECTING)
            for state in states
        ):
            return ConnectionState.RECONNECTING
        return ConnectionState.DISCONNECTED

    async def get_positions(self) -> list[Position]:
        results = await asyncio.gather(
            *(adapter.get_positions() for adapter in self._adapters),
            return_exceptions=True,
        )
        out: list[Position] = []
        for adapter, result in zip(self._adapters, results, strict=False):
            if isinstance(result, Exception):
                _LOG.warning("%s get_positions failed: %s", adapter.name, result)
                continue
            out.extend(result)
        return out

    async def get_account_summary(self) -> list[AccountSummary]:
        results = await asyncio.gather(
            *(adapter.get_account_summary() for adapter in self._adapters),
            return_exceptions=True,
        )
        out: list[AccountSummary] = []
        for adapter, result in zip(self._adapters, results, strict=False):
            if isinstance(result, Exception):
                _LOG.warning("%s get_account_summary failed: %s", adapter.name, result)
                continue
            out.extend(result)
        return out

    async def _start_one(self, adapter: Broker) -> None:
        start = getattr(adapter, "start", None)
        if callable(start):
            await start()
        else:
            await adapter.connect()

    async def _disconnect_one(self, adapter: Broker) -> None:
        try:
            await adapter.disconnect()
        except Exception as exc:
            _LOG.warning("%s disconnect failed: %s", adapter.name, exc)

    async def _state_pair(self, adapter: Broker) -> tuple[str, ConnectionState]:
        try:
            return adapter.name, await adapter.get_connection_state()
        except Exception as exc:
            _LOG.warning("%s get_connection_state failed: %s", adapter.name, exc)
            return adapter.name, ConnectionState.DISCONNECTED
