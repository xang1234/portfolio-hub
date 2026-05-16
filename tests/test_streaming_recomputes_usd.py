"""When a tick updates last_price, it must also recompute market_value_usd
and unrealized_pnl_usd using the current FX rate. Otherwise the USD
columns get stuck at whatever they were when the position was first
seeded — which is 0 for any row built before its FX rate arrived.

This race bit a real user portfolio: EUR positions seeded with
last_price=0 (Yahoo fallback hadn't completed yet) → mv_native=0 →
mv_usd=0. Later ticks set last_price > 0 but mv_usd stayed at 0,
rendering — even though we had a perfectly good FX rate.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from app.core.broker import Position
from app.core.fx import FxRate, FxService


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


@dataclass
class _FakeTicker:
    contract: object
    last: float = -1.0
    close: float = -1.0
    bid: float | None = None
    ask: float | None = None
    updateEvent: _Event = field(default_factory=_Event)
    def marketPrice(self):
        return self.last


def _euro_position(*, mv_usd: float = 0.0, fx_is_fallback: bool = True) -> Position:
    """Simulates a position that was seeded before its FX rate was known —
    mv_usd starts at 0 even though the EUR rate is available."""
    return Position(
        broker="IBKR", account_id="U1", native_key="46469310",
        canonical_symbol="M7U.DE", native_symbol="M7U",
        exchange="IBIS", currency="EUR",
        name_en="NYNOMIC AG", asset_class="STK",
        quantity=250.0, avg_cost=20.0, last_price=0.0,
        market_value_native=0.0,
        market_value_usd=mv_usd,
        unrealized_pnl_native=0.0,
        unrealized_pnl_usd=0.0,
        fx_is_fallback=fx_is_fallback,
        fx_unavailable=False,
    )


@pytest.fixture
async def fx_with_eur(tmp_path):
    from app.db.store import Store

    store = Store(tmp_path / "test.db")
    await store.init_schema()
    svc = FxService(store=store, api_fetcher=None)
    await svc.start()
    await svc.set_rate(FxRate(
        pair="EURUSD", rate=1.17,
        quoted_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc),
        is_stale=False, source="API_FALLBACK",
    ))
    yield svc
    await store.close()


def _make_adapter_with_fx(fx_svc):
    from app.adapters.ibkr import IbkrAdapter
    from app.core.live_positions import LivePositions

    live = LivePositions()
    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: None,
        live_positions=live,
        fx_service=fx_svc,
    )
    return adapter, live


# Real tick should recompute mv_usd ------------------------------------------


async def test_ticker_update_recomputes_mv_usd_using_current_fx_rate(fx_with_eur):
    """The race we hit: seed had mv_usd=0 because last_price was 0 at build
    time. Later tick sets last_price=19.50; mv_usd must update too."""
    adapter, live = _make_adapter_with_fx(fx_with_eur)

    seeded = _euro_position()  # mv_usd = 0
    fake_contract = object()
    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 46469310}))
    adapter._streaming[46469310] = (seeded, fake_contract, fake_ticker)
    live.set_position(seeded)

    # Tick arrives with a real price (Yahoo fallback resolved, or IB
    # delayed-frozen kicked in)
    fake_ticker.last = 19.50
    adapter._on_ticker_update(fake_ticker)

    after = live.get_all()[0]
    assert after.last_price == pytest.approx(19.50)
    assert after.market_value_native == pytest.approx(250 * 19.50)
    # NEW: mv_usd recomputed from current EUR rate (1.17)
    assert after.market_value_usd == pytest.approx(250 * 19.50 * 1.17)


async def test_ticker_update_recomputes_pnl_usd(fx_with_eur):
    """Same as above but for unrealized P&L."""
    adapter, live = _make_adapter_with_fx(fx_with_eur)

    seeded = _euro_position()  # avg_cost=20, qty=250
    fake_contract = object()
    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 46469310}))
    adapter._streaming[46469310] = (seeded, fake_contract, fake_ticker)
    live.set_position(seeded)

    fake_ticker.last = 19.50
    adapter._on_ticker_update(fake_ticker)

    after = live.get_all()[0]
    # P&L native: (19.50 - 20.00) * 250 = -125
    # P&L USD: -125 * 1.17 = -146.25
    assert after.unrealized_pnl_native == pytest.approx(-125.0)
    assert after.unrealized_pnl_usd == pytest.approx(-146.25)


# fx_unavailable should be cleared if rate is now available ----------------


async def test_ticker_update_clears_fx_unavailable_when_rate_available(fx_with_eur):
    """A position seeded with fx_unavailable=True (rate wasn't loaded yet)
    should have the flag cleared once a rate is in the service and a tick
    triggers recompute."""
    adapter, live = _make_adapter_with_fx(fx_with_eur)

    seeded = Position(
        broker="IBKR", account_id="U1", native_key="46469310",
        canonical_symbol="M7U.DE", native_symbol="M7U",
        exchange="IBIS", currency="EUR",
        name_en="NYNOMIC AG", asset_class="STK",
        quantity=250.0, avg_cost=20.0, last_price=0.0,
        market_value_native=0.0, market_value_usd=0.0,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
        fx_unavailable=True,
        fx_is_fallback=False,
    )
    fake_contract = object()
    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 46469310}))
    adapter._streaming[46469310] = (seeded, fake_contract, fake_ticker)
    live.set_position(seeded)

    fake_ticker.last = 19.50
    adapter._on_ticker_update(fake_ticker)

    after = live.get_all()[0]
    assert after.fx_unavailable is False
    assert after.fx_is_fallback is True  # API_FALLBACK rate was used
    assert after.market_value_usd == pytest.approx(250 * 19.50 * 1.17)


# USD-denominated position shouldn't touch FX -----------------------------


async def test_ticker_update_for_usd_position_does_not_call_fx_service(fx_with_eur):
    """USD positions: mv_usd = mv_native, no FX needed."""
    adapter, live = _make_adapter_with_fx(fx_with_eur)

    seeded = Position(
        broker="IBKR", account_id="U1", native_key="265598",
        canonical_symbol="AAPL.US", native_symbol="AAPL",
        exchange="NASDAQ", currency="USD",
        name_en="APPLE INC", asset_class="STK",
        quantity=10.0, avg_cost=150.0, last_price=150.0,
        market_value_native=1500.0, market_value_usd=1500.0,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
    )
    fake_contract = object()
    fake_ticker = _FakeTicker(contract=type("C", (), {"conId": 265598}))
    adapter._streaming[265598] = (seeded, fake_contract, fake_ticker)
    live.set_position(seeded)

    fake_ticker.last = 180.0
    adapter._on_ticker_update(fake_ticker)

    after = live.get_all()[0]
    assert after.market_value_usd == pytest.approx(1800.0)
    assert after.unrealized_pnl_usd == pytest.approx(300.0)
