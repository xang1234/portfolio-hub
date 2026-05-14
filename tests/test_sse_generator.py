"""Tests for the SSE event generator that streams holdings updates.

The generator is the "loop" that runs once per SSE client. It:
  - Emits a 'snapshot' event with all rows on first iteration
  - Then emits 'positions' delta events containing only rows whose hash changed
  - Throttles to one event per `min_interval` seconds
  - Emits a 'heartbeat' event when idle for `heartbeat_interval` seconds

We inject a renderer fn so tests work with plain strings (no template plumbing).
min_interval and heartbeat_interval are also injectable so tests stay fast.
"""

import asyncio
from dataclasses import dataclass

import pytest

from app.core.broker import Position
from app.core.live_positions import LivePositions


def _new_position(**overrides):
    base = dict(
        broker="IBKR",
        account_id="U7575980",
        native_key="76792991",
        canonical_symbol="700.HK",
        native_symbol="700",
        exchange="SEHK",
        currency="HKD",
        name_en="TENCENT",
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


def render_rows_to_csv(positions: list[Position]) -> str:
    """Trivial renderer for tests: comma-separated canonical_symbol:last_price."""
    return ",".join(f"{p.canonical_symbol}:{p.last_price}" for p in positions)


async def _drain(generator, *, max_events: int, timeout: float):
    """Collect events from an async generator until max_events or timeout."""
    out = []
    while len(out) < max_events:
        try:
            event = await asyncio.wait_for(generator.__anext__(), timeout=timeout)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
        out.append(event)
    return out


# Initial snapshot -------------------------------------------------------------


async def test_first_event_is_snapshot_with_all_current_positions():
    from app.core.live_positions import stream_events

    live = LivePositions()
    live.set_position(_new_position(canonical_symbol="700.HK"))
    live.set_position(_new_position(canonical_symbol="AAPL.US", native_key="2", native_symbol="AAPL"))

    gen = stream_events(live, render_rows_to_csv, min_interval=0.01, heartbeat_interval=10.0)

    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    assert first["event"] == "snapshot"
    assert "700.HK:420.0" in first["data"]
    assert "AAPL.US:420.0" in first["data"]
    await gen.aclose()


async def test_snapshot_event_emits_even_when_no_positions():
    """Empty portfolios should still get a snapshot event so clients have a
    deterministic 'I'm connected and there's nothing to show' state."""
    from app.core.live_positions import stream_events

    live = LivePositions()
    gen = stream_events(live, render_rows_to_csv, min_interval=0.01, heartbeat_interval=10.0)

    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    assert first["event"] == "snapshot"
    assert first["data"] == ""
    await gen.aclose()


# Delta semantics --------------------------------------------------------------


async def test_set_position_triggers_positions_delta_event():
    from app.core.live_positions import stream_events

    live = LivePositions()
    live.set_position(_new_position(last_price=420.0))

    gen = stream_events(live, render_rows_to_csv, min_interval=0.01, heartbeat_interval=10.0)
    # Consume initial snapshot
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    # Now mutate
    live.set_position(_new_position(last_price=421.0, market_value_native=42100.0))

    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    assert second["event"] == "positions"
    assert "700.HK:421.0" in second["data"]
    await gen.aclose()


async def test_positions_event_contains_all_current_rows_after_one_changes():
    """The wire format carries the full snapshot so HTMX can simply replace
    the tbody innerHTML. The server-side delta detection is a *filter* (we
    skip emits when nothing has changed), not a partial render."""
    from app.core.live_positions import stream_events

    live = LivePositions()
    p1 = _new_position(canonical_symbol="700.HK", last_price=420.0)
    p2 = _new_position(canonical_symbol="AAPL.US", native_key="2", native_symbol="AAPL", last_price=180.0)
    live.set_position(p1)
    live.set_position(p2)

    gen = stream_events(live, render_rows_to_csv, min_interval=0.01, heartbeat_interval=10.0)
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)  # snapshot

    # Only mutate AAPL
    live.set_position(_new_position(canonical_symbol="AAPL.US", native_key="2", native_symbol="AAPL", last_price=181.0))

    second = await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    assert second["event"] == "positions"
    # New AAPL price visible AND tencent still in the wire data
    assert "AAPL.US:181.0" in second["data"]
    assert "700.HK" in second["data"]
    await gen.aclose()


async def test_no_event_emitted_when_set_position_is_a_noop():
    """Set with identical data — generator should not emit a positions event."""
    from app.core.live_positions import stream_events

    live = LivePositions()
    p = _new_position()
    live.set_position(p)

    gen = stream_events(live, render_rows_to_csv, min_interval=0.01, heartbeat_interval=10.0)
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)  # snapshot

    # No real change — same values
    live.set_position(_new_position())

    # Expect a timeout (no event) within a short window
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(gen.__anext__(), timeout=0.1)
    await gen.aclose()


# Throttle ---------------------------------------------------------------------


async def test_throttle_collapses_burst_into_one_event_per_min_interval():
    """10 rapid set_positions within 50ms should produce ~1 event when
    min_interval=100ms (one event for the burst, possibly one more for
    the final state)."""
    from app.core.live_positions import stream_events

    live = LivePositions()
    live.set_position(_new_position(last_price=400.0))

    gen = stream_events(live, render_rows_to_csv, min_interval=0.1, heartbeat_interval=10.0)
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)  # snapshot

    # Burst: 10 changes in 50ms
    async def burst():
        for i in range(10):
            live.set_position(_new_position(last_price=400.0 + i, market_value_native=40000.0 + i))
            await asyncio.sleep(0.005)

    asyncio.create_task(burst())
    # Drain with a per-event timeout > min_interval so the throttled emit
    # has time to fire. The generator only resumes its work once __anext__
    # is awaited, so we don't pre-sleep here.
    events = await _drain(gen, max_events=10, timeout=0.3)

    # Should be at most 2 events (some intermediate + final), well below 10
    assert 1 <= len(events) <= 3, f"expected throttling to collapse 10 ticks; got {len(events)} events"
    # And the most recent event should reflect the final state
    assert "400.0" not in events[-1]["data"] or "409" in events[-1]["data"]
    await gen.aclose()


# Heartbeat --------------------------------------------------------------------


async def test_heartbeat_event_emits_when_idle_past_heartbeat_interval():
    from app.core.live_positions import stream_events

    live = LivePositions()
    gen = stream_events(live, render_rows_to_csv, min_interval=0.01, heartbeat_interval=0.1)
    await asyncio.wait_for(gen.__anext__(), timeout=1.0)  # snapshot

    # Don't mutate. Wait past heartbeat interval.
    heartbeat = await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    assert heartbeat["event"] == "heartbeat"
    await gen.aclose()
