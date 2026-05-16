"""Tests for the SQLite-backed name_cache store.

Slice 2 surface for the store:
  - init_schema() creates all required tables (currently just name_cache)
  - get_name_cache(broker, native_key) -> tuple[canonical_symbol, name_en] | None
  - put_name_cache(broker, native_key, canonical_symbol, name_en)
  - Cache entries older than NAME_CACHE_TTL_DAYS days are treated as misses
    (PLAN.md: TTL of 30 days so corporate-action renames eventually propagate)
"""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
async def store(tmp_path):
    """Fresh on-disk SQLite store per test, isolated to a temp directory."""
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


async def test_init_schema_creates_name_cache_table(store):
    """Schema initialization is idempotent and creates name_cache."""
    # Calling init_schema a second time must not raise
    await store.init_schema()
    # And the cache should be queryable
    result = await store.get_name_cache(broker="IBKR", native_key="76792991")
    assert result is None


async def test_put_then_get_name_cache_roundtrip(store):
    await store.put_name_cache(
        broker="IBKR",
        native_key="76792991",
        canonical_symbol="700.HK",
        name_en="TENCENT HOLDINGS LTD",
    )

    result = await store.get_name_cache(broker="IBKR", native_key="76792991")

    assert result == ("700.HK", "TENCENT HOLDINGS LTD", 1)


async def test_get_name_cache_miss_returns_none(store):
    result = await store.get_name_cache(broker="IBKR", native_key="does-not-exist")
    assert result is None


async def test_put_name_cache_is_idempotent_upsert(store):
    """Re-putting the same (broker, native_key) overwrites with fresh data."""
    await store.put_name_cache("IBKR", "1", "FB.US", "FACEBOOK INC")
    await store.put_name_cache("IBKR", "1", "META.US", "META PLATFORMS INC")  # rename

    result = await store.get_name_cache("IBKR", "1")
    assert result == ("META.US", "META PLATFORMS INC", 1)


async def test_name_cache_entries_older_than_ttl_are_returned_as_miss(store, monkeypatch):
    """Entries older than NAME_CACHE_TTL_DAYS are treated as if not present."""
    from app.db import store as store_module

    # Insert a row with a stale updated_at by patching "now" backwards
    old_now = datetime.now(timezone.utc) - timedelta(days=store_module.NAME_CACHE_TTL_DAYS + 1)
    monkeypatch.setattr(store_module, "_utcnow", lambda: old_now)
    await store.put_name_cache("IBKR", "76792991", "700.HK", "TENCENT HOLDINGS LTD")

    # Restore real time and try to read
    monkeypatch.undo()
    result = await store.get_name_cache("IBKR", "76792991")
    assert result is None


async def test_name_cache_keyed_by_broker_and_native_key(store):
    """Same native_key for different brokers must be distinct entries."""
    await store.put_name_cache("IBKR", "1", "AAPL.US", "APPLE INC")
    await store.put_name_cache("Futu", "1", "DIFFERENT.US", "DIFFERENT CO")

    assert await store.get_name_cache("IBKR", "1") == ("AAPL.US", "APPLE INC", 1)
    assert await store.get_name_cache("Futu", "1") == ("DIFFERENT.US", "DIFFERENT CO", 1)
