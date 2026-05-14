"""Slice 3 cycle 6: staleness rule with FX-market-hours awareness.

Goal: don't show a misleading ⚠️ on rows when the FX market is closed
(Saturday + most of Sunday). Rates legitimately stop ticking then.

Rule per the plan:
  IB-sourced rate is stale when:
    (now - quoted_at) > 60s  AND  FX market is open
  FX market hours: Sunday 22:00 UTC → Friday 22:00 UTC

API_FALLBACK rates are never marked stale — they're a deliberate slow
source, not a feed that's expected to tick continuously.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.fx import FxRate, FxService, is_fx_market_open


def _at(year: int, month: int, day: int, hh: int, mm: int) -> datetime:
    return datetime(year, month, day, hh, mm, tzinfo=timezone.utc)


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


# is_fx_market_open helper -------------------------------------------------


def test_fx_market_open_during_weekday_us_session():
    """Wednesday 2026-05-13 14:00 UTC = ~10am ET, classic FX trading."""
    assert is_fx_market_open(_at(2026, 5, 13, 14, 0)) is True


def test_fx_market_closed_on_saturday():
    """Saturday is fully closed."""
    assert is_fx_market_open(_at(2026, 5, 16, 12, 0)) is False


def test_fx_market_open_after_sunday_22_utc():
    """Asian session starts Sunday 22:00 UTC."""
    assert is_fx_market_open(_at(2026, 5, 17, 22, 30)) is True


def test_fx_market_closed_before_sunday_22_utc():
    """Sunday morning UTC: still closed."""
    assert is_fx_market_open(_at(2026, 5, 17, 12, 0)) is False


def test_fx_market_closed_after_friday_22_utc():
    """NY close on Friday at 22:00 UTC = 5pm ET (DST inactive)."""
    assert is_fx_market_open(_at(2026, 5, 15, 22, 30)) is False


def test_fx_market_open_just_before_friday_22_utc():
    assert is_fx_market_open(_at(2026, 5, 15, 21, 59)) is True


# get_rate() applies staleness at read time --------------------------------


async def test_get_rate_marks_ib_rate_stale_when_older_than_60s_during_market(store):
    """During market hours, an IB quote older than 60s should be flagged."""
    quoted_at = _at(2026, 5, 13, 14, 0)  # Wednesday market open
    now = quoted_at + timedelta(seconds=90)

    svc = FxService(store=store, clock=lambda: now)
    await svc.start()
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=quoted_at,
        is_stale=False, source="IB",
    ))

    rate = await svc.get_rate("HKD")
    assert rate is not None
    assert rate.is_stale is True


async def test_get_rate_does_not_mark_stale_within_60s(store):
    quoted_at = _at(2026, 5, 13, 14, 0)
    now = quoted_at + timedelta(seconds=30)

    svc = FxService(store=store, clock=lambda: now)
    await svc.start()
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=quoted_at,
        is_stale=False, source="IB",
    ))

    rate = await svc.get_rate("HKD")
    assert rate.is_stale is False


async def test_get_rate_does_not_mark_stale_when_market_closed(store):
    """Saturday 12 UTC + 10 min old quote: no stale flag, market closed."""
    quoted_at = _at(2026, 5, 16, 12, 0)  # Saturday
    now = quoted_at + timedelta(minutes=10)

    svc = FxService(store=store, clock=lambda: now)
    await svc.start()
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=quoted_at,
        is_stale=False, source="IB",
    ))

    rate = await svc.get_rate("HKD")
    assert rate.is_stale is False


# API_FALLBACK is never stale ----------------------------------------------


async def test_api_fallback_rate_never_marked_stale_even_when_old(store):
    """API_FALLBACK is by definition a low-cadence source. Marking it
    stale would put ⚠️ on every row backed by it — meaningless noise."""
    quoted_at = _at(2026, 5, 13, 14, 0)
    now = quoted_at + timedelta(hours=2)

    svc = FxService(store=store, clock=lambda: now)
    await svc.start()
    await svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=quoted_at,
        is_stale=False, source="API_FALLBACK",
    ))

    rate = await svc.get_rate("HKD")
    assert rate.is_stale is False


# USD synthetic rate is never stale ----------------------------------------


async def test_usd_synthetic_rate_is_not_stale(store):
    """The USD=1.0 synthetic rate doesn't have a real quoted_at, so its
    staleness is always False to avoid confusing the row renderer."""
    svc = FxService(store=store)
    await svc.start()

    rate = await svc.get_rate("USD")
    assert rate.is_stale is False
