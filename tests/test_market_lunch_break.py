"""Lunch break detection for HKEX (12:00-13:00 HKT), TSE (11:30-12:30 JST),
SSE/SZSE (11:30-13:00 CST). exchange_calendars surfaces these via
schedule['break_start'] / schedule['break_end'].

Markets without lunch breaks (NYSE, LSE, ASX, ...) have NaT for those
columns — those exchanges only ever see OPEN/CLOSED/HOLIDAY.
"""

from datetime import datetime, timezone

from app.core.markets import MarketHours, MarketState


def _utc(year, month, day, hh=0, mm=0):
    return datetime(year, month, day, hh, mm, tzinfo=timezone.utc)


# HKEX LUNCH ----------------------------------------------------------------


def test_hkex_lunch_at_12_30_hkt():
    """HKEX lunch break = 12:00-13:00 HKT = 04:00-05:00 UTC.
    Wednesday 2026-05-20 04:30 UTC = 12:30 HKT (squarely in lunch)."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 4, 30))

    status = hours.status("SEHK")

    assert status.state is MarketState.LUNCH


def test_hkex_open_in_afternoon_after_lunch():
    """13:30 HKT = 05:30 UTC. Lunch ended at 13:00 HKT, market reopens."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 5, 30))

    status = hours.status("SEHK")

    assert status.state is MarketState.OPEN


def test_hkex_open_in_morning_before_lunch():
    """11:00 HKT = 03:00 UTC — morning session, before lunch."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 3, 0))

    status = hours.status("SEHK")

    assert status.state is MarketState.OPEN


# Tokyo (TSE) LUNCH --------------------------------------------------------


def test_tse_lunch_at_12_00_jst():
    """TSE lunch = 11:30-12:30 JST = 02:30-03:30 UTC.
    Wednesday 2026-05-20 03:00 UTC = 12:00 JST."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 3, 0))

    status = hours.status("TSEJ")

    assert status.state is MarketState.LUNCH


# No-lunch exchange should never be LUNCH ----------------------------------


def test_nyse_never_returns_lunch():
    """NYSE has no lunch break. During trading hours it's OPEN, period."""
    # 12:00 ET = 16:00 UTC (DST off in May)
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 16, 0))

    status = hours.status("NYSE")

    assert status.state is not MarketState.LUNCH
    assert status.state is MarketState.OPEN


# Lunch on a closed day should NOT show LUNCH ------------------------------


def test_hkex_holiday_overrides_lunch_window():
    """If today is a HK holiday, 12:30 HKT should show HOLIDAY, not LUNCH.
    HK Labour Day 2026 = Friday May 1."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 1, 4, 30))

    status = hours.status("SEHK")

    assert status.state is MarketState.HOLIDAY
