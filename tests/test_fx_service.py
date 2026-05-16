"""Slice 3 cycle 3: FxService core.

The service is the single async-safe FX cache the rest of the app reads
from. This cycle covers the in-memory surface; later cycles wire up IB
subscriptions and the public-API fallback.

Design notes:
  - get_rate(currency) → FxRate | None. None means "no rate available;
    render — in the USD column."
  - convert(amount, currency) → float | None. None propagates the
    no-rate signal up to the row renderer.
  - USD is the base; convert(x, 'USD') returns x and get_rate('USD')
    returns a synthetic FxRate with rate=1.0.
  - start() loads fx_cache so we have last-known rates immediately.
"""

from datetime import datetime, timezone

import pytest

from app.core.fx import FxRate


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _at(hh: int, mm: int) -> datetime:
    return datetime(2026, 5, 15, hh, mm, tzinfo=timezone.utc)


# USD is the base currency ---------------------------------------------------


async def test_convert_returns_amount_unchanged_for_usd(store):
    from app.core.fx import FxService

    svc = FxService(store=store)
    await svc.start()

    assert await svc.convert(100.0, "USD") == 100.0


async def test_get_rate_for_usd_returns_synthetic_one(store):
    from app.core.fx import FxService

    svc = FxService(store=store)
    await svc.start()

    rate = await svc.get_rate("USD")
    assert rate is not None
    assert rate.rate == 1.0
    assert rate.pair == "USDUSD"


# Unknown currency → None ---------------------------------------------------


async def test_get_rate_returns_none_when_currency_not_cached(store):
    from app.core.fx import FxService

    svc = FxService(store=store)
    await svc.start()

    assert await svc.get_rate("HKD") is None


async def test_convert_returns_none_when_no_rate_available(store):
    """When the row renderer gets None, it shows — in the USD column
    instead of $0.00 (which would falsely look like a valid zero)."""
    from app.core.fx import FxService

    svc = FxService(store=store)
    await svc.start()

    assert await svc.convert(100.0, "HKD") is None


# Set + read round-trip -----------------------------------------------------


async def test_set_rate_makes_get_rate_return_the_value(store):
    """Tests use this hook to seed rates; production wiring (IB ticks /
    API fallback) goes through the same path."""
    from app.core.fx import FxService

    svc = FxService(store=store)
    await svc.start()
    rate = FxRate(
        pair="HKDUSD",
        rate=0.1283,
        quoted_at=_at(12, 0),
        is_stale=False,
        source="IB",
    )

    await svc.set_rate(rate)

    got = await svc.get_rate("HKD")
    assert got is not None
    assert got.rate == pytest.approx(0.1283)


async def test_convert_uses_set_rate_value(store):
    from app.core.fx import FxService

    svc = FxService(store=store)
    await svc.start()
    await svc.set_rate(FxRate(
        pair="HKDUSD",
        rate=0.1283,
        quoted_at=_at(12, 0),
        is_stale=False,
        source="IB",
    ))

    assert await svc.convert(1000.0, "HKD") == pytest.approx(128.3)


# Persistence: set_rate writes to fx_cache, start() reads it back -----------


async def test_set_rate_persists_to_fx_cache(store):
    """Production wants every set_rate to also write to disk so a restart
    can recover last-known values."""
    from app.core.fx import FxService

    svc = FxService(store=store)
    await svc.start()
    await svc.set_rate(FxRate(
        pair="HKDUSD",
        rate=0.1283,
        quoted_at=_at(12, 0),
        is_stale=False,
        source="IB",
    ))

    row = await store.get_fx_rate("HKDUSD")
    assert row is not None
    assert row["rate"] == pytest.approx(0.1283)
    assert row["source"] == "IB"


async def test_start_loads_existing_fx_cache_into_memory(store):
    """The whole point of fx_cache: after a restart, USD columns show
    last-known values immediately without waiting for new ticks."""
    # Seed the store as if a previous session had run
    await store.put_fx_rate(
        pair="HKDUSD",
        rate=0.1290,
        source="IB",
        quoted_at=_at(11, 55),
    )
    from app.core.fx import FxService

    svc = FxService(store=store)
    await svc.start()

    got = await svc.get_rate("HKD")
    assert got is not None
    assert got.rate == pytest.approx(0.1290)
    assert got.source == "IB"


# CNY rejection at the convert boundary -------------------------------------


async def test_convert_rejects_cny(store):
    """If anything ever produces a CNY-denominated position (mis-configured
    contract, future bug), the conversion path must fail loudly rather than
    silently use HKD or skip the row."""
    from app.core.fx import FxService

    svc = FxService(store=store)
    await svc.start()

    with pytest.raises(ValueError) as excinfo:
        await svc.convert(100.0, "CNY")

    assert "CNY" in str(excinfo.value)
    assert "CNH" in str(excinfo.value)


async def test_get_rate_rejects_cny(store):
    from app.core.fx import FxService

    svc = FxService(store=store)
    await svc.start()

    with pytest.raises(ValueError):
        await svc.get_rate("CNY")
