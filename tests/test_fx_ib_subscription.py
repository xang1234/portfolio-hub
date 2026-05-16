"""Slice 3 cycle 4: FxService subscribes to IB Forex pairs.

When the adapter knows which currencies are in the portfolio, it tells
FxService to ensure a live subscription per currency. Ticker updates
flow through to the in-memory cache (and on to fx_cache via the
existing set_rate path).

We don't import ib_async here — tests stub out the contract factory
and feed fake tickers. The real wiring happens in cycle 8 when the
adapter connects FxService to its IB instance.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from app.core.fx import FxService


# Fake IB surface ------------------------------------------------------------


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
class FakeForexContract:
    pair: str  # e.g. "HKDUSD"


@dataclass
class FakeTicker:
    contract: FakeForexContract
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    updateEvent: _Event = field(default_factory=_Event)

    def midpoint(self):
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return None

    def marketPrice(self):
        return self.last


class FakeIB:
    def __init__(self) -> None:
        self.req_mkt_data_calls: list[FakeForexContract] = []
        self.cancel_calls: list[FakeForexContract] = []
        self._tickers_by_pair: dict[str, FakeTicker] = {}

    def reqMktData(self, contract, *args, **kwargs):
        ticker = FakeTicker(contract=contract)
        self._tickers_by_pair[contract.pair] = ticker
        self.req_mkt_data_calls.append(contract)
        return ticker

    def cancelMktData(self, contract):
        self.cancel_calls.append(contract)

    def ticker_for(self, pair: str) -> FakeTicker:
        return self._tickers_by_pair[pair]


def _forex(currency: str) -> FakeForexContract:
    return FakeForexContract(pair=f"{currency}USD")


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


# Subscription wiring --------------------------------------------------------


async def test_ensure_subscribed_calls_req_mkt_data_for_each_currency(store):
    fake_ib = FakeIB()
    svc = FxService(store=store, ib=fake_ib, forex_factory=_forex)
    await svc.start()

    await svc.ensure_subscribed({"HKD", "JPY"})

    pairs = {c.pair for c in fake_ib.req_mkt_data_calls}
    assert pairs == {"HKDUSD", "JPYUSD"}


async def test_ensure_subscribed_skips_usd(store):
    fake_ib = FakeIB()
    svc = FxService(store=store, ib=fake_ib, forex_factory=_forex)
    await svc.start()

    await svc.ensure_subscribed({"USD", "HKD"})

    pairs = {c.pair for c in fake_ib.req_mkt_data_calls}
    assert "USDUSD" not in pairs


async def test_ensure_subscribed_is_idempotent(store):
    """Calling twice with the same currency must not double-subscribe."""
    fake_ib = FakeIB()
    svc = FxService(store=store, ib=fake_ib, forex_factory=_forex)
    await svc.start()

    await svc.ensure_subscribed({"HKD"})
    await svc.ensure_subscribed({"HKD"})

    assert len(fake_ib.req_mkt_data_calls) == 1


async def test_ensure_subscribed_rejects_cny(store):
    """Even if a position somehow shows up with currency=CNY, the
    subscription path must fail loudly."""
    fake_ib = FakeIB()
    svc = FxService(store=store, ib=fake_ib, forex_factory=_forex)
    await svc.start()

    with pytest.raises(ValueError):
        await svc.ensure_subscribed({"CNY"})


# Tick → cache propagation ---------------------------------------------------


async def test_ticker_update_propagates_to_get_rate(store):
    """When IB sends a price update, get_rate(currency) should reflect it."""
    fake_ib = FakeIB()
    svc = FxService(store=store, ib=fake_ib, forex_factory=_forex)
    await svc.start()
    await svc.ensure_subscribed({"HKD"})

    # Simulate a tick: midpoint = 0.1283
    ticker = fake_ib.ticker_for("HKDUSD")
    ticker.bid = 0.12825
    ticker.ask = 0.12835
    ticker.updateEvent.emit(ticker)

    rate = await svc.get_rate("HKD")
    assert rate is not None
    assert rate.rate == pytest.approx(0.1283)
    assert rate.source == "IB"


async def test_ticker_update_persists_to_fx_cache(store):
    """Every IB tick should land in fx_cache so a restart can recover it."""
    fake_ib = FakeIB()
    svc = FxService(store=store, ib=fake_ib, forex_factory=_forex)
    await svc.start()
    await svc.ensure_subscribed({"HKD"})

    ticker = fake_ib.ticker_for("HKDUSD")
    ticker.bid = 0.12825
    ticker.ask = 0.12835
    ticker.updateEvent.emit(ticker)
    # Allow the persistence write to schedule (set_rate awaits the store call)
    import asyncio
    await asyncio.sleep(0.02)

    row = await store.get_fx_rate("HKDUSD")
    assert row is not None
    assert row["rate"] == pytest.approx(0.1283)
    assert row["source"] == "IB"


async def test_ticker_with_no_midpoint_does_not_update_cache(store):
    """If IB sends an empty/uninitialized tick (None bid/ask, None last),
    don't overwrite the existing rate with NaN/0."""
    fake_ib = FakeIB()
    svc = FxService(store=store, ib=fake_ib, forex_factory=_forex)
    await svc.start()
    await svc.ensure_subscribed({"HKD"})
    # Seed a known rate
    ticker = fake_ib.ticker_for("HKDUSD")
    ticker.bid = 0.12825
    ticker.ask = 0.12835
    ticker.updateEvent.emit(ticker)

    # Now an empty tick
    ticker.bid = None
    ticker.ask = None
    ticker.last = None
    ticker.updateEvent.emit(ticker)

    rate = await svc.get_rate("HKD")
    assert rate.rate == pytest.approx(0.1283)  # unchanged


# No-IB safety net -----------------------------------------------------------


async def test_ensure_subscribed_is_noop_without_ib(store):
    """If FxService was constructed without an IB instance (e.g., tests
    that only seed rates manually), ensure_subscribed should be a quiet
    no-op rather than crashing."""
    svc = FxService(store=store)  # no ib
    await svc.start()

    await svc.ensure_subscribed({"HKD"})  # no raise

    assert await svc.get_rate("HKD") is None
