"""Slice 3 cycle 5: public-API fallback fetcher.

When IB FX data is missing or stale, we fall back to open.er-api.com
for last-known rates. This is the "📡 Fallback FX" path.

Cycle 5 scope: the fetch + apply mechanism. The activation rule —
"only prefer API over IB when IB is stale" — comes in cycle 7.

CNH gotcha: the public API exposes CNY (onshore) but NOT CNH (offshore,
what IB returns for Stock Connect A-shares). We REFUSE to substitute
CNY for CNH because the two rates can diverge 1-3%. CNH rows with no
IB rate render — in the USD column.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core.fx import FxService


def _at(hh: int, mm: int) -> datetime:
    return datetime(2026, 5, 15, hh, mm, tzinfo=timezone.utc)


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _build_api_response(rates: dict[str, float]) -> dict:
    """Mimic the shape of open.er-api.com's response."""
    return {
        "result": "success",
        "provider": "https://www.exchangerate-api.com",
        "base_code": "USD",
        "rates": rates,
        "time_last_update_utc": "Fri, 15 May 2026 12:00:00 +0000",
    }


# Inverse conversion --------------------------------------------------------


async def test_api_rates_are_inverted_to_usd_per_native(store):
    """The API gives '1 USD = X HKD'. We need 'how many USD per HKD' = 1/X."""
    async def fake_fetcher():
        return _build_api_response({"HKD": 7.8, "JPY": 156.0})

    svc = FxService(store=store, api_fetcher=fake_fetcher)
    await svc.start()
    await svc.refresh_from_api()

    hkd = await svc.get_rate("HKD")
    assert hkd is not None
    assert hkd.rate == pytest.approx(1 / 7.8, rel=1e-6)
    assert hkd.source == "API_FALLBACK"


# CNH must not be substituted with CNY --------------------------------------


async def test_api_skips_cnh_even_when_cny_is_in_response(store):
    """The API has CNY but not CNH. Silently using CNY for CNH would
    misprice HK-routed A-share positions. Must stay None."""
    async def fake_fetcher():
        return _build_api_response({"HKD": 7.8, "CNY": 7.2})  # no CNH

    svc = FxService(store=store, api_fetcher=fake_fetcher)
    await svc.start()
    await svc.refresh_from_api()

    assert await svc.get_rate("CNH") is None  # not substituted from CNY


async def test_api_does_not_record_cny_as_a_rate(store):
    """Even though CNY appears in the API response, validate_currency
    must reject it. The fetcher must not attempt to store CNY."""
    async def fake_fetcher():
        return _build_api_response({"CNY": 7.2})

    svc = FxService(store=store, api_fetcher=fake_fetcher)
    await svc.start()
    await svc.refresh_from_api()  # must not raise

    # CNY isn't a SUPPORTED_FX currency — nothing should appear
    row = await store.get_fx_rate("CNYUSD")
    assert row is None


# Network / parsing errors --------------------------------------------------


async def test_api_fetcher_returning_none_is_handled_gracefully(store):
    """If the public API is down (httpx error → None), refresh_from_api()
    must not raise. It just leaves the existing cache as-is."""
    async def fake_fetcher():
        return None

    svc = FxService(store=store, api_fetcher=fake_fetcher)
    await svc.start()
    await svc.refresh_from_api()  # must not raise

    assert await svc.get_rate("HKD") is None  # nothing got applied


async def test_api_response_missing_rates_key_is_handled(store):
    """Malformed JSON without a 'rates' key: don't crash."""
    async def fake_fetcher():
        return {"result": "error"}

    svc = FxService(store=store, api_fetcher=fake_fetcher)
    await svc.start()
    await svc.refresh_from_api()  # must not raise

    assert await svc.get_rate("HKD") is None


async def test_api_skips_currencies_with_zero_or_negative_rates(store):
    """A garbage rate of 0.0 would division-by-zero. Skip it."""
    async def fake_fetcher():
        return _build_api_response({"HKD": 7.8, "JPY": 0.0, "KRW": -1.0})

    svc = FxService(store=store, api_fetcher=fake_fetcher)
    await svc.start()
    await svc.refresh_from_api()

    assert await svc.get_rate("HKD") is not None  # good rate persisted
    assert await svc.get_rate("JPY") is None  # zero rejected
    assert await svc.get_rate("KRW") is None  # negative rejected


# Persistence ---------------------------------------------------------------


async def test_api_rates_are_persisted_with_api_fallback_source(store):
    """Restart-after-fallback should keep the 📡 badge."""
    async def fake_fetcher():
        return _build_api_response({"HKD": 7.8})

    svc = FxService(store=store, api_fetcher=fake_fetcher)
    await svc.start()
    await svc.refresh_from_api()
    await asyncio.sleep(0.02)  # let persist tasks settle

    row = await store.get_fx_rate("HKDUSD")
    assert row is not None
    assert row["source"] == "API_FALLBACK"


# Periodic polling ----------------------------------------------------------


async def test_start_schedules_periodic_api_polling(store):
    """start() should kick off a background task that calls the fetcher
    on the configured interval."""
    call_count = 0

    async def fake_fetcher():
        nonlocal call_count
        call_count += 1
        return _build_api_response({"HKD": 7.8})

    svc = FxService(
        store=store,
        api_fetcher=fake_fetcher,
        api_poll_interval_s=0.05,
    )
    await svc.start()
    await asyncio.sleep(0.18)  # ~3 polls
    await svc.stop()

    assert call_count >= 2


async def test_stop_cancels_polling_task(store):
    """No leaked tasks after stop()."""
    async def fake_fetcher():
        return _build_api_response({"HKD": 7.8})

    svc = FxService(
        store=store,
        api_fetcher=fake_fetcher,
        api_poll_interval_s=0.05,
    )
    await svc.start()
    await asyncio.sleep(0.1)
    await svc.stop()

    calls_after_stop = 0
    # No further polls should happen
    await asyncio.sleep(0.15)
    # We can't easily count further calls without instrumenting; instead
    # assert the task is done.
    assert svc._api_task is None or svc._api_task.done()
