"""Locks in the prev_close lifecycle across the streaming hot path.

Two concerns the reviewer flagged:

1. **Fallback → live transition.** A position seeded via the unsubscribed-
   market fallback has last_price == previous_close and the
   last_price_is_previous_close flag set. The first live tick must clear
   that flag so intraday_change_pct stops returning None and starts
   reflecting the real intraday move.

2. **Long-lived session staleness.** Without the daily re-seed loop, the
   streaming Positions would carry the previous_close they were seeded
   with on connect — for a session that survives past UTC midnight the
   intraday % would silently drift to "now vs the close from the day
   we connected." _reseed_streaming_previous_closes() reads from the
   per-UTC-day cache so a fresh value lands without restarting the
   gateway.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from app.core.broker import Position
from app.core.live_positions import LivePositions


class _Event:
    def __init__(self): self._cbs = []
    def __iadd__(self, cb): self._cbs.append(cb); return self
    def __isub__(self, cb):
        if cb in self._cbs: self._cbs.remove(cb)
        return self


@dataclass
class _FakeTicker:
    contract: object
    last: float = -1.0
    close: float = -1.0
    bid: float | None = None
    ask: float | None = None
    updateEvent: _Event = field(default_factory=_Event)
    def marketPrice(self): return self.last


def _fallback_position() -> Position:
    """TSEJ stock seeded from the unsubscribed-market historical fallback:
    last_price == previous_close == 1100, flag set."""
    return Position(
        broker="IBKR", account_id="U1", native_key="14016494",
        canonical_symbol="6315.JP", native_symbol="6315",
        exchange="TSEJ", currency="JPY",
        name_en="TOYO ENGINEERING", asset_class="STK",
        quantity=200.0, avg_cost=1000.0, last_price=1100.0,
        market_value_native=220_000.0, market_value_usd=1500.0,
        unrealized_pnl_native=20_000.0, unrealized_pnl_usd=136.36,
        last_price_is_previous_close=True,
        previous_close=1100.0,
    )


def _make_adapter():
    from app.adapters.ibkr import IbkrAdapter
    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: None,
        live_positions=live,
    )
    return adapter, live


# Fallback → live transition --------------------------------------------------


async def test_first_live_tick_after_fallback_clears_flag_and_enables_intraday():
    adapter, live = _make_adapter()
    seeded = _fallback_position()
    # Confirm the precondition the fallback path puts us in:
    assert seeded.intraday_change_pct is None
    assert seeded.last_price_is_previous_close is True

    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 14016494}))
    adapter._streaming[14016494] = (seeded, object(), fake_ticker)
    live.set_position(seeded)

    # A real live tick arrives — Yahoo finally returned, or the user added a
    # data subscription mid-session.
    fake_ticker.last = 1155.0
    adapter._on_ticker_update(fake_ticker)

    after = live.get_all()[0]
    # Live price wins
    assert after.last_price == pytest.approx(1155.0)
    # Fallback flag cleared (replace() override in _on_ticker_update)
    assert after.last_price_is_previous_close is False
    # previous_close persists across the transition — it's the prior session
    # close, untouched by today's ticks
    assert after.previous_close == pytest.approx(1100.0)
    # And the intraday signal turns on: (1155 - 1100) / 1100 ≈ +5.00 %
    assert after.intraday_change_pct == pytest.approx(5.0, abs=1e-6)


async def test_subsequent_ticks_preserve_previous_close():
    """Once live, every tick goes through replace() — previous_close must
    survive untouched so the intraday % keeps tracking against the right
    baseline rather than drifting toward the most recent print."""
    adapter, live = _make_adapter()
    seeded = _fallback_position()
    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 14016494}))
    adapter._streaming[14016494] = (seeded, object(), fake_ticker)
    live.set_position(seeded)

    for tick_price in (1155.0, 1162.0, 1148.5, 1170.0):
        fake_ticker.last = tick_price
        adapter._on_ticker_update(fake_ticker)
        after = live.get_all()[0]
        assert after.previous_close == pytest.approx(1100.0)
        assert after.last_price == pytest.approx(tick_price)


# Daily re-seed for long-lived sessions ---------------------------------------


async def test_reseed_streaming_previous_closes_updates_in_memory_position():
    """The daily refresh loop calls _reseed_streaming_previous_closes() at
    UTC midnight. Drive it directly: stub the cache to return a fresh value,
    confirm the in-memory Position picks it up via live_positions."""
    adapter, live = _make_adapter()
    # Pretend we're already streaming with yesterday's prev_close
    seeded = Position(
        broker="IBKR", account_id="U1", native_key="14016494",
        canonical_symbol="6315.JP", native_symbol="6315",
        exchange="TSEJ", currency="JPY",
        name_en="TOYO ENGINEERING", asset_class="STK",
        quantity=200.0, avg_cost=1000.0, last_price=1180.0,
        market_value_native=236_000.0, market_value_usd=1600.0,
        unrealized_pnl_native=36_000.0, unrealized_pnl_usd=240.0,
        previous_close=1100.0,  # ← yesterday's; stale after UTC rollover
    )
    fake_contract = object()
    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 14016494}))
    adapter._streaming[14016494] = (seeded, fake_contract, fake_ticker)
    live.set_position(seeded)
    # Stub the IB client so _reseed sees a non-None _ib
    adapter._ib = object()

    # Patch _fetch_previous_closes_cached to return the new day's close
    async def _fake_fetch(contracts):
        return {14016494: 1175.0}
    adapter._fetch_previous_closes_cached = _fake_fetch

    await adapter._reseed_streaming_previous_closes()

    after = live.get_all()[0]
    assert after.previous_close == pytest.approx(1175.0)
    # The other fields are preserved by replace()
    assert after.last_price == pytest.approx(1180.0)
    assert after.quantity == pytest.approx(200.0)


async def test_reseed_skips_when_no_streaming_positions():
    """No-op when nothing is streaming — must not blow up on an empty dict."""
    adapter, _live = _make_adapter()
    adapter._ib = object()
    # Should return without calling anything
    await adapter._reseed_streaming_previous_closes()


async def test_reseed_skips_when_ib_is_none():
    """Defensive: a reseed firing during a disconnect window must not crash."""
    adapter, live = _make_adapter()
    seeded = _fallback_position()
    adapter._streaming[14016494] = (seeded, object(), _FakeTicker(contract=object()))
    live.set_position(seeded)
    adapter._ib = None  # mid-disconnect
    # No exception
    await adapter._reseed_streaming_previous_closes()
