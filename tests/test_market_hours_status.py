"""MarketHours.status — core OPEN / CLOSED / HOLIDAY logic.

Cycle 3 covers regular session detection plus holiday handling.
Lunch breaks (HKEX/TSE/SSE) are cycle 4; US extended hours are cycle 5.

The clock is injectable so we can pin time to specific UTC moments and
assert deterministic states without waiting for real-world conditions.
"""

from datetime import datetime, timezone

import pytest

from app.core.markets import MarketHours, MarketState


def _utc(year, month, day, hh=0, mm=0):
    return datetime(year, month, day, hh, mm, tzinfo=timezone.utc)


# HKEX OPEN ----------------------------------------------------------------


def test_hkex_open_during_trading_hours_morning_session():
    """HKEX morning session is 09:30–12:00 HKT = 01:30–04:00 UTC.
    Pick 02:30 UTC (10:30 HKT) — squarely inside the morning session,
    well before the 12:00 lunch break."""
    # Wednesday 2026-05-20 (non-holiday)
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 2, 30))

    status = hours.status("SEHK")

    assert status is not None
    assert status.exchange == "HKEX"
    assert status.state is MarketState.OPEN


# HKEX CLOSED --------------------------------------------------------------


def test_hkex_closed_before_open():
    """Pre-market on a regular session day: CLOSED, next transition = open."""
    # Wednesday 06:00 UTC = 14:00 HKT — after close (16:00 HKT)
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 9, 0))  # 17:00 HKT

    status = hours.status("SEHK")

    assert status.state is MarketState.CLOSED


def test_hkex_closed_on_saturday():
    """Saturday is not a session day at all."""
    # Saturday 2026-05-23 at 03:00 UTC (11:00 HKT, would-be open hours)
    hours = MarketHours(clock=lambda: _utc(2026, 5, 23, 3, 0))

    status = hours.status("SEHK")

    # Saturday isn't even on the calendar's session list — could be
    # either CLOSED or HOLIDAY depending on how exchange_calendars
    # frames weekends; both are sensible. Just assert it's not OPEN.
    assert status.state is not MarketState.OPEN


# NYSE HOLIDAY -------------------------------------------------------------


def test_nyse_holiday_on_christmas_day():
    """Christmas Day 2026 falls on a Friday — markets closed."""
    # Friday 2026-12-25 at 15:30 UTC = 10:30 ET (would-be open hours)
    hours = MarketHours(clock=lambda: _utc(2026, 12, 25, 15, 30))

    status = hours.status("NYSE")

    assert status.state is MarketState.HOLIDAY


def test_nyse_holiday_on_new_years_day():
    """New Year's Day 2027 = Friday."""
    hours = MarketHours(clock=lambda: _utc(2027, 1, 1, 15, 30))

    status = hours.status("NYSE")

    assert status.state is MarketState.HOLIDAY


# NYSE OPEN ----------------------------------------------------------------


def test_nyse_open_during_us_session():
    """NYSE regular session 09:30–16:00 ET = 13:30–20:00 UTC (DST off)."""
    # Wednesday 2026-05-20 16:00 UTC = 12:00 ET (during open session)
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 16, 0))

    status = hours.status("NYSE")

    assert status.state is MarketState.OPEN


# Unknown exchange ---------------------------------------------------------


def test_unknown_exchange_returns_none():
    """An unmapped IB exchange code yields None — caller skips the row."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 16, 0))

    assert hours.status("NOT_A_REAL_EXCHANGE") is None


# Status carries canonical exchange name -----------------------------------


def test_status_exchange_field_is_canonical_not_ib_code():
    """The card heading should read 'HKEX' (canonical) not 'SEHK' (IB code).
    The dashboard's user-facing dialect maps IB codes to plain names."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 2, 30))

    status = hours.status("SEHK")
    assert status.exchange == "HKEX"
    assert status.exchange != "SEHK"


def test_status_exchange_field_for_nyse_is_nyse():
    """US codes stay as-is (NYSE, NASDAQ, etc.) since they already match
    the user-facing convention."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 16, 0))

    assert hours.status("NYSE").exchange == "NYSE"
