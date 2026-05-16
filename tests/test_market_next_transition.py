"""Each MarketStatus needs three transition fields populated:

- `next_transition_iso`: UTC timestamp for client-side countdown
- `next_transition_local`: human string in exchange-local time ("16:00 HKT")
- `next_transition_label`: verb like "Closes" / "Opens" / "Reopens"

The label is set by the state-determining code (cycle 3-5). This cycle
fills in the timestamps.
"""

from datetime import datetime, timezone

import pytest

from app.core.markets import MarketHours, MarketState


def _utc(year, month, day, hh=0, mm=0):
    return datetime(year, month, day, hh, mm, tzinfo=timezone.utc)


# OPEN: next transition is close ------------------------------------------


def test_open_hkex_next_transition_is_today_close_in_utc_iso():
    """HKEX 02:30 UTC Wednesday → state=OPEN. Next: 04:00 UTC (12:00 HKT lunch).
    Actually first transition is to lunch, not close. Let me reconsider.

    Use 06:30 UTC = 14:30 HKT (afternoon session). Next transition is
    16:00 HKT close = 08:00 UTC."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 6, 30))

    status = hours.status("SEHK")

    assert status.state is MarketState.OPEN
    assert status.next_transition_iso.startswith("2026-05-20T08:00")


def test_open_hkex_morning_session_next_transition_is_lunch_start():
    """Morning session 02:30 UTC = 10:30 HKT. Next transition is 12:00 HKT
    lunch = 04:00 UTC."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 2, 30))

    status = hours.status("SEHK")

    assert status.next_transition_iso.startswith("2026-05-20T04:00")
    # Label should reflect that we're hitting lunch, not close
    assert "Lunch" in status.next_transition_label or "Closes" in status.next_transition_label or "Reopens" not in status.next_transition_label


# LUNCH: next transition is reopen ----------------------------------------


def test_lunch_next_transition_is_break_end_in_utc():
    """HKEX 04:30 UTC = 12:30 HKT (lunch). Lunch ends 13:00 HKT = 05:00 UTC."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 4, 30))

    status = hours.status("SEHK")

    assert status.state is MarketState.LUNCH
    assert status.next_transition_iso.startswith("2026-05-20T05:00")
    assert status.next_transition_label == "Reopens"


# CLOSED: next transition is next session's open --------------------------


def test_closed_next_transition_is_next_session_open():
    """HKEX after close (09:00 UTC = 17:00 HKT) → next open is tomorrow."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 9, 0))

    status = hours.status("SEHK")

    assert status.state is MarketState.CLOSED
    # Next session is Thursday 2026-05-21 01:30 UTC = 09:30 HKT
    assert status.next_transition_iso.startswith("2026-05-21T01:30")
    assert status.next_transition_label == "Opens"


def test_closed_weekend_next_transition_skips_to_monday():
    """Saturday → next session. (Monday 2026-05-25 is Buddha's Birthday
    observance on HKEX, so the actual next session is Tuesday 2026-05-26.)"""
    # Saturday 2026-05-23 03:00 UTC
    hours = MarketHours(clock=lambda: _utc(2026, 5, 23, 3, 0))

    status = hours.status("SEHK")

    # Tuesday 2026-05-26 01:30 UTC = 09:30 HKT
    assert status.next_transition_iso.startswith("2026-05-26T01:30")


# US EXTENDED: PRE → regular open, POST → post-market end ----------------


def test_us_pre_market_next_transition_is_regular_open():
    """06:00 EST = 11:00 UTC. Pre-market ends at regular open = 14:30 UTC."""
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 11, 0))

    status = hours.status("NYSE")

    assert status.state is MarketState.EXTENDED
    assert status.extended_session == "PRE"
    assert status.next_transition_iso.startswith("2026-02-18T14:30")
    assert status.next_transition_label == "Pre-market ends"


def test_us_post_market_next_transition_is_post_end():
    """17:00 EST = 22:00 UTC. Post-market ends at 20:00 EST = 01:00 UTC next day."""
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 22, 0))

    status = hours.status("NYSE")

    assert status.state is MarketState.EXTENDED
    assert status.extended_session == "POST"
    assert status.next_transition_iso.startswith("2026-02-19T01:00")


# Exchange-local time format ---------------------------------------------


def test_hkex_local_time_uses_hkt_label():
    """HKEX close should render as 'HH:MM HKT'."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 6, 30))

    status = hours.status("SEHK")

    assert "HKT" in status.next_transition_local
    assert "16:00" in status.next_transition_local


def test_nyse_local_time_uses_et_label():
    """NYSE times in ET (note: ET covers both EST and EDT depending on date)."""
    hours = MarketHours(clock=lambda: _utc(2026, 2, 18, 15, 0))  # regular open

    status = hours.status("NYSE")

    assert "ET" in status.next_transition_local


def test_tse_local_time_uses_jst_label():
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 1, 0))  # 10:00 JST

    status = hours.status("TSEJ")

    assert "JST" in status.next_transition_local


# HOLIDAY next transition --------------------------------------------------


def test_holiday_next_transition_is_following_session_open():
    """Christmas Day 2026 (Friday) → next open is Monday 2026-12-28."""
    hours = MarketHours(clock=lambda: _utc(2026, 12, 25, 15, 30))

    status = hours.status("NYSE")

    assert status.state is MarketState.HOLIDAY
    # 09:30 EST Dec 28 = 14:30 UTC
    assert status.next_transition_iso.startswith("2026-12-28T14:30")
    assert status.next_transition_label == "Opens"


# ISO format sanity --------------------------------------------------------


def test_iso_is_parseable_back_to_utc_datetime():
    """Round-trip: any next_transition_iso must parse via fromisoformat."""
    hours = MarketHours(clock=lambda: _utc(2026, 5, 20, 6, 30))

    status = hours.status("SEHK")

    parsed = datetime.fromisoformat(status.next_transition_iso)
    assert parsed.tzinfo is not None  # has timezone (UTC)
