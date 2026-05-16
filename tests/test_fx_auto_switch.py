"""Slice 3 cycle 7: prefer API rate when IB rate is stale.

Rule from the plan:
  When the IB rate becomes stale (market is open AND quote > 60s old),
  AND the public API has a fresher value, return the API rate from
  get_rate() so the row shows the 📡 Fallback FX badge.

When IB recovers (fresh tick arrives, source=IB), get_rate() returns
the IB rate again — the switch is per-read, not sticky.

If API has no rate for that currency (e.g. CNH), we keep returning the
stale IB rate. Better a slightly old rate than none, and the ⚠️ flag
still warns the user.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.fx import FxRate, FxService


def _at(year: int, month: int, day: int, hh: int, mm: int) -> datetime:
    return datetime(year, month, day, hh, mm, tzinfo=timezone.utc)


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


# Fresh IB + fresh API: prefer IB ----------------------------------------


async def test_prefers_ib_when_both_sources_are_fresh(store):
    """IB is the primary; API only takes over when IB is stale."""
    now = _at(2026, 5, 13, 14, 0)  # Wednesday market open
    svc = FxService(store=store, clock=lambda: now, api_fetcher=None)
    await svc.start()

    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=now - timedelta(seconds=10),
        is_stale=False, source="IB",
    ))
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1290, quoted_at=now - timedelta(minutes=30),
        is_stale=False, source="API_FALLBACK",
    ))

    rate = await svc.get_rate("HKD")
    assert rate.source == "IB"
    assert rate.rate == pytest.approx(0.1283)


# Stale IB + fresh API: prefer API --------------------------------------


async def test_prefers_api_when_ib_is_stale_and_api_is_fresher(store):
    """The whole point of the fallback: a frozen IB feed shouldn't trap
    the dashboard at a wrong-by-many-minutes rate when the API has
    something newer."""
    now = _at(2026, 5, 13, 14, 0)
    svc = FxService(store=store, clock=lambda: now, api_fetcher=None)
    await svc.start()

    # IB rate is 5 minutes stale (during market hours)
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=now - timedelta(minutes=5),
        is_stale=False, source="IB",
    ))
    # API rate is 30s old, fresher than IB
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1290, quoted_at=now - timedelta(seconds=30),
        is_stale=False, source="API_FALLBACK",
    ))

    rate = await svc.get_rate("HKD")
    assert rate.source == "API_FALLBACK"
    assert rate.rate == pytest.approx(0.1290)


# Stale IB + no API: keep IB (stale flag warns user) --------------------


async def test_keeps_stale_ib_when_api_has_no_rate_for_that_currency(store):
    """CNH path: API doesn't have it. Stale IB beats nothing, with ⚠️."""
    now = _at(2026, 5, 13, 14, 0)
    svc = FxService(store=store, clock=lambda: now, api_fetcher=None)
    await svc.start()

    await svc.set_rate(FxRate(
        pair="CNHUSD", rate=0.139, quoted_at=now - timedelta(minutes=5),
        is_stale=False, source="IB",
    ))
    # No API rate for CNH

    rate = await svc.get_rate("CNH")
    assert rate.source == "IB"
    assert rate.is_stale is True


# Stale IB + older API: keep IB ----------------------------------------


async def test_keeps_stale_ib_when_api_rate_is_even_older(store):
    """A 2-hour-old API rate is worse than a 5-minute-old IB rate. Keep IB."""
    now = _at(2026, 5, 13, 14, 0)
    svc = FxService(store=store, clock=lambda: now, api_fetcher=None)
    await svc.start()

    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=now - timedelta(minutes=5),
        is_stale=False, source="IB",
    ))
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1290, quoted_at=now - timedelta(hours=2),
        is_stale=False, source="API_FALLBACK",
    ))

    rate = await svc.get_rate("HKD")
    assert rate.source == "IB"


# Switch is not sticky -------------------------------------------------


async def test_switches_back_to_ib_when_fresh_ib_tick_arrives(store):
    """After auto-switching to API, a new IB tick should put us back on IB."""
    now = _at(2026, 5, 13, 14, 0)
    svc = FxService(store=store, clock=lambda: now, api_fetcher=None)
    await svc.start()

    # Start: IB stale, API fresh → API wins
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=now - timedelta(minutes=5),
        is_stale=False, source="IB",
    ))
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1290, quoted_at=now - timedelta(seconds=30),
        is_stale=False, source="API_FALLBACK",
    ))
    assert (await svc.get_rate("HKD")).source == "API_FALLBACK"

    # Fresh IB tick arrives
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1295, quoted_at=now - timedelta(seconds=5),
        is_stale=False, source="IB",
    ))

    rate = await svc.get_rate("HKD")
    assert rate.source == "IB"
    assert rate.rate == pytest.approx(0.1295)


# Market closed: stale logic doesn't apply, IB wins -------------------


async def test_market_closed_returns_ib_even_when_old(store):
    """Saturday: nothing's stale by definition. Return IB unchanged."""
    now = _at(2026, 5, 16, 14, 0)  # Saturday
    svc = FxService(store=store, clock=lambda: now, api_fetcher=None)
    await svc.start()

    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=now - timedelta(hours=6),
        is_stale=False, source="IB",
    ))
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1290, quoted_at=now - timedelta(minutes=10),
        is_stale=False, source="API_FALLBACK",
    ))

    rate = await svc.get_rate("HKD")
    assert rate.source == "IB"
    assert rate.is_stale is False
