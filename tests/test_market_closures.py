"""Tests for the "Closures this week" market-rail sibling.

Three layers:

1. **Unit** on `MarketHours.closures_in_range` — verifies the data layer
   correctly distinguishes full holidays from scheduled early closes,
   handles multi-day holidays (Lunar New Year), and stays silent on
   unmapped exchanges.

2. **Helper** on `_closures_this_week` in main.py — verifies the
   per-card derived fields (is_today, is_past, date_label, ordering)
   are computed correctly relative to an injected clock.

3. **Integration** on `/` — renders the full page with a fake broker
   and asserts the closures strip appears (or doesn't) under the live
   market rail, scoped to `.market-section--closures` markup so a
   holdings-row-flag emoji can't satisfy the assertion by accident.
"""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position
from app.core.markets import ExchangeClosure, MarketHours


# ---- Shared fixtures ------------------------------------------------------


def _utc(year, month, day, hh=0, mm=0):
    return datetime(year, month, day, hh, mm, tzinfo=timezone.utc)


class _Fake:
    name = "IBKR"

    def __init__(self, positions):
        self._p = positions

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._p)
    async def get_account_summary(self): return []


def _stk(**overrides) -> Position:
    base = dict(
        broker="IBKR", account_id="U1", native_key="0",
        canonical_symbol="X.US", native_symbol="X",
        exchange="NYSE", currency="USD",
        name_en="X Corp", asset_class="STK",
        quantity=100.0, avg_cost=50.0, last_price=60.0,
        market_value_native=6000.0, market_value_usd=6000.0,
        unrealized_pnl_native=1000.0, unrealized_pnl_usd=1000.0,
    )
    base.update(overrides)
    return Position(**base)


# ============================================================================
# 1. Unit — MarketHours.closures_in_range
# ============================================================================


def test_independence_day_2025_returns_eve_early_close_and_full_holiday():
    """NYSE: Fri Jul 4 2025 is a full-day holiday (Independence Day), and
    Thu Jul 3 is the eve early close (13:00 ET). The strip surfaces both
    so users see the abbreviated session ahead of the holiday."""
    hours = MarketHours()
    closures = hours.closures_in_range(
        "NYSE", start=date(2025, 6, 30), end=date(2025, 7, 4),
    )
    assert closures == [
        ExchangeClosure(date=date(2025, 7, 3), kind="EARLY_CLOSE", close_local="13:00 ET"),
        ExchangeClosure(date=date(2025, 7, 4), kind="HOLIDAY", close_local=None),
    ]


def test_thanksgiving_week_returns_full_holiday_plus_early_close():
    """NYSE Thanksgiving week 2025: Thu Nov 27 closed (HOLIDAY),
    Fri Nov 28 early close (EARLY_CLOSE at 13:00 ET). Locks in that the
    library distinguishes the two correctly and that close_local is
    formatted in exchange-local time."""
    hours = MarketHours()
    closures = hours.closures_in_range(
        "NYSE", start=date(2025, 11, 24), end=date(2025, 11, 28),
    )
    assert closures == [
        ExchangeClosure(date=date(2025, 11, 27), kind="HOLIDAY", close_local=None),
        ExchangeClosure(
            date=date(2025, 11, 28), kind="EARLY_CLOSE", close_local="13:00 ET",
        ),
    ]


def test_lunar_new_year_2026_hkex_returns_multiple_holiday_days():
    """HKEX Lunar New Year 2026 spans Tue Feb 17 – Thu Feb 19 (closed),
    preceded by an early close on Mon Feb 16 (Lunar New Year Eve, 12:00
    HKT). The strip renders one card per day — no range-collapse logic
    yet, by design."""
    hours = MarketHours()
    closures = hours.closures_in_range(
        "SEHK", start=date(2026, 2, 16), end=date(2026, 2, 20),
    )
    kinds = [(c.date, c.kind) for c in closures]
    assert (date(2026, 2, 16), "EARLY_CLOSE") in kinds
    assert (date(2026, 2, 17), "HOLIDAY") in kinds
    assert (date(2026, 2, 18), "HOLIDAY") in kinds
    assert (date(2026, 2, 19), "HOLIDAY") in kinds
    # Friday Feb 20 is back to a normal session
    assert all(c.date != date(2026, 2, 20) for c in closures)


