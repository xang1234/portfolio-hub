"""Equity-snapshot scheduler — per-market-close NLV captures.

For each STK-exchange held in the current portfolio, the loop sleeps
until that exchange's next regular-session close (half-day-aware,
holiday-aware via exchange_calendars), then captures one
equity_snapshots row per linked account tagged
`f"{exchange}_CLOSE"`.

The whole account's NLV is captured at every close — not just that
exchange's positions — giving sub-daily resolution to the future
equity-curve UI regardless of which exchange triggered.

All wall-clock and I/O dependencies (`now`, `sleep`, the capture
helper) are injectable so tests can drive the loop without real
calendar time.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from app.core.equity import build_equity_snapshot_row
from app.core.markets import MarketHours
from app.db.store import Store


_LOG = logging.getLogger(__name__)

_DEFAULT_EMPTY_RECHECK = 5 * 60  # seconds


async def _capture_snapshot(
    adapter, store: Store, ib_exchange: str, snapshot_at: datetime,
) -> int:
    """Snapshot every linked account once, tagged with f"{ib_exchange}_CLOSE".
    Returns count of NEW rows inserted (collisions silently dropped via
    Store.insert_equity_snapshot's INSERT OR IGNORE).
    """
    try:
        summaries = await adapter.get_account_summary()
    except Exception as exc:
        _LOG.warning(
            "get_account_summary failed at %s_CLOSE: %s", ib_exchange, exc,
        )
        return 0

    session = f"{ib_exchange}_CLOSE"
    inserted = 0
    for s in summaries:
        try:
            row = build_equity_snapshot_row(
                account=s, snapshot_at=snapshot_at, snapshot_session=session,
            )
            if await store.insert_equity_snapshot(**row):
                inserted += 1
                # Privacy: log session + account only, NOT NLV/cash amounts.
                _LOG.info(
                    "Captured equity snapshot session=%s broker=%s account=%s",
                    session, s.broker, s.account_id,
                )
        except Exception as exc:
            _LOG.warning(
                "insert_equity_snapshot failed for %s/%s at %s: %s",
                s.broker, s.account_id, session, exc,
            )
    return inserted


def _next_close_across_held_exchanges(
    positions, hours: MarketHours,
) -> tuple[str, datetime] | None:
    """Of all STK exchanges currently held, return (ib_exchange,
    soonest_close_utc) — or None if no STK holdings."""
    exchanges = {p.exchange for p in positions if p.asset_class == "STK" and p.exchange}
    soonest: tuple[str, datetime] | None = None
    for ib_ex in exchanges:
        close = hours.next_close_at(ib_ex)
        if close is None:
            continue
        if soonest is None or close < soonest[1]:
            soonest = (ib_ex, close)
    return soonest


async def scheduled_snapshot_loop(
    adapter,
    store: Store,
    hours: MarketHours,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    capture: Callable[..., Awaitable[int]] = _capture_snapshot,
    empty_recheck_interval: float = _DEFAULT_EMPTY_RECHECK,
) -> None:
    """Long-lived loop: sleep until soonest held-exchange close, capture,
    repeat. Handles empty-portfolio / unmapped-exchange edge cases without
    crashing, and survives a single capture failure.
    """
    while True:
        try:
            positions = await adapter.get_positions()
        except Exception as exc:
            _LOG.warning("get_positions failed in snapshot loop: %s", exc)
            positions = []

        target = _next_close_across_held_exchanges(positions, hours)
        if target is None:
            # No STK holdings OR no mapped venue. Re-check later instead of
            # spinning or crashing — operator may add positions any time.
            await sleep(empty_recheck_interval)
            continue

        ib_ex, close_at = target
        current = now()
        delay = max(0.0, (close_at - current).total_seconds())
        _LOG.info(
            "Next equity snapshot scheduled for %s_CLOSE at %s (in %.0fs)",
            ib_ex, close_at.isoformat(), delay,
        )
        await sleep(delay)

        try:
            await capture(adapter, store, ib_ex, close_at)
        except Exception as exc:
            # Defensive — capture() already handles its own exceptions, but
            # any future refactor that lets one bubble up shouldn't kill the
            # daily schedule.
            _LOG.warning("snapshot capture raised at %s_CLOSE: %s", ib_ex, exc)

        # Belt-and-suspenders: tick past the close moment so the next
        # iteration's next_close_at returns the NEXT session, not this one.
        await sleep(1.0)
