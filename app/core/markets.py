"""Market hours subsystem.

Computes per-exchange trading state (OPEN / EXTENDED / LUNCH / CLOSED /
HOLIDAY) for the dashboard's market-status panel. Backed by
`exchange_calendars` for regular sessions, lunch breaks, and holidays;
US extended hours (pre/post market) are hand-coded since
exchange_calendars doesn't model them.

The MarketStatus type is the only thing the rest of the app sees — the
template renders one card per status.

`from __future__ import annotations` is required because MarketHours.status
returns `MarketStatus | None` but MarketStatus is defined further down;
without deferred annotations the module fails to import on Python <3.14.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Literal


_LOG = logging.getLogger(__name__)


# IB Contract.primaryExchange → ISO MIC code for exchange_calendars.
#
# Hong Kong (XHKG), Taiwan (XTAI), and mainland China (XSHG) are
# deliberately three distinct calendars — they have different lunch
# breaks, holidays, and closing times. Never collapse them.
#
# Unknown codes return None and the market-status panel skips that
# exchange rather than guessing. Add new entries here as new venues
# surface from reqContractDetails.
_IB_EXCHANGE_TO_MIC: dict[str, str] = {
    # Asia-Pacific
    "SEHK": "XHKG",      # Hong Kong
    "SEHKNTL": "XHKG",   # Stock Connect Northbound (uses HK schedule)
    "SEHKSZSE": "XHKG",
    "TSEJ": "XTKS",      # Tokyo
    "TSE": "XTKS",
    "OSE": "XTKS",       # Osaka uses Tokyo calendar
    "KRX": "XKRX",       # Korea (KOSPI)
    "KSE": "XKRX",
    "KOSDAQ": "XKRX",
    "TWSE": "XTAI",      # Taiwan main board
    "TPEX": "XTAI",      # Taipei OTC
    "SSE": "XSHG",       # Shanghai
    "SZSE": "XSHG",      # Shenzhen — exchange_calendars unifies with Shanghai
    "SGX": "XSES",       # Singapore
    "ASX": "XASX",       # Sydney
    # Europe
    "LSE": "XLON",       # London
    "IOB": "XLON",
    "IBIS": "XETR",      # Xetra Frankfurt
    "FWB": "XETR",
    "SBF": "XPAR",       # Euronext Paris
    "AEB": "XAMS",       # Euronext Amsterdam
    "BM": "XMAD",        # Madrid
    "BVME": "XMIL",      # Borsa Italiana Milan
    "SFB": "XSTO",       # Stockholm
    "EBS": "XSWX",       # Swiss
    "SIX": "XSWX",
    # Americas
    "TSX": "XTSE",       # Toronto
    "NYSE": "XNYS",
    "NASDAQ": "XNYS",    # exchange_calendars unifies US under XNYS
    "ARCA": "XNYS",
    "AMEX": "XNYS",
    "BATS": "XNYS",
}


def mic_for_ib_exchange(ib_exchange: str) -> str | None:
    """Return the ISO MIC code for `exchange_calendars`, or None if unmapped."""
    return _IB_EXCHANGE_TO_MIC.get(ib_exchange)


# IB code → user-facing exchange name shown on the card header.
# Most IB codes already match the canonical name (NYSE, ASX, LSE, SGX);
# the asian venues need translation (SEHK → HKEX, TSEJ → TSE).
_IB_EXCHANGE_TO_DISPLAY: dict[str, str] = {
    "SEHK": "HKEX",
    "SEHKNTL": "HKEX",
    "SEHKSZSE": "HKEX",
    "TSEJ": "TSE",
    "OSE": "OSE",
    "KSE": "KRX",
    "IBIS": "Xetra",
    "FWB": "Xetra",
    "SBF": "Euronext Paris",
    "AEB": "Euronext Amsterdam",
    "BM": "BME Madrid",
    "BVME": "Borsa Italiana",
    "SFB": "Stockholm",
    "EBS": "SIX Swiss",
    "SIX": "SIX Swiss",
    "TPEX": "TPEx",
}


def display_name_for_ib_exchange(ib_exchange: str) -> str:
    """User-facing exchange name. Falls back to the IB code itself for
    venues that already match the canonical convention (NYSE, ASX, ...)."""
    return _IB_EXCHANGE_TO_DISPLAY.get(ib_exchange, ib_exchange)


# US equity exchanges that observe pre/post-market sessions in our model.
# Pre-market starts 5h30m before regular open (04:00 ET ↔ 09:30 ET); post
# runs 4h after regular close (16:00 ET ↔ 20:00 ET). Anchoring off the
# exchange_calendars open/close timestamps sidesteps DST handling.
_US_EXTENDED_EXCHANGES: frozenset[str] = frozenset(
    {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
)


# Per-state emoji shown in the drawer's collapsed glyph row and inside each
# market card. Single source of truth — both the panel and the card partial
# pull from this map so they can never drift apart.
STATE_EMOJI: dict[str, str] = {
    "OPEN": "🟢",
    "EXTENDED": "🌒",
    "LUNCH": "🟡",
    "CLOSED": "🔴",
    "HOLIDAY": "⚫",
}


# Exchange-local timezone abbreviations for the "16:00 HKT" line on the
# market-status card. Hardcoded (rather than %Z) because the OS-provided
# abbreviation varies by platform and splits across DST variants (EST/EDT).
# "ET" deliberately collapses EST and EDT — users want a stable label.
_MIC_TO_TZ_LABEL: dict[str, str] = {
    "XHKG": "HKT",
    "XTKS": "JST",
    "XKRX": "KST",
    "XTAI": "TST",
    "XSHG": "CST",
    "XSES": "SGT",
    "XASX": "AEST",
    "XLON": "GMT",
    "XETR": "CET",
    "XPAR": "CET",
    "XAMS": "CET",
    "XMAD": "CET",
    "XMIL": "CET",
    "XSTO": "CET",
    "XSWX": "CET",
    "XNYS": "ET",
    "XTSE": "ET",
}


def _format_local(utc_dt: datetime, cal, mic: str) -> str:
    """Render a UTC moment in the exchange's local time as 'HH:MM TZ'.

    Falls back to %Z if the MIC has no canonical label, which keeps newly
    added venues from rendering an empty string. Any unexpected attribute
    or value error is logged so a silent regression is at least traceable.
    """
    label = _MIC_TO_TZ_LABEL.get(mic, "")
    try:
        local = utc_dt.astimezone(cal.tz)
    except (AttributeError, ValueError, TypeError) as exc:
        _LOG.warning("could not format local time for mic=%s: %s", mic, exc)
        return ""
    if label:
        return f"{local.strftime('%H:%M')} {label}"
    return local.strftime("%H:%M %Z").strip()


class MarketHours:
    """Compute current trading state per exchange.

    Backed by exchange_calendars. The clock is injectable so tests can
    pin time without waiting for real-world conditions. Calendars are
    cached after first lookup — get_calendar() is the slowest step.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._calendars: dict[str, object] = {}

    def _calendar(self, mic: str):
        cal = self._calendars.get(mic)
        if cal is None:
            import exchange_calendars as ec
            cal = ec.get_calendar(mic)
            self._calendars[mic] = cal
        return cal

    def _previous_session_close(self, cal, today_date) -> datetime | None:
        """UTC close of the most recent session before `today_date`,
        or None if there isn't one in the cached schedule."""
        import pandas as pd

        schedule = cal.schedule
        prior = schedule.loc[:today_date]
        if prior.empty or prior.index[-1] == today_date:
            prior = prior.iloc[:-1] if not prior.empty and prior.index[-1] == today_date else prior
        if prior.empty:
            return None
        return prior.iloc[-1]["close"].to_pydatetime()

    def _next_session_open(self, cal, today_date) -> datetime | None:
        """UTC open of the next session strictly after `today_date` if today
        is itself a session, or starting at `today_date` if today isn't."""
        schedule = cal.schedule
        future = schedule.loc[today_date:]
        if not future.empty and future.index[0] == today_date:
            future = future.iloc[1:]
        if future.empty:
            return None
        return future.iloc[0]["open"].to_pydatetime()

    def status(self, ib_exchange: str) -> MarketStatus | None:
        """Return current MarketStatus for the given IB exchange code,
        or None if the venue isn't mapped (caller skips the row)."""
        mic = mic_for_ib_exchange(ib_exchange)
        if mic is None:
            return None
        cal = self._calendar(mic)
        now = self._clock()
        display = display_name_for_ib_exchange(ib_exchange)

        import pandas as pd

        # Schedule sessions are labeled by their *local* trading-day date,
        # so we must key by the venue's local date — not the UTC date.
        # Indexing by UTC date corrupts venues whose session opens before
        # UTC midnight (e.g. ASX during AEDT opens 23:00 UTC the prior day).
        local_now = now.astimezone(cal.tz)
        date = pd.Timestamp(local_now.date())

        def make_status(state, transition_dt, label, extended_session=None):
            if transition_dt is not None:
                iso = transition_dt.isoformat()
                local = _format_local(transition_dt, cal, mic)
            else:
                iso = ""
                local = ""
            return MarketStatus(
                exchange=display,
                state=state,
                extended_session=extended_session,
                next_transition_local=local,
                next_transition_iso=iso,
                next_transition_label=label,
            )

        try:
            is_session_today = cal.is_session(date)
        except Exception:
            is_session_today = False

        if not is_session_today:
            return make_status(MarketState.HOLIDAY, self._next_session_open(cal, date), "Opens")

        try:
            session = cal.schedule.loc[date]
        except KeyError:
            return make_status(MarketState.HOLIDAY, self._next_session_open(cal, date), "Opens")

        open_ts = session["open"].to_pydatetime()
        close_ts = session["close"].to_pydatetime()
        break_start = session.get("break_start")
        break_end = session.get("break_end")

        if now < open_ts or now >= close_ts:
            # US exchanges check pre/post-market windows before settling on CLOSED.
            if ib_exchange in _US_EXTENDED_EXCHANGES:
                pre_start = open_ts - timedelta(hours=5, minutes=30)
                if pre_start <= now < open_ts:
                    return make_status(
                        MarketState.EXTENDED, open_ts, "Pre-market ends",
                        extended_session="PRE",
                    )
                # Post-market crosses midnight UTC; check both today's and the
                # previous session's close → close+4h windows.
                post_end_today = close_ts + timedelta(hours=4)
                if close_ts <= now < post_end_today:
                    return make_status(
                        MarketState.EXTENDED, post_end_today, "After-hours ends",
                        extended_session="POST",
                    )
                if now < open_ts:
                    prev_close = self._previous_session_close(cal, date)
                    if prev_close is not None and prev_close <= now < prev_close + timedelta(hours=4):
                        return make_status(
                            MarketState.EXTENDED, prev_close + timedelta(hours=4),
                            "After-hours ends", extended_session="POST",
                        )
            # CLOSED: before open → today's open; after close → next session's open.
            if now < open_ts:
                return make_status(MarketState.CLOSED, open_ts, "Opens")
            return make_status(MarketState.CLOSED, self._next_session_open(cal, date), "Opens")

        # HKEX / TSE / SSE midday break. break_start / break_end are NaT
        # on exchanges without lunch, and pd.notna handles both NaT and None.
        if (
            break_start is not None and pd.notna(break_start)
            and break_end is not None and pd.notna(break_end)
            and break_start.to_pydatetime() <= now < break_end.to_pydatetime()
        ):
            return make_status(MarketState.LUNCH, break_end.to_pydatetime(), "Reopens")

        # OPEN — if a lunch break lies ahead today, point at it; otherwise close.
        if (
            break_start is not None and pd.notna(break_start)
            and now < break_start.to_pydatetime()
        ):
            return make_status(MarketState.OPEN, break_start.to_pydatetime(), "Lunch")
        return make_status(MarketState.OPEN, close_ts, "Closes")


class MarketState(str, Enum):
    """The five UI states a market card can show.

    OPEN:     regular trading session in progress
    EXTENDED: US pre/post-market (NYSE/NASDAQ/ARCA/AMEX only)
    LUNCH:    HKEX/TSE/SSE/SZSE midday break
    CLOSED:   outside regular hours on a regular session day
    HOLIDAY:  market closed for the whole day
    """

    OPEN = "OPEN"
    EXTENDED = "EXTENDED"
    LUNCH = "LUNCH"
    CLOSED = "CLOSED"
    HOLIDAY = "HOLIDAY"


@dataclass(frozen=True)
class MarketStatus:
    """Per-exchange trading state plus its next transition.

    `next_transition_iso` is the UTC moment the state will change; the
    template uses it for a client-side countdown so we don't need to
    push every second from the server. `next_transition_local` is the
    same moment formatted in exchange-local time for human display.

    `extended_session` is "PRE" or "POST" when state==EXTENDED, otherwise
    None.
    """

    exchange: str
    state: MarketState
    extended_session: Literal["PRE", "POST"] | None
    next_transition_local: str
    next_transition_iso: str
    next_transition_label: str