def test_empty_week_returns_empty_list():
    """A regular Mon-Fri with no closures returns []. Important — the
    template depends on falsy emptiness to suppress the entire section."""
    hours = MarketHours()
    # Week of Mon Mar 2 2026 — no US holidays
    closures = hours.closures_in_range(
        "NYSE", start=date(2026, 3, 2), end=date(2026, 3, 6),
    )
    assert closures == []


def test_unmapped_ib_exchange_returns_empty_list_without_crash():
    """Unknown IB codes silently return [] — same forgiving contract as
    MarketHours.status(). Crashing here would break the entire index
    render if a new venue surfaces from reqContractDetails."""
    hours = MarketHours()
    closures = hours.closures_in_range(
        "NOT_A_REAL_EXCHANGE", start=date(2026, 1, 1), end=date(2026, 1, 5),
    )
    assert closures == []


def test_weekends_are_skipped_not_reported_as_holidays():
    """Sat/Sun are expected non-sessions; reporting them as 'closures'
    would be noise. Range that spans a full week confirms no Sat/Sun
    appears even when the surrounding weekdays carry closures."""
    hours = MarketHours()
    # NYSE Jul 4 2025 week: range goes Sat Jun 28 to Sun Jul 6 — should
    # NOT include Sat Jun 28, Sun Jun 29, Sat Jul 5, or Sun Jul 6.
    closures = hours.closures_in_range(
        "NYSE", start=date(2025, 6, 28), end=date(2025, 7, 6),
    )
    weekend_dates = {date(2025, 6, 28), date(2025, 6, 29),
                     date(2025, 7, 5), date(2025, 7, 6)}
    assert not any(c.date in weekend_dates for c in closures), (
        "weekends should never appear in the closures list"
    )
    # Sanity: the weekday closures (Jul 3 early, Jul 4 holiday) still appear
    assert any(c.date == date(2025, 7, 3) for c in closures)
    assert any(c.date == date(2025, 7, 4) for c in closures)


# ============================================================================
# 2. Helper — _closures_this_week (date_label, is_today, is_past, order)
# ============================================================================


def test_closures_this_week_marks_today_and_past_correctly():
    """When the clock is pinned to mid-week, past weekdays in this week
    carry is_past=True and the current day carries is_today=True. The
    template uses these to dim past days and warm-halo today."""
    from app.main import _closures_this_week
    from app.core.markets import MarketState, MarketStatus

    # Pin clock to Wed Nov 26 2025 — Thanksgiving is Thu Nov 27.
    hours = MarketHours()
    status_by_ib = {
        "NYSE": MarketStatus(
            exchange="NYSE", state=MarketState.OPEN, extended_session=None,
            next_transition_local="", next_transition_iso="",
            next_transition_label="",
        ),
    }
    flag_by_ib = {"NYSE": "🇺🇸"}
    cards = _closures_this_week(
        status_by_ib, flag_by_ib, hours, now=_utc(2025, 11, 26, 18, 0),
    )

    # Two cards: Thu Nov 27 (HOLIDAY, future), Fri Nov 28 (EARLY_CLOSE, future)
    assert len(cards) == 2
    assert cards[0].date_iso == "2025-11-27"
    assert cards[0].kind == "HOLIDAY"
    assert cards[0].is_today is False
    assert cards[0].is_past is False
    assert cards[1].date_iso == "2025-11-28"
    assert cards[1].kind == "EARLY_CLOSE"
    assert cards[1].close_local == "13:00 ET"
    assert cards[0].flag == "🇺🇸"  # threaded through from flag_by_ib


def test_closures_this_week_marks_today_on_early_close():
    """Clock pinned to the half-day itself — is_today fires on the
    EARLY_CLOSE card (which is NOT deduped, unlike a today-holiday).
    Friday Nov 28 2025 is the day-after-Thanksgiving 13:00 ET close."""
    from app.main import _closures_this_week
    from app.core.markets import MarketState, MarketStatus

    hours = MarketHours()
    status_by_ib = {
        "NYSE": MarketStatus(
            exchange="NYSE", state=MarketState.OPEN, extended_session=None,
            next_transition_local="", next_transition_iso="",
            next_transition_label="",
        ),
    }
    flag_by_ib = {"NYSE": "🇺🇸"}
    cards = _closures_this_week(
        status_by_ib, flag_by_ib, hours, now=_utc(2025, 11, 28, 15, 0),
    )

    # Thursday is now in the past (within this week)
    thanksgiving = next(c for c in cards if c.date_iso == "2025-11-27")
    assert thanksgiving.is_today is False
    assert thanksgiving.is_past is True
    # Friday early close — today
    early = next(c for c in cards if c.date_iso == "2025-11-28")
    assert early.is_today is True
    assert early.is_past is False


