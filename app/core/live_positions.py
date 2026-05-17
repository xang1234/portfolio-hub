"""LivePositions — observable in-memory store for the current set of positions.

The store is the boundary between the producer side (the broker adapter, which
pushes Position objects as IB ticks arrive) and the consumer side (the SSE
handler, which reads the snapshot and waits for change notifications).

Level-triggered change notification: many rapid set_position() calls collapse
into a single wake-up for the consumer, which then sees the final state and
emits one downstream event. This is the natural pre-throttle for the slice 4
500ms-min-interval requirement.
"""

import asyncio
import hashlib
import time
from dataclasses import replace
from typing import AsyncIterator, Callable

from app.core.broker import Position


PositionKey = tuple[str, str, str]  # (broker, account_id, canonical_symbol)


def _key(p: Position) -> PositionKey:
    return (p.broker, p.account_id, p.canonical_symbol)


class LivePositions:
    def __init__(self) -> None:
        self._positions: dict[PositionKey, Position] = {}
        self._changed = asyncio.Event()

    def get_all(self) -> list[Position]:
        return list(self._positions.values())

    def set_position(self, position: Position) -> None:
        """Insert or update a position. No-op (no change event) if the new
        position is field-by-field identical to the existing one for that key."""
        key = _key(position)
        existing = self._positions.get(key)
        if existing is not None and existing == position:
            return
        self._positions[key] = position
        self._changed.set()

    def replace_all(self, positions: list[Position]) -> None:
        new_map = {_key(p): p for p in positions}
        if new_map == self._positions:
            return
        self._positions = new_map
        self._changed.set()

    async def wait_for_change(self) -> None:
        """Block until at least one change has happened since the last call.

        Auto-clears the underlying event so callers loop forever cleanly:

            while True:
                await live.wait_for_change()
                snapshot = live.get_all()
                ...
        """
        await self._changed.wait()
        self._changed.clear()


# Hash function ----------------------------------------------------------------


def hash_position(p: Position) -> str:
    """Compute a short, stable hash over a Position's display-relevant fields.

    Used by the SSE handler to compute row-level deltas: only rows whose hash
    differs from the per-client last-seen hash get included in the next push.

    Fields *not* in the hash because they don't affect display: native_key,
    avg_cost (until slice 8's detail card surfaces it). The row key
    (broker, account_id, canonical_symbol) is also omitted — change-detection
    is per-row, so the key is implicit.
    """
    payload = (
        f"{p.name_en}|"
        f"{p.currency}|"
        f"{p.quantity}|"
        f"{p.last_price}|"
        f"{p.market_value_native}|"
        f"{p.market_value_usd}|"
        f"{p.unrealized_pnl_native}|"
        f"{p.unrealized_pnl_usd}|"
        f"{int(p.last_price_is_stale)}"
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# Event generator --------------------------------------------------------------


Renderer = Callable[[list[Position]], str]


async def stream_events(
    live: LivePositions,
    render_rows: Renderer,
    *,
    min_interval: float = 0.5,
    heartbeat_interval: float = 15.0,
) -> AsyncIterator[dict[str, str]]:
    """Async generator that yields SSE events for one client.

    Events:
      - 'snapshot' once at connect time, carrying render_rows(all_positions)
      - 'positions' for each delta — render_rows(only changed positions)
      - 'heartbeat' when idle for heartbeat_interval seconds
    """
    # 1. Initial snapshot
    positions = live.get_all()
    last_hashes: dict[PositionKey, str] = {_key(p): hash_position(p) for p in positions}
    yield {"event": "snapshot", "data": render_rows(positions)}

    last_emit = time.monotonic()

    while True:
        try:
            await asyncio.wait_for(live.wait_for_change(), timeout=heartbeat_interval)
            had_change = True
        except asyncio.TimeoutError:
            had_change = False

        if not had_change:
            yield {"event": "heartbeat", "data": ""}
            continue

        # 500ms throttle: ensure min_interval since last real emit. While we
        # sleep, more set_position() calls may pile up — when we wake we'll
        # see the latest aggregated state via get_all(). If post-sleep state
        # has no actual hash changes (e.g., the change was reverted), we emit
        # nothing and loop back to wait for the next real change.
        elapsed = time.monotonic() - last_emit
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)

        current = live.get_all()
        new_hashes = {_key(p): hash_position(p) for p in current}

        # Hash check is the server-side filter: skip emit when nothing
        # actually changed (e.g., set_position called with identical data).
        # Wire format always carries the full snapshot — simplest HTMX swap.
        if new_hashes != last_hashes:
            yield {"event": "positions", "data": render_rows(current)}
            last_emit = time.monotonic()
            last_hashes = new_hashes
