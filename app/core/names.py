"""NameResolver — cache-with-fallback for resolving English company names.

The cache is keyed on (broker, native_key) where native_key is the broker's
most stable instrument identifier (IB conId as string, etc.). The fetcher is
injected so the IBKR adapter passes a closure over reqContractDetails; other
adapters supply their own.

Cached fields also include IB's `priceMagnifier` (1 for most contracts, 100
for pence-quoted UK equities like IQE) so downstream USD math doesn't have
to re-fetch contract details on every page load.
"""

from typing import Awaitable, Callable

from app.db.store import Store


# Fetcher returns (canonical_symbol, name_en, price_magnifier) or None.
Fetcher = Callable[[str, str], Awaitable[tuple[str, str, int] | None]]


class NameResolver:
    def __init__(self, *, store: Store, fetcher: Fetcher) -> None:
        self._store = store
        self._fetcher = fetcher

    async def resolve(self, broker: str, native_key: str) -> tuple[str, str, int] | None:
        cached = await self._store.get_name_cache(broker, native_key)
        if cached is not None:
            return cached

        fetched = await self._fetcher(broker, native_key)
        if fetched is None:
            # Don't cache negative results — a transient fetch failure should
            # not permanently mark this instrument as unknown.
            return None

        canonical_symbol, name_en, price_magnifier = fetched
        await self._store.put_name_cache(
            broker, native_key, canonical_symbol, name_en, price_magnifier,
        )
        return fetched
