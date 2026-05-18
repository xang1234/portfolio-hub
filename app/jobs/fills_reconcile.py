"""EOD fills reconciliation — backstop for execDetailsEvent gaps.

If the dashboard was disconnected when a fill happened, the live event
stream missed it. IB keeps the execution server-side for ~24-48h, so
calling reqExecutions periodically lets us pick up missed fills without
relying on the live stream alone. INSERT OR IGNORE on (broker, execution_id)
makes the job safe to re-run as often as the operator wants.

Designed to be small, side-effect-isolated, and testable without a real IB:
the adapter exposes `_req_executions()` which the production adapter
implements as a thin wrapper around `IB.reqExecutionsAsync(filter=...)`,
while tests can supply a fake adapter that returns a fixed list.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Awaitable, Callable

from app.core.fills import build_fill_row
from app.db.store import Store


_LOG = logging.getLogger(__name__)
_DEFAULT_HHMM = time(23, 0)


async def reconcile_fills(adapter, store: Store) -> int:
    """Pull recent executions from the broker and INSERT OR IGNORE each one.

    Returns the count of NEW rows inserted (i.e. the number of fills that
    the live execDetailsEvent stream missed). Zero is the healthy steady-
    state value when the live stream is working.

    Never raises: a broker-side error logs and returns 0 so the daily
    scheduler can retry on its next tick.
    """
    try:
        fills = await adapter._req_executions()
    except Exception as exc:
        _LOG.warning("reqExecutions failed during reconcile: %s", exc)
        return 0

    fx = getattr(adapter, "_fx_service", None)
    new_count = 0
    for fill in fills:
        row = build_fill_row(broker=adapter.name, fill=fill, fx_service=fx)
        if row is None:
            continue
        try:
            inserted = await store.insert_fill(**row)
        except Exception as exc:
            _LOG.warning(
                "insert_fill failed during reconcile for exec %s: %s",
                row.get("execution_id", "?"), exc,
            )
            continue
        if inserted:
            new_count += 1
    if new_count:
        _LOG.info("Reconcile inserted %d previously-missed fill(s)", new_count)
    return new_count


# ---- daily scheduler -------------------------------------------------------


def parse_hhmm(value: str) -> time:
    """Parse 'HH:MM' from RECONCILE_AT_HHMM env, falling back to 23:00 on
    typos. Operators tweaking config shouldn't be able to crash the lifespan.
    """
    if not value:
        return _DEFAULT_HHMM
    parts = value.strip().split(":")
    if len(parts) != 2:
        return _DEFAULT_HHMM
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return _DEFAULT_HHMM
    if not (0 <= h < 24 and 0 <= m < 60):
        return _DEFAULT_HHMM
    return time(h, m)


def next_fire_at(now: datetime, target: time, *, tz: tzinfo) -> datetime:
    """Return the next datetime at `target` HH:MM in `tz`, strictly after `now`.

    If today's instance hasn't passed yet we schedule today; if it's already
    passed (or we're exactly on it), we schedule tomorrow. The strict-after
    semantic prevents double-firing when the loop wakes up at exactly the
    target time.
    """
    local_now = now.astimezone(tz)
    today_local = datetime.combine(local_now.date(), target, tzinfo=tz)
    if today_local > local_now:
        return today_local.astimezone(now.tzinfo or timezone.utc)
    tomorrow_local = today_local + timedelta(days=1)
    return tomorrow_local.astimezone(now.tzinfo or timezone.utc)


async def scheduled_reconcile_loop(
    adapter,
    store: Store,
    *,
    at: time = _DEFAULT_HHMM,
    tz: tzinfo = timezone.utc,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    reconcile: Callable[..., Awaitable[int]] = reconcile_fills,
) -> None:
    """Sleep until the next configured local time, run reconcile, repeat.

    `sleep`, `now`, `reconcile` are injected so tests can drive the loop
    without real wall-clock time. A failing reconcile is swallowed (logged)
    so one bad day doesn't stop the daily schedule.
    """
    while True:
        current = now()
        wake_at = next_fire_at(current, at, tz=tz)
        delay = max(0.0, (wake_at - current).total_seconds())
        _LOG.info("fills reconcile scheduled for %s (in %.0fs)", wake_at.isoformat(), delay)
        await sleep(delay)
        try:
            await reconcile(adapter, store)
        except Exception as exc:
            _LOG.warning("scheduled reconcile_fills raised: %s", exc)
        # Belt-and-suspenders: next_fire_at's strict-after semantic already
        # prevents same-day re-fire on the boundary, but a 1s buffer guards
        # against microsecond races on systems with low clock resolution.
        await sleep(1.0)
