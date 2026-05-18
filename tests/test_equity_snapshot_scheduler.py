"""Slice 10 cycle 5: equity-snapshot scheduler.

The scheduler:
  1. Asks the adapter for current STK positions; collects distinct exchanges.
  2. Asks MarketHours for the next_close_at of each held exchange.
  3. Picks the soonest; sleeps until that moment.
  4. Wakes; calls get_account_summary; persists one equity_snapshot row per
     account with snapshot_session=f"{exchange}_CLOSE".
  5. Loops: re-compute held exchanges, re-find soonest, repeat.

All wall-clock / I/O is injected so tests can drive the loop in microseconds.
A snapshot that fails for one account doesn't stop the others or the loop.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.core.broker import AccountSummary, ConnectionState, Position


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _stk(account="U1", exchange="NASDAQ", symbol="AAPL"):
    return Position(
        broker="IBKR", account_id=account, native_key="1",
        canonical_symbol=f"{symbol}.US", native_symbol=symbol,
        exchange=exchange, currency="USD", name_en=symbol,
        asset_class="STK", quantity=10.0, avg_cost=100.0, last_price=110.0,
        market_value_native=1100.0, market_value_usd=1100.0,
        unrealized_pnl_native=100.0, unrealized_pnl_usd=100.0,
    )


def _summary(account="U1", base="USD", nlv_native=125000.0, nlv_usd=125000.0,
             cash_usd=30000.0, gpv_usd=95000.0):
    return AccountSummary(
        broker="IBKR", account_id=account, base_currency=base,
        net_liquidation_usd=nlv_usd, cash_usd=cash_usd, buying_power_usd=200000.0,
        net_liquidation_native=nlv_native, gross_position_value_usd=gpv_usd,
    )


class _FakeAdapter:
    name = "IBKR"
    def __init__(self, positions, summaries):
        self._positions = positions
        self._summaries = summaries
        self.get_summary_calls = 0
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self):
        self.get_summary_calls += 1
        return list(self._summaries)


class _StaticMarketHours:
    """Stub: returns a fixed next_close_at per exchange. Lets us prove the
    'soonest close wins' logic without invoking exchange_calendars."""
    def __init__(self, closes):
        self._closes = closes  # dict[str, datetime|None]
    def next_close_at(self, ib_exchange):
        return self._closes.get(ib_exchange)


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store
    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


# Picks the soonest close ----------------------------------------------------


async def test_scheduler_picks_soonest_close_across_held_exchanges(store):
    """When the portfolio holds positions on NASDAQ + SEHK, the scheduler
    must sleep until whichever closes first, then snapshot for that exchange."""
    from app.jobs.snapshot import scheduled_snapshot_loop

    adapter = _FakeAdapter(
        positions=[_stk(exchange="NASDAQ"), _stk(exchange="SEHK", symbol="700")],
        summaries=[_summary()],
    )
    # HKEX closes 08:00 UTC, NYSE closes 20:00 UTC → HKEX is sooner.
    hours = _StaticMarketHours({
        "NASDAQ": _utc("2026-05-20T20:00:00+00:00"),
        "SEHK":   _utc("2026-05-20T08:00:00+00:00"),
    })

    sleeps: list[float] = []
    now_ref = {"t": _utc("2026-05-20T03:00:00+00:00")}
    def now_fn(): return now_ref["t"]

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now_ref["t"] = now_ref["t"].replace() + _td(seconds)
        # cancel after the first iteration's wake-and-snapshot
        if len(sleeps) >= 2:
            raise asyncio.CancelledError()

    try:
        await scheduled_snapshot_loop(
            adapter, store, hours,
            sleep=fake_sleep, now=now_fn,
        )
    except asyncio.CancelledError:
        pass

    # First sleep was 03:00 UTC → 08:00 UTC = 5h.
    assert sleeps[0] == 5 * 3600, (
        f"first sleep should target HKEX close at 08:00 UTC = 5h from 03:00; "
        f"got {sleeps[0]}"
    )

    rows = await store.get_equity_snapshots_since(
        broker="IBKR", account_id="U1",
        since=_utc("2026-05-20T00:00:00+00:00"),
    )
    assert len(rows) == 1
    assert rows[0]["snapshot_session"] == "SEHK_CLOSE"
    assert rows[0]["snapshot_at"] == _utc("2026-05-20T08:00:00+00:00")


# One row per account --------------------------------------------------------


async def test_scheduler_inserts_one_row_per_account(store):
    from app.jobs.snapshot import scheduled_snapshot_loop

    adapter = _FakeAdapter(
        positions=[_stk(account="U1", exchange="NASDAQ")],
        summaries=[_summary(account="U1"), _summary(account="U2")],
    )
    hours = _StaticMarketHours({
        "NASDAQ": _utc("2026-05-20T20:00:00+00:00"),
    })
    now_ref = {"t": _utc("2026-05-20T14:00:00+00:00")}
    def now_fn(): return now_ref["t"]
    sleeps = []
    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now_ref["t"] = now_ref["t"] + _td(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError()

    try:
        await scheduled_snapshot_loop(adapter, store, hours, sleep=fake_sleep, now=now_fn)
    except asyncio.CancelledError:
        pass

    u1 = await store.get_equity_snapshots_since(
        broker="IBKR", account_id="U1", since=_utc("2026-05-20T00:00:00+00:00"),
    )
    u2 = await store.get_equity_snapshots_since(
        broker="IBKR", account_id="U2", since=_utc("2026-05-20T00:00:00+00:00"),
    )
    assert len(u1) == 1
    assert len(u2) == 1


# No held exchanges → no snapshot, but the loop survives --------------------


async def test_scheduler_sleeps_when_no_exchanges_held(store):
    """An account with no STK positions has no exchanges to schedule
    against. The loop should sleep a fallback interval and re-check
    rather than crashing or spinning."""
    from app.jobs.snapshot import scheduled_snapshot_loop

    adapter = _FakeAdapter(positions=[], summaries=[_summary()])
    hours = _StaticMarketHours({})
    sleeps = []
    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 1:
            raise asyncio.CancelledError()
    try:
        await scheduled_snapshot_loop(
            adapter, store, hours,
            sleep=fake_sleep, now=lambda: _utc("2026-05-20T03:00:00+00:00"),
            empty_recheck_interval=60.0,
        )
    except asyncio.CancelledError:
        pass

    assert sleeps == [60.0]
    # No rows were inserted.
    rows = await store.get_equity_snapshots_since(
        broker="IBKR", account_id="U1", since=_utc("2026-05-20T00:00:00+00:00"),
    )
    assert rows == []


# Loop survives one snapshot raising -----------------------------------------


async def test_scheduler_survives_get_account_summary_raising(store):
    """If get_account_summary blows up (transient IB error), the loop
    must keep going — next iteration recovers."""
    from app.jobs.snapshot import scheduled_snapshot_loop

    class _FlakyAdapter(_FakeAdapter):
        def __init__(self):
            super().__init__(
                positions=[_stk(exchange="NASDAQ")],
                summaries=[_summary()],
            )
            self._fail_next = True
        async def get_account_summary(self):
            self.get_summary_calls += 1
            if self._fail_next:
                self._fail_next = False
                raise RuntimeError("simulated IB blip")
            return list(self._summaries)

    adapter = _FlakyAdapter()

    # Each iteration sees a fresh "next close" 24h later so the loop
    # progresses rather than stalling at delay=0.
    class _RollingHours:
        def __init__(self):
            self._next = _utc("2026-05-20T20:00:00+00:00")
        def next_close_at(self, ib_exchange):
            val = self._next
            self._next = self._next + _td(86400)
            return val
    hours = _RollingHours()

    now_ref = {"t": _utc("2026-05-20T14:00:00+00:00")}
    sleeps = []
    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now_ref["t"] = now_ref["t"] + _td(seconds)
        # iter 1: sleep[0]=6h (to 20:00) → snapshot raises → sleep[1]=1s tick
        # iter 2: sleep[2]=~24h → snapshot succeeds → sleep[3]=1s tick
        # iter 3: sleep[4] → cancel before next snapshot
        if len(sleeps) >= 5:
            raise asyncio.CancelledError()

    try:
        await scheduled_snapshot_loop(
            adapter, store, hours, sleep=fake_sleep, now=lambda: now_ref["t"],
        )
    except asyncio.CancelledError:
        pass

    # Two snapshot attempts happened (one failed, one succeeded).
    assert adapter.get_summary_calls == 2
    # Loop is still alive when we cancelled — proved by len(sleeps) >= 4.
    assert len(sleeps) >= 4


# Helper ---------------------------------------------------------------------


def _td(seconds):
    from datetime import timedelta
    return timedelta(seconds=seconds)
