"""Slice 11 cycle 6: daily reconcile scheduler.

The scheduler is a small asyncio task that:
  1. Computes the next fire-at instant from `RECONCILE_AT_HHMM` env var
     (default "23:00") in the configured local timezone.
  2. Sleeps until that instant.
  3. Calls reconcile_fills(adapter, store).
  4. Loops.

Tests use injectable now() + sleep callables so we don't actually wait.
"""

import asyncio
from datetime import datetime, time, timedelta, timezone


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# Computing the next fire-at -------------------------------------------------


def test_next_fire_at_today_if_target_in_future():
    """If 23:00 hasn't happened today yet, the next run is today at 23:00."""
    from app.jobs.fills_reconcile import next_fire_at

    now = _utc("2026-05-17T15:30:00+00:00")
    target = time(23, 0)
    nxt = next_fire_at(now, target, tz=timezone.utc)
    assert nxt == _utc("2026-05-17T23:00:00+00:00")


def test_next_fire_at_tomorrow_if_target_already_passed_today():
    """If 23:00 has already passed today, the next run is tomorrow at 23:00."""
    from app.jobs.fills_reconcile import next_fire_at

    now = _utc("2026-05-17T23:30:00+00:00")
    target = time(23, 0)
    nxt = next_fire_at(now, target, tz=timezone.utc)
    assert nxt == _utc("2026-05-18T23:00:00+00:00")


def test_next_fire_at_exactly_at_target_jumps_to_tomorrow():
    """At exactly the target time, schedule the NEXT one (tomorrow) — avoids
    double-running if we wake up exactly on the boundary."""
    from app.jobs.fills_reconcile import next_fire_at

    now = _utc("2026-05-17T23:00:00+00:00")
    target = time(23, 0)
    nxt = next_fire_at(now, target, tz=timezone.utc)
    assert nxt == _utc("2026-05-18T23:00:00+00:00")


# Parsing RECONCILE_AT_HHMM --------------------------------------------------


def test_parse_hhmm_valid():
    from app.jobs.fills_reconcile import parse_hhmm
    assert parse_hhmm("23:00") == time(23, 0)
    assert parse_hhmm("00:00") == time(0, 0)
    assert parse_hhmm("09:30") == time(9, 30)


def test_parse_hhmm_invalid_falls_back_to_default():
    """Operator typos shouldn't crash the lifespan — fall back to 23:00."""
    from app.jobs.fills_reconcile import parse_hhmm
    assert parse_hhmm("not a time") == time(23, 0)
    assert parse_hhmm("25:99") == time(23, 0)
    assert parse_hhmm("") == time(23, 0)


# Loop behaviour -------------------------------------------------------------


class _FakeAdapter:
    name = "IBKR"
    def __init__(self): self._fx_service = None; self.call_count = 0
    async def _req_executions(self):
        self.call_count += 1
        return []  # no fills returned


class _FakeStore:
    """Counts insert_fill calls — but reconcile_fills doesn't call us when
    there are no fills, so we just need to exist for the signature."""


async def test_scheduler_calls_reconcile_then_sleeps_to_next_day():
    """One iteration: sleeps the configured duration, then runs reconcile."""
    from app.jobs.fills_reconcile import scheduled_reconcile_loop

    adapter = _FakeAdapter()
    store = _FakeStore()

    sleeps: list[float] = []
    reconcile_calls = {"n": 0}

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        # Trigger cancellation after the first iteration completes
        if len(sleeps) >= 2:
            raise asyncio.CancelledError()

    async def fake_reconcile(adp, st):
        reconcile_calls["n"] += 1
        assert adp is adapter
        assert st is store
        return 0

    # Run the loop until our fake sleep cancels it.
    now = _utc("2026-05-17T15:30:00+00:00")
    def now_fn(): return now

    try:
        await scheduled_reconcile_loop(
            adapter, store,
            at=time(23, 0), tz=timezone.utc,
            sleep=fake_sleep, now=now_fn, reconcile=fake_reconcile,
        )
    except asyncio.CancelledError:
        pass

    # First sleep was for ~7.5h to reach 23:00 from 15:30.
    assert len(sleeps) >= 1
    expected_seconds = (23 - 15) * 3600 - 30 * 60  # 7h 30m
    assert sleeps[0] == expected_seconds, (
        f"first sleep should be {expected_seconds}s (until 23:00), got {sleeps[0]}"
    )
    # Reconcile fired once between the two sleeps.
    assert reconcile_calls["n"] == 1


async def test_scheduler_survives_reconcile_raising():
    """A bad reconcile run shouldn't kill the daily loop."""
    from app.jobs.fills_reconcile import scheduled_reconcile_loop

    sleeps: list[float] = []
    calls = {"n": 0}

    # Each loop iteration produces 2 sleeps (until-fire-at + 1s tick after
    # reconcile). Two iterations = 4 sleeps; cancel on the 5th to prove the
    # second reconcile fired (i.e. the loop survived the first raise).
    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 5:
            raise asyncio.CancelledError()

    async def fake_reconcile(adp, st):
        calls["n"] += 1
        raise RuntimeError("broker hiccup")

    now = _utc("2026-05-17T15:30:00+00:00")
    try:
        await scheduled_reconcile_loop(
            _FakeAdapter(), _FakeStore(),
            at=time(23, 0), tz=timezone.utc,
            sleep=fake_sleep, now=lambda: now, reconcile=fake_reconcile,
        )
    except asyncio.CancelledError:
        pass

    # Reconcile ran (and raised) twice; loop kept going.
    assert calls["n"] == 2
