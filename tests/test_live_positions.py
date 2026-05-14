"""Tests for the LivePositions in-memory observable store.

The store decouples the broker adapter (which pushes Position updates as IB
ticks arrive) from the SSE handler (which pulls the current snapshot and
waits for change notifications). It must:

  - Hold the current set of positions, keyed by (broker, account_id, canonical_symbol)
  - Notify waiters when anything changes
  - NOT notify when set_position() is called with identical data (no-op)
  - Support bulk replace (used when the initial reqPositions snapshot lands)
"""

import asyncio

import pytest

from app.core.broker import Position


def _new_position(**overrides) -> Position:
    base = dict(
        broker="IBKR",
        account_id="U7575980",
        native_key="76792991",
        canonical_symbol="700.HK",
        native_symbol="700",
        exchange="SEHK",
        currency="HKD",
        name_en="TENCENT HOLDINGS LTD",
        asset_class="STK",
        quantity=100.0,
        avg_cost=400.0,
        last_price=420.0,
        market_value_native=42000.0,
        market_value_usd=0.0,
        unrealized_pnl_native=2000.0,
        unrealized_pnl_usd=0.0,
    )
    base.update(overrides)
    return Position(**base)


# get_all() --------------------------------------------------------------------


def test_get_all_returns_empty_list_when_no_positions_set():
    from app.core.live_positions import LivePositions

    live = LivePositions()

    assert live.get_all() == []


def test_get_all_returns_position_after_set_position():
    from app.core.live_positions import LivePositions

    live = LivePositions()
    p = _new_position()

    live.set_position(p)

    assert live.get_all() == [p]


def test_set_position_with_same_key_replaces_previous():
    from app.core.live_positions import LivePositions

    live = LivePositions()
    live.set_position(_new_position(last_price=400.0))
    live.set_position(_new_position(last_price=420.0))  # same key, new price

    rows = live.get_all()
    assert len(rows) == 1
    assert rows[0].last_price == 420.0


def test_different_keys_are_independent_rows():
    from app.core.live_positions import LivePositions

    live = LivePositions()
    aapl = _new_position(
        native_key="2",
        canonical_symbol="AAPL.US",
        native_symbol="AAPL",
        exchange="NASDAQ",
        currency="USD",
        name_en="APPLE INC",
    )
    tencent = _new_position()
    live.set_position(aapl)
    live.set_position(tencent)

    assert len(live.get_all()) == 2


# replace_all() ----------------------------------------------------------------


def test_replace_all_swaps_entire_set():
    from app.core.live_positions import LivePositions

    live = LivePositions()
    live.set_position(_new_position(native_key="1", canonical_symbol="OLD.HK"))

    new_position = _new_position(native_key="2", canonical_symbol="NEW.HK")
    live.replace_all([new_position])

    rows = live.get_all()
    assert len(rows) == 1
    assert rows[0].canonical_symbol == "NEW.HK"


def test_replace_all_with_empty_list_clears_all_positions():
    from app.core.live_positions import LivePositions

    live = LivePositions()
    live.set_position(_new_position())

    live.replace_all([])

    assert live.get_all() == []


# wait_for_change() ------------------------------------------------------------


async def test_wait_for_change_unblocks_after_set_position():
    from app.core.live_positions import LivePositions

    live = LivePositions()

    async def setter():
        await asyncio.sleep(0.01)
        live.set_position(_new_position())

    asyncio.create_task(setter())

    # Should unblock within a reasonable time
    await asyncio.wait_for(live.wait_for_change(), timeout=1.0)


async def test_wait_for_change_does_not_unblock_when_set_is_a_no_op():
    """Setting the exact same Position again must NOT fire a change."""
    from app.core.live_positions import LivePositions

    live = LivePositions()
    p = _new_position()
    live.set_position(p)
    # Consume the initial change event
    await live.wait_for_change()

    # Setting an identical position should NOT notify
    live.set_position(_new_position())  # same fields as p

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(live.wait_for_change(), timeout=0.1)


async def test_wait_for_change_unblocks_after_replace_all():
    from app.core.live_positions import LivePositions

    live = LivePositions()

    async def setter():
        await asyncio.sleep(0.01)
        live.replace_all([_new_position()])

    asyncio.create_task(setter())

    await asyncio.wait_for(live.wait_for_change(), timeout=1.0)


async def test_multiple_set_positions_collapse_into_one_notification():
    """Level-triggered semantics: bursty ticks collapse into a single wake-up
    for the SSE consumer. This is the natural pre-throttle for the 500ms
    min-interval downstream."""
    from app.core.live_positions import LivePositions

    live = LivePositions()

    # Five rapid changes before the consumer has a chance to wake
    for price in (400, 405, 410, 415, 420):
        live.set_position(_new_position(last_price=float(price)))

    # Consumer wakes once and sees the final state
    await asyncio.wait_for(live.wait_for_change(), timeout=0.5)
    assert live.get_all()[0].last_price == 420.0

    # No further notifications pending
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(live.wait_for_change(), timeout=0.1)