def test_today_holiday_is_deduped_when_live_status_already_holiday():
    """If today is itself a HOLIDAY and the exchange's live status
    already says HOLIDAY, the closures strip skips that card to avoid
    duplicating the message the live market rail already conveys."""
    from app.main import _closures_this_week
    from app.core.markets import MarketState, MarketStatus

    hours = MarketHours()
    # Live status reports HOLIDAY (which is what status() would actually
    # return on a holiday morning).
    status_by_ib = {
        "NYSE": MarketStatus(
            exchange="NYSE", state=MarketState.HOLIDAY, extended_session=None,
            next_transition_local="", next_transition_iso="",
            next_transition_label="",
        ),
    }
    flag_by_ib = {"NYSE": "🇺🇸"}
    # Clock on Thanksgiving — the only weekday closure today is Nov 27
    cards = _closures_this_week(
        status_by_ib, flag_by_ib, hours, now=_utc(2025, 11, 27, 18, 0),
    )

    # Thursday's holiday card is suppressed (live rail already shows it)
    assert all(c.date_iso != "2025-11-27" for c in cards), (
        "today-holiday card should be deduped when live status==HOLIDAY"
    )
    # But Friday's early close is still listed — the live card on Fri
    # morning would say OPEN/Closes, so the strip's advance warning
    # remains useful.
    assert any(
        c.date_iso == "2025-11-28" and c.kind == "EARLY_CLOSE" for c in cards
    )


def test_today_early_close_is_not_deduped():
    """Symmetric guard: an EARLY_CLOSE card on its own day stays
    visible — the live market card on a half-day shows OPEN/Closes at
    13:00 ET, not 'early close', so the strip's explicit label is the
    only place the user sees the abbreviation called out."""
    from app.main import _closures_this_week
    from app.core.markets import MarketState, MarketStatus

    hours = MarketHours()
    status_by_ib = {
        "NYSE": MarketStatus(
            exchange="NYSE", state=MarketState.OPEN, extended_session=None,
            next_transition_local="", next_transition_iso="",
            next_transition_label="",
        ),
    }
    flag_by_ib = {"NYSE": "🇺🇸"}
    cards = _closures_this_week(
        status_by_ib, flag_by_ib, hours, now=_utc(2025, 11, 28, 15, 0),
    )

    early = next(c for c in cards if c.date_iso == "2025-11-28")
    assert early.kind == "EARLY_CLOSE"
    assert early.is_today is True


def test_closures_this_week_sorts_by_date_then_exchange():
    """Two held exchanges sort by date first so the strip reads
    chronologically left-to-right; within the same date, exchanges
    sort alphabetically by display name for a stable order.

    Week of Mon Feb 16 2026: NYSE is closed Presidents Day (Mon Feb 16),
    HKEX is closed Tue Feb 17 – Thu Feb 19 (Lunar New Year) plus has
    an EARLY_CLOSE on Mon Feb 16 (LNY eve). So Monday Feb 16 carries
    TWO closures (HKEX early, NYSE holiday) — perfect for testing the
    same-date alphabetical tiebreaker."""
    from app.main import _closures_this_week
    from app.core.markets import MarketState, MarketStatus

    hours = MarketHours()
    status_by_ib = {
        # Insertion order chosen to be the opposite of alphabetical so
        # the sort actually has to do work.
        "SEHK": MarketStatus(
            exchange="HKEX", state=MarketState.OPEN, extended_session=None,
            next_transition_local="", next_transition_iso="",
            next_transition_label="",
        ),
        "NYSE": MarketStatus(
            exchange="NYSE", state=MarketState.OPEN, extended_session=None,
            next_transition_local="", next_transition_iso="",
            next_transition_label="",
        ),
    }
    flag_by_ib = {"SEHK": "🇭🇰", "NYSE": "🇺🇸"}
    cards = _closures_this_week(
        status_by_ib, flag_by_ib, hours, now=_utc(2026, 2, 16, 8, 0),
    )

    # Chronological order overall
    dates = [c.date_iso for c in cards]
    assert dates == sorted(dates)

    # Mon Feb 16: HKEX (alphabetically first) then NYSE
    monday_cards = [c for c in cards if c.date_iso == "2026-02-16"]
    assert [c.exchange_display for c in monday_cards] == ["HKEX", "NYSE"]


