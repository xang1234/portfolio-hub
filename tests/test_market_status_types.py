"""Slice 5 cycle 1: pure types for the market-hours subsystem.

MarketState enumerates the five UI states (OPEN, EXTENDED, LUNCH,
CLOSED, HOLIDAY). MarketStatus bundles per-exchange data the template
needs: which state, when the next transition fires, what label to
show, etc.

Times are surfaced in two forms:
- `next_transition_iso`: UTC ISO string for client-side countdown
- `next_transition_local`: exchange-local human string ("16:00 HKT")
"""

from datetime import datetime, timezone


# Enum shape -----------------------------------------------------------------


def test_market_state_has_five_values():
    from app.core.markets import MarketState

    values = {s.value for s in MarketState}
    assert values == {"OPEN", "EXTENDED", "LUNCH", "CLOSED", "HOLIDAY"}


def test_market_state_extended_is_distinct_from_open():
    """US pre/post market is its own state, not a sub-flavor of OPEN."""
    from app.core.markets import MarketState

    assert MarketState.OPEN is not MarketState.EXTENDED


# MarketStatus dataclass ----------------------------------------------------


def test_market_status_can_be_built_for_open_exchange():
    from app.core.markets import MarketState, MarketStatus

    status = MarketStatus(
        exchange="HKEX",
        state=MarketState.OPEN,
        extended_session=None,
        next_transition_local="16:00 HKT",
        next_transition_iso="2026-05-16T08:00:00+00:00",
        next_transition_label="Closes",
    )

    assert status.exchange == "HKEX"
    assert status.state is MarketState.OPEN
    assert status.extended_session is None


def test_market_status_carries_extended_session_for_us_pre_market():
    """When state=EXTENDED, extended_session must be 'PRE' or 'POST'."""
    from app.core.markets import MarketState, MarketStatus

    status = MarketStatus(
        exchange="NYSE",
        state=MarketState.EXTENDED,
        extended_session="PRE",
        next_transition_local="09:30 ET",
        next_transition_iso="2026-05-16T13:30:00+00:00",
        next_transition_label="Pre-market ends",
    )

    assert status.extended_session == "PRE"


def test_market_status_holiday_has_no_transition_today():
    """HOLIDAY status surfaces with a forward-looking next_transition
    (e.g., next session open). Tested at the type level only."""
    from app.core.markets import MarketState, MarketStatus

    status = MarketStatus(
        exchange="NYSE",
        state=MarketState.HOLIDAY,
        extended_session=None,
        next_transition_local="09:30 ET",
        next_transition_iso="2026-05-26T13:30:00+00:00",
        next_transition_label="Opens",
    )

    assert status.state is MarketState.HOLIDAY
    assert "Opens" in status.next_transition_label


# Frozen / hashable for cheap change detection ------------------------------


def test_market_status_is_frozen_dataclass():
    """Frozen so we can use equality for SSE change-detection without
    accidental mutation."""
    from app.core.markets import MarketState, MarketStatus
    import pytest

    status = MarketStatus(
        exchange="HKEX",
        state=MarketState.OPEN,
        extended_session=None,
        next_transition_local="16:00 HKT",
        next_transition_iso="2026-05-16T08:00:00+00:00",
        next_transition_label="Closes",
    )

    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        status.exchange = "NYSE"
