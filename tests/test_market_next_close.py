"""Slice 10 cycle 4: MarketHours.next_close_at(ib_exchange).

The snapshot scheduler needs to know "when does this exchange next close?"
which is subtly different from MarketStatus.next_transition_iso:

- next_transition_iso is "next state change of any kind" — could be the
  next OPEN if the market is currently CLOSED, or a LUNCH start, etc.
- next_close_at is specifically the next time the regular session ends.

The two must converge at the actual close moment but diverge during
overnight/weekend hours when next-transition is the morning open.

Behaviour:
- Returns a tz-aware UTC datetime.
- On half-days (US Christmas Eve 13:00 ET), returns the half-day close
  (not the regular 16:00 ET).
- On holidays, skips to the next trading day's close.
- Returns None for unmapped exchanges (caller skips).
- Strictly future: if `now` is exactly the close instant, returns the
  NEXT close (avoids re-firing the same snapshot on boundary races).
"""

from datetime import datetime, timezone

import pytest


# Unmapped exchange ----------------------------------------------------------


def test_next_close_at_unmapped_exchange_returns_none():
    from app.core.markets import MarketHours

    mh = MarketHours()
    assert mh.next_close_at("NOT_A_REAL_EXCH") is None


# NYSE: regular weekday close at 16:00 ET = 20:00 UTC ----------------------


def test_next_close_at_nyse_regular_weekday_morning():
    """At 10:00 ET on a Wednesday, next NYSE close is today at 20:00 UTC."""
    from app.core.markets import MarketHours

    # 2026-05-20 is a Wednesday in May (no holidays). NYSE trades 13:30-20:00 UTC.
    fixed_now = datetime(2026, 5, 20, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    mh = MarketHours(clock=lambda: fixed_now)
    nxt = mh.next_close_at("NYSE")
    assert nxt is not None
    assert nxt == datetime(2026, 5, 20, 20, 0, tzinfo=timezone.utc)


def test_next_close_at_nyse_after_close_jumps_to_next_session():
    """At 22:00 UTC (after close), next close is TOMORROW at 20:00 UTC."""
    from app.core.markets import MarketHours

    fixed_now = datetime(2026, 5, 20, 22, 0, tzinfo=timezone.utc)
    mh = MarketHours(clock=lambda: fixed_now)
    nxt = mh.next_close_at("NYSE")
    # Next trading day is Thursday 2026-05-21.
    assert nxt == datetime(2026, 5, 21, 20, 0, tzinfo=timezone.utc)


def test_next_close_at_exact_close_moment_jumps_to_next():
    """At exactly the close instant, return the NEXT close — strict-after
    semantic so the scheduler can't re-fire the same snapshot on the boundary."""
    from app.core.markets import MarketHours

    at_close = datetime(2026, 5, 20, 20, 0, tzinfo=timezone.utc)
    mh = MarketHours(clock=lambda: at_close)
    nxt = mh.next_close_at("NYSE")
    assert nxt > at_close  # NOT same-day 20:00 again


# NYSE half-day: Black Friday Nov 27 2026 closes 13:00 ET = 18:00 UTC ------


def test_next_close_at_nyse_half_day():
    """Black Friday 2026-11-27 is a NYSE half-day: closes 13:00 ET = 18:00 UTC.
    exchange_calendars knows this; next_close_at must respect it (not return
    20:00 UTC)."""
    from app.core.markets import MarketHours

    morning = datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)  # 11:00 ET
    mh = MarketHours(clock=lambda: morning)
    nxt = mh.next_close_at("NYSE")
    assert nxt == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc), (
        f"expected half-day 18:00 UTC, got {nxt}"
    )


# NYSE on a holiday morning skips to next session --------------------------


def test_next_close_at_skips_holiday():
    """Thanksgiving 2026 is 2026-11-26 (Thursday). At 14:00 UTC that day,
    next NYSE close should be Black Friday 2026-11-27 at 18:00 UTC (half-day),
    not today (closed)."""
    from app.core.markets import MarketHours

    thanksgiving_morning = datetime(2026, 11, 26, 14, 0, tzinfo=timezone.utc)
    mh = MarketHours(clock=lambda: thanksgiving_morning)
    nxt = mh.next_close_at("NYSE")
    assert nxt == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)


# HKEX: 08:00 UTC close ------------------------------------------------------


def test_next_close_at_hkex_regular_morning():
    """HKEX regular close 16:00 HKT = 08:00 UTC. At 03:00 UTC on a trading
    day, that day's close is in the future."""
    from app.core.markets import MarketHours

    # 2026-05-20 is a Wednesday — confirmed regular HKEX trading day.
    fixed_now = datetime(2026, 5, 20, 3, 0, tzinfo=timezone.utc)
    mh = MarketHours(clock=lambda: fixed_now)
    nxt = mh.next_close_at("SEHK")
    assert nxt == datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc)