# ============================================================================
# 3. Integration — full / render
# ============================================================================


def _client(positions, *, now=None):
    from app.main import create_app
    market_hours = None
    if now is not None:
        market_hours = MarketHours(clock=lambda: now)
    return TestClient(create_app(broker=_Fake(positions), market_hours=market_hours))


def test_closures_section_renders_inside_market_section_closures():
    """Held US position; assert the closures strip appears as a sibling
    section with the dedicated `.market-section--closures` class."""
    text = _client([_stk()], now=_utc(2025, 6, 30, 12, 0)).get("/").text
    # The strip's container class — distinct from the live market rail
    assert 'market-section--closures' in text


def test_closures_section_absent_when_no_closures_in_held_exchanges():
    """If the user holds nothing or holds only exchanges with no
    closures in the current week, the section should be omitted
    entirely (no empty `.market-rail-head` shell). Hard to assert
    deterministically across calendar weeks, so we test the data-layer
    contract: empty closure list → no section markup. We exercise the
    template helper's else branch by rendering with no STK positions
    (CASH-only portfolio has no held exchanges → no closures lookup
    → empty list → section omitted)."""
    cash = _stk(asset_class="CASH", exchange="", quantity=1000.0)
    text = _client([cash]).get("/").text
    assert 'market-section--closures' not in text


def test_early_close_card_carries_dedicated_modifier_class():
    """Lock in the `.market-card--early-close` modifier so a CSS rename
    can't silently demote half-days to the muted holiday styling.
    Tested via direct partial render so we don't depend on the current
    real calendar week containing an early close."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from app.main import TEMPLATES_DIR, ClosureCard

    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("partials/closure_card.html")
    card = ClosureCard(
        flag="🇺🇸",
        exchange_display="NYSE",
        date_iso="2025-11-28",
        date_label="Fri Nov 28",
        kind="EARLY_CLOSE",
        close_local="13:00 ET",
        is_today=False,
        is_past=False,
    )
    html = tmpl.render(c=card)

    assert 'market-card--early-close' in html
    assert 'market-card--holiday' not in html
    assert 'Early close' in html
    assert '13:00 ET' in html


def test_full_holiday_card_carries_holiday_modifier_not_early_close():
    """Symmetric guard: a HOLIDAY card must NOT pick up the early-close
    class. Distinct visual treatment per kind is the whole point of the
    modifier."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from app.main import TEMPLATES_DIR, ClosureCard

    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("partials/closure_card.html")
    card = ClosureCard(
        flag="🇺🇸",
        exchange_display="NYSE",
        date_iso="2025-11-27",
        date_label="Thu Nov 27",
        kind="HOLIDAY",
        close_local=None,
        is_today=False,
        is_past=False,
    )
    html = tmpl.render(c=card)

    assert 'market-card--holiday' in html
    assert 'market-card--early-close' not in html
    assert 'Closed' in html


def test_today_card_carries_today_modifier():
    """The today-halo class lives only on today's row, never on past or
    future rows."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from app.main import TEMPLATES_DIR, ClosureCard

    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("partials/closure_card.html")
    today = ClosureCard(
        flag="🇺🇸", exchange_display="NYSE",
        date_iso="2026-01-19", date_label="Mon Jan 19",
        kind="HOLIDAY", close_local=None,
        is_today=True, is_past=False,
    )
    past = ClosureCard(
        flag="🇺🇸", exchange_display="NYSE",
        date_iso="2026-01-19", date_label="Mon Jan 19",
        kind="HOLIDAY", close_local=None,
        is_today=False, is_past=True,
    )
    assert 'market-card--today' in tmpl.render(c=today)
    assert 'market-card--today' not in tmpl.render(c=past)
    assert 'market-card--past' in tmpl.render(c=past)
    assert 'market-card--past' not in tmpl.render(c=today)
