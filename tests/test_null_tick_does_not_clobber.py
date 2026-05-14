"""Streaming guard: a null tick from IB (zero / -1 / NaN) must NOT
overwrite an existing price.

This bites the non-subscribed exchanges (TSEJ, SBF, IBIS, SFB...) where
reqMktData succeeds but every subsequent tick is empty. Without the
guard, the Yahoo previous-close we just installed gets clobbered to 0
within milliseconds and the MV column drops back to —.

The semantics: a 0/null tick is "I don't know," not "the price is zero."
Real ticks (any positive value) are accepted and clear the
last_price_is_previous_close flag because we now have live data.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from app.core.broker import Position


class _Event:
    def __init__(self):
        self._callbacks = []
    def __iadd__(self, cb):
        self._callbacks.append(cb)
        return self
    def __isub__(self, cb):
        if cb in self._callbacks:
            self._callbacks.remove(cb)
        return self
    def emit(self, *args, **kwargs):
        for cb in list(self._callbacks):
            cb(*args, **kwargs)


@dataclass
class _FakeTicker:
    contract: object
    last: float = -1.0  # IB's "no data" sentinel
    close: float = -1.0
    bid: float | None = None
    ask: float | None = None
    updateEvent: _Event = field(default_factory=_Event)

    def marketPrice(self):
        return self.last


def _seed_position(*, last_price: float, is_prev_close: bool) -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key="14016494",
        canonical_symbol="6315.JP", native_symbol="6315",
        exchange="TSEJ", currency="JPY",
        name_en="TOYO ENGINEERING CORP", asset_class="STK",
        quantity=200.0, avg_cost=1000.0, last_price=last_price,
        market_value_native=200.0 * last_price,
        market_value_usd=0.0,
        unrealized_pnl_native=(last_price - 1000.0) * 200.0,
        unrealized_pnl_usd=0.0,
        last_price_is_previous_close=is_prev_close,
    )


def _make_adapter_with_streaming_seed():
    """Build an adapter with a pre-seeded streaming entry — bypassing the
    real connect() path so we can drive _on_ticker_update directly."""
    from app.adapters.ibkr import IbkrAdapter
    from app.core.live_positions import LivePositions

    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: None,
        live_positions=live,
    )
    adapter._live_positions = live
    return adapter, live


# Null tick must NOT overwrite -----------------------------------------------


def test_null_tick_does_not_clobber_existing_price():
    """A ticker with last=-1.0 / close=-1.0 / marketPrice=None should leave
    the cached price intact — it's "no data," not "zero."""
    adapter, live = _make_adapter_with_streaming_seed()

    seeded = _seed_position(last_price=4040.0, is_prev_close=True)
    fake_contract = object()
    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 14016494}))
    adapter._streaming[14016494] = (seeded, fake_contract, fake_ticker)
    live.set_position(seeded)

    # Now an empty tick (the IB Error 354 case)
    fake_ticker.last = -1.0
    fake_ticker.close = -1.0
    adapter._on_ticker_update(fake_ticker)

    after = live.get_all()[0]
    assert after.last_price == pytest.approx(4040.0)
    assert after.last_price_is_previous_close is True


def test_zero_tick_does_not_clobber_existing_price():
    """A ticker with last=0 should also be ignored. Zero isn't a valid
    price for any tradable instrument."""
    adapter, live = _make_adapter_with_streaming_seed()

    seeded = _seed_position(last_price=4040.0, is_prev_close=True)
    fake_contract = object()
    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 14016494}))
    adapter._streaming[14016494] = (seeded, fake_contract, fake_ticker)
    live.set_position(seeded)

    fake_ticker.last = 0.0
    fake_ticker.close = 0.0
    adapter._on_ticker_update(fake_ticker)

    after = live.get_all()[0]
    assert after.last_price == pytest.approx(4040.0)


# Real tick replaces the prev-close placeholder -----------------------------


def test_real_tick_overrides_prev_close_and_clears_the_flag():
    """When a positive tick finally arrives, it should win over the
    Yahoo prev-close AND clear the last_price_is_previous_close flag
    (it's no longer EOD-only)."""
    adapter, live = _make_adapter_with_streaming_seed()

    seeded = _seed_position(last_price=4040.0, is_prev_close=True)
    fake_contract = object()
    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 14016494}))
    adapter._streaming[14016494] = (seeded, fake_contract, fake_ticker)
    live.set_position(seeded)

    fake_ticker.last = 4055.0
    adapter._on_ticker_update(fake_ticker)

    after = live.get_all()[0]
    assert after.last_price == pytest.approx(4055.0)
    assert after.last_price_is_previous_close is False


def test_same_price_tick_is_no_op():
    """Ticker fires with same price → don't re-emit a change."""
    adapter, live = _make_adapter_with_streaming_seed()

    seeded = _seed_position(last_price=4040.0, is_prev_close=False)
    fake_contract = object()
    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 14016494}))
    adapter._streaming[14016494] = (seeded, fake_contract, fake_ticker)
    live.set_position(seeded)

    fake_ticker.last = 4040.0
    adapter._on_ticker_update(fake_ticker)

    after = live.get_all()[0]
    assert after.last_price == pytest.approx(4040.0)
