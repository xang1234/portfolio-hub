"""Sessions for some exchanges open before UTC midnight of their local
trading day (e.g. ASX during AEDT opens at 23:00 UTC on the prior day,
TSE during winter opens at 00:00 UTC on the local day, KRX/SGX likewise
straddle midnight UTC for many windows).

`MarketHours.status` must key today's schedule by **exchange-local date**,
not by UTC date — otherwise it looks up the wrong session row and reports
CLOSED with a transition timestamp in the past.

Pinning ASX during AEDT (Oct–Apr) gives the clearest reproduction:
- AEDT = UTC+11
- ASX trades 10:00–16:00 AEDT = 23:00–05:00 UTC
- At 23:30 UTC on Wed 2026-01-14, ASX is OPEN in its Thu 2026-01-15 session.
  The UTC date is still 2026-01-14, but the session label is 2026-01-15.
"""

from datetime import datetime, timezone

from app.core.markets import MarketHours, MarketState


def _utc(year, month, day, hh=0, mm=0):
    return datetime(year, month, day, hh, mm, tzinfo=timezone.utc)


def test_asx_open_during_aedt_morning_when_utc_date_is_prior_day():
    """23:30 UTC on Wed 2026-01-14 = 10:30 AEDT Thu 2026-01-15 (ASX OPEN).

    The bug: status() was indexing the schedule by UTC date (2026-01-14)
    and finding yesterday's session (whose close was 05:00 UTC the same
    day) — returning CLOSED with a stale transition.
    """
    hours = MarketHours(clock=lambda: _utc(2026, 1, 14, 23, 30))

    status = hours.status("ASX")

    assert status is not None
    assert status.state is MarketState.OPEN
    # Next transition is today's close = 05:00 UTC Jan 15 (= 16:00 AEDT)
    assert status.next_transition_iso.startswith("2026-01-15T05:00")


def test_asx_next_transition_is_not_in_the_past():
    """Even if the state were CLOSED, the next-transition time must never
    be earlier than `now` — a "Opens in -30m" string is worse than no
    information at all."""
    now = _utc(2026, 1, 14, 23, 30)
    hours = MarketHours(clock=lambda: now)

    status = hours.status("ASX")

    if status.next_transition_iso:
        parsed = datetime.fromisoformat(status.next_transition_iso)
        assert parsed >= now, (
            f"transition {parsed} is before now {now} "
            f"(state={status.state}, label={status.next_transition_label})"
        )


def test_asx_pre_open_during_aedt_returns_closed_with_future_open():
    """At 22:00 UTC on Wed 2026-01-14 (= 09:00 AEDT Thu 2026-01-15), ASX
    has not yet opened. State must be CLOSED with the next transition
    pointing forward to the 23:00 UTC open of that same session."""
    hours = MarketHours(clock=lambda: _utc(2026, 1, 14, 22, 0))

    status = hours.status("ASX")

    assert status.state is MarketState.CLOSED
    assert status.next_transition_iso.startswith("2026-01-14T23:00")
