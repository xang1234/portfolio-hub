"""Tests for NameResolver.

The resolver is a thin layer over Store.{get,put}_name_cache that orchestrates:
  1. Try the cache by (broker, native_key)
  2. On miss, invoke a fetcher callback (in production: IB reqContractDetails)
  3. Cache the result
  4. Skip the fetcher when the cache is warm

The fetcher returns (canonical_symbol, name_en) or None. The resolver passes
None through unchanged (no caching of negatives) so a transient IB failure
doesn't permanently poison the cache.
"""

import pytest


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


async def test_first_lookup_calls_fetcher_and_caches_result(store):
    from app.core.names import NameResolver

    fetcher_calls: list[tuple[str, str]] = []

    async def fetcher(broker: str, native_key: str):
        fetcher_calls.append((broker, native_key))
        return ("700.HK", "TENCENT HOLDINGS LTD")

    resolver = NameResolver(store=store, fetcher=fetcher)

    result = await resolver.resolve(broker="IBKR", native_key="76792991")

    assert result == ("700.HK", "TENCENT HOLDINGS LTD")
    assert fetcher_calls == [("IBKR", "76792991")]


async def test_second_lookup_uses_cache_and_does_not_call_fetcher(store):
    from app.core.names import NameResolver

    fetcher_calls: list[tuple[str, str]] = []

    async def fetcher(broker: str, native_key: str):
        fetcher_calls.append((broker, native_key))
        return ("700.HK", "TENCENT HOLDINGS LTD")

    resolver = NameResolver(store=store, fetcher=fetcher)

    await resolver.resolve("IBKR", "76792991")  # populates cache
    await resolver.resolve("IBKR", "76792991")  # should hit cache

    assert len(fetcher_calls) == 1, "fetcher should be called only once across two resolve()s"


async def test_fetcher_returning_none_does_not_poison_the_cache(store):
    """If reqContractDetails fails transiently we shouldn't cache the failure."""
    from app.core.names import NameResolver

    fetcher_results: list[tuple[str, str] | None] = [None, ("700.HK", "TENCENT")]

    async def fetcher(broker: str, native_key: str):
        return fetcher_results.pop(0)

    resolver = NameResolver(store=store, fetcher=fetcher)

    first = await resolver.resolve("IBKR", "76792991")
    second = await resolver.resolve("IBKR", "76792991")

    assert first is None
    assert second == ("700.HK", "TENCENT")


async def test_different_brokers_get_independent_cache_entries(store):
    from app.core.names import NameResolver

    async def fetcher(broker: str, native_key: str):
        if broker == "IBKR":
            return ("AAPL.US", "APPLE INC")
        return ("AAPL.US", "APPLE INC (FUTU)")

    resolver = NameResolver(store=store, fetcher=fetcher)

    a = await resolver.resolve("IBKR", "1")
    b = await resolver.resolve("Futu", "1")

    assert a == ("AAPL.US", "APPLE INC")
    assert b == ("AAPL.US", "APPLE INC (FUTU)")
