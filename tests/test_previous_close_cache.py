"""Previous-close per-UTC-day cache on IbkrAdapter.

Commit 2 added an always-fetch path so every STK position has a populated
`previous_close` for the intraday-% display. Without the cache, a refresh
loop firing every few seconds (live SSE redraws, /healthz polling, etc.)
would generate N reqHistoricalData calls per cycle — wasteful and likely
to trigger IB pacing violations. The cache keys per conId per UTC date,
so within a single trading day each conId is fetched exactly once.
"""

import pytest

# Reuse the existing fake-IB scaffolding from the fallback test.
from tests.test_historical_close_fallback import (  # noqa: E402
    FakeIB,
    _no_yahoo,
    _tse_position_with_conid,
)


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


async def test_second_get_positions_uses_cached_previous_close(store):
    """A second get_positions() inside the same UTC day must not re-issue
    reqHistoricalDataAsync — the in-memory cache absorbs it."""
    pos1, details1 = _tse_position_with_conid(1, "1111")
    pos2, details2 = _tse_position_with_conid(2, "2222")

    fake_ib = FakeIB(
        positions=[pos1, pos2],
        details={1: details1, 2: details2},
        historical_closes={1: 1101.0, 2: 1102.0},
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=_no_yahoo,
    )
    await adapter.connect()

    # First call — populates the cache. Two contracts → two historical fetches.
    first = await adapter.get_positions()
    assert sorted(fake_ib.historical_calls) == [1, 2]
    # Both positions carry the historical close as previous_close
    assert {p.native_symbol: p.previous_close for p in first} == {
        "1111": pytest.approx(1101.0),
        "2222": pytest.approx(1102.0),
    }

    # Second call within the same UTC day — should NOT issue new fetches.
    second = await adapter.get_positions()
    assert sorted(fake_ib.historical_calls) == [1, 2]  # unchanged
    assert {p.native_symbol: p.previous_close for p in second} == {
        "1111": pytest.approx(1101.0),
        "2222": pytest.approx(1102.0),
    }


async def test_cache_invalidates_on_utc_date_rollover(store):
    """If the cached date no longer matches today, re-fetch. Simulated by
    rewriting the cache value's date to a past day, then calling again."""
    import datetime as _dt

    pos1, details1 = _tse_position_with_conid(1, "1111")
    fake_ib = FakeIB(
        positions=[pos1],
        details={1: details1},
        historical_closes={1: 1101.0},
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=_no_yahoo,
    )
    await adapter.connect()

    await adapter.get_positions()
    assert fake_ib.historical_calls == [1]

    # Force the cache entry to look like it was written yesterday.
    yesterday = _dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=1)
    adapter._previous_close_cache[1] = (yesterday, 1101.0)

    # Update the mocked historical to a new value so we can prove the
    # re-fetch picked it up rather than serving the stale cache entry.
    fake_ib._historical_closes = {1: 1150.0}
    positions = await adapter.get_positions()

    assert fake_ib.historical_calls == [1, 1]  # one fresh fetch
    assert positions[0].previous_close == pytest.approx(1150.0)
