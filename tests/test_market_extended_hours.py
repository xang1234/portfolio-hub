"""US exchanges have pre-market (04:00-09:30 ET) and post-market
(16:00-20:00 ET) windows that exchange_calendars doesn't surface. We
hand-code them for NYSE/NASDAQ/ARCA/AMEX so the status panel shows
🌒 EXTENDED instead of 🔴 CLOSED during those hours.

Other exchanges never return EXTENDED — only OPEN/LUNCH/CLOSED/HOLIDAY.
"""

from datetime import datetime, timezone

from app.core.markets import MarketHours, MarketState


def _utc(year, month, day, hh=0, mm=0):
    return datetime(year, month, day, hh, mm, tzinfo=timezone.utc)


# NYSE pre-market ---------------------------------------------------------


def test_nyse_pre_market_at_06_00_et():
    """06:00 ET = 10:00 UTC (DST off in early May? Actually May is EDT).
    Use 2026-05-20 (Wednesday). EDT = UTC-4. So 06:00 EDT = 10:00 UTC.
    Wait — exchange_calendars schedule for XNYS uses naive timestamps...
    let's pick a date when DST is OFF: February. 2026-02-18 Wednesday."""
    # Feb 18 2026 (DST off): 06:00 EST = 11:00 UTC
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 11, 0))

    status = hours.status("NYSE")

    assert status.state is MarketState.EXTENDED
    assert status.extended_session == "PRE"


def test_nyse_pre_market_at_09_29_et_one_minute_before_open():
    """09:29 EST = 14:29 UTC (DST off). Still in pre-market."""
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 14, 29))

    status = hours.status("NYSE")

    assert status.state is MarketState.EXTENDED
    assert status.extended_session == "PRE"


def test_nyse_pre_market_at_03_59_too_early_is_closed():
    """03:59 EST = 08:59 UTC. Before pre-market (which starts 04:00 EST)."""
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 8, 59))

    status = hours.status("NYSE")

    assert status.state is MarketState.CLOSED


# NYSE post-market --------------------------------------------------------


def test_nyse_post_market_at_17_00_et():
    """17:00 EST = 22:00 UTC. Post-market 16:00-20:00 EST."""
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 22, 0))

    status = hours.status("NYSE")

    assert status.state is MarketState.EXTENDED
    assert status.extended_session == "POST"


def test_nyse_post_market_at_19_59_one_minute_before_close():
    """19:59 EST = 00:59 UTC next day."""
    hours = MarketHours(clock=lambda: _utc(2026, 2, 19, 0, 59))

    status = hours.status("NYSE")

    assert status.state is MarketState.EXTENDED
    assert status.extended_session == "POST"


def test_nyse_post_20_00_et_is_closed():
    """20:00 EST = 01:00 UTC next day. Past the 20:00 post-market end."""
    hours = MarketHours(clock=lambda: _utc(2026, 2, 19, 1, 0))

    status = hours.status("NYSE")

    assert status.state is MarketState.CLOSED


# NASDAQ / ARCA / AMEX share NYSE extended hours --------------------------


def test_nasdaq_pre_market():
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 11, 0))

    status = hours.status("NASDAQ")

    assert status.state is MarketState.EXTENDED
    assert status.extended_session == "PRE"


def test_arca_post_market():
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 22, 0))

    status = hours.status("ARCA")

    assert status.state is MarketState.EXTENDED
    assert status.extended_session == "POST"


# Holidays: no extended hours --------------------------------------------


def test_nyse_holiday_does_not_show_extended():
    """Christmas Day, regardless of time-of-day, remains HOLIDAY."""
    # 06:00 EST on Christmas would be pre-market on a session day
    hours = MarketHours(clock=lambda: _utc(2026, 12, 25, 11, 0))

    status = hours.status("NYSE")

    assert status.state is MarketState.HOLIDAY
    assert status.extended_session is None


# Regular session is OPEN, not EXTENDED ----------------------------------


def test_nyse_regular_session_at_10am_et_is_open_not_extended():
    """10:00 EST = 15:00 UTC. Inside 09:30-16:00 regular session."""
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 15, 0))

    status = hours.status("NYSE")

    assert status.state is MarketState.OPEN
    assert status.extended_session is None


# Non-US exchanges never return EXTENDED ---------------------------------


def test_hkex_never_returns_extended():
    """HKEX doesn't have pre/post markets in our model."""
    # Pick a non-trading time for HKEX (would-be pre/post if it had any)
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 8, 30))  # 16:30 HKT

    status = hours.status("SEHK")

    assert status.state is not MarketState.EXTENDED


def test_lse_never_returns_extended():
    """LSE post-close = CLOSED, not EXTENDED."""
    # 17:00 UK = 17:00 UTC (DST off in February)
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 17, 0))

    status = hours.status("LSE")

    assert status.state is not MarketState.EXTENDED
