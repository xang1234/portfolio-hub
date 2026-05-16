"""Slice 3 cycle 2: fx_cache table + Store.get_fx_rate / put_fx_rate.

The table persists the latest usable rate per pair across restarts, so
the dashboard can show last-known USD values *before* the first fresh
IB tick arrives after boot. Without this, every restart would briefly
show — in USD columns until the FX subscriptions warm up.

Source metadata is persisted too, so the 📡 fallback badge correctly
survives a restart that happened while we were on the public API.
"""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


# Round-trip: put then get ----------------------------------------------------


async def test_put_fx_rate_then_get_returns_same_rate(store):
    quoted_at = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)

    await store.put_fx_rate(
        pair="HKDUSD",
        rate=0.1283,
        source="IB",
        quoted_at=quoted_at,
    )
    row = await store.get_fx_rate("HKDUSD")

    assert row is not None
    assert row["pair"] == "HKDUSD"
    assert row["rate"] == pytest.approx(0.1283)
    assert row["source"] == "IB"
    assert row["quoted_at"] == quoted_at


async def test_get_fx_rate_returns_none_for_unknown_pair(store):
    row = await store.get_fx_rate("JPYUSD")
    assert row is None


# Upsert semantics ------------------------------------------------------------


async def test_put_fx_rate_upserts_on_pair_conflict(store):
    """The same pair can only have one row. A second put overwrites."""
    t1 = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 15, 12, 5, tzinfo=timezone.utc)

    await store.put_fx_rate(pair="HKDUSD", rate=0.1283, source="IB", quoted_at=t1)
    await store.put_fx_rate(pair="HKDUSD", rate=0.1290, source="IB", quoted_at=t2)

    row = await store.get_fx_rate("HKDUSD")
    assert row["rate"] == pytest.approx(0.1290)
    assert row["quoted_at"] == t2


async def test_put_fx_rate_preserves_source_metadata_across_restart(store):
    """If we were on API_FALLBACK when shut down, the next boot reads back
    API_FALLBACK and the 📡 badge appears immediately."""
    await store.put_fx_rate(
        pair="JPYUSD",
        rate=0.0064,
        source="API_FALLBACK",
        quoted_at=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
    )

    row = await store.get_fx_rate("JPYUSD")
    assert row["source"] == "API_FALLBACK"


# Source values --------------------------------------------------------------


async def test_put_fx_rate_accepts_both_known_sources(store):
    """Sanity: both source literals round-trip correctly."""
    t = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)

    await store.put_fx_rate(pair="HKDUSD", rate=0.128, source="IB", quoted_at=t)
    await store.put_fx_rate(pair="EURUSD", rate=1.08, source="API_FALLBACK", quoted_at=t)

    assert (await store.get_fx_rate("HKDUSD"))["source"] == "IB"
    assert (await store.get_fx_rate("EURUSD"))["source"] == "API_FALLBACK"


# updated_at advances on each put --------------------------------------------


async def test_put_fx_rate_advances_updated_at_on_upsert(store):
    """quoted_at is when the source took the quote; updated_at is when we
    wrote to disk. Both move on an upsert."""
    t1 = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(minutes=5)

    await store.put_fx_rate(pair="HKDUSD", rate=0.128, source="IB", quoted_at=t1)
    row_after_first_put = await store.get_fx_rate("HKDUSD")

    await store.put_fx_rate(pair="HKDUSD", rate=0.129, source="IB", quoted_at=t2)
    row_after_second_put = await store.get_fx_rate("HKDUSD")

    assert row_after_second_put["updated_at"] > row_after_first_put["updated_at"]
