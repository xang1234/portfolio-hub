"""Slice 3 cycle 8: FxService wired into IbkrAdapter.get_positions().

After this cycle, every Position has:
  - market_value_usd computed via FxService.convert()
  - unrealized_pnl_usd computed via FxService.convert()
  - fx_is_stale / fx_is_fallback flags reflecting the FxRate used
  - fx_unavailable=True when no rate (e.g. CNH with no IB tick) so the
    template can render — instead of $0.00

USD-denominated positions: market_value_usd = market_value_native, no
FX metadata flags set.

The adapter also calls fx_service.ensure_subscribed() to register
Forex subscriptions for any currencies that appear in the portfolio.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.core.broker import Position
from app.core.fx import FxRate, FxService


# Test doubles ----------------------------------------------------------------


@dataclass
class FakeContract:
    conId: int
    symbol: str
    secType: str
    currency: str
    exchange: str = "SMART"
    primaryExchange: str = ""


@dataclass
class FakeContractDetails:
    contract: FakeContract
    longName: str


@dataclass
class FakeIBPosition:
    account: str
    contract: FakeContract
    position: float
    avgCost: float


class FakeTicker:
    def __init__(self, contract, last=None):
        self.contract = contract
        self.last = last
        self.close = None

    def marketPrice(self):
        return self.last


class FakeIB:
    def __init__(self, positions, details, last_prices):
        self._positions = positions
        self._details = details
        self._last_prices = last_prices
        self._connected = False
        self.req_mkt_data_calls = []

    async def connectAsync(self, host, port, clientId):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isConnected(self):
        return self._connected

    def reqMarketDataType(self, mdt):
        pass

    async def reqPositionsAsync(self):
        return self._positions

    async def reqContractDetailsAsync(self, contract):
        d = self._details.get(contract.conId)
        return [d] if d is not None else []

    async def reqTickersAsync(self, *contracts):
        return [FakeTicker(c, last=self._last_prices.get(c.conId)) for c in contracts]

    def reqMktData(self, contract, *args, **kwargs):
        # Used by FxService for Forex subscriptions
        self.req_mkt_data_calls.append(contract)
        ticker = FakeTicker(contract)
        ticker.bid = None
        ticker.ask = None
        # Hook events
        from tests.test_fx_ib_subscription import _Event
        ticker.updateEvent = _Event()
        return ticker


def _tencent_hkd():
    contract = FakeContract(conId=76792991, symbol="700", secType="STK", currency="HKD")
    details = FakeContractDetails(
        contract=FakeContract(
            conId=76792991, symbol="700", secType="STK", currency="HKD",
            primaryExchange="SEHK",
        ),
        longName="TENCENT HOLDINGS LTD",
    )
    pos = FakeIBPosition(account="U7575980", contract=contract, position=100.0, avgCost=400.0)
    return pos, details


def _apple_usd():
    contract = FakeContract(conId=265598, symbol="AAPL", secType="STK", currency="USD")
    details = FakeContractDetails(
        contract=FakeContract(
            conId=265598, symbol="AAPL", secType="STK", currency="USD",
            primaryExchange="NASDAQ",
        ),
        longName="APPLE INC",
    )
    pos = FakeIBPosition(account="U7575980", contract=contract, position=10.0, avgCost=150.0)
    return pos, details


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _at(hh: int, mm: int) -> datetime:
    return datetime(2026, 5, 13, hh, mm, tzinfo=timezone.utc)


# USD positions: no FX needed -----------------------------------------------


async def test_usd_position_has_usd_equal_to_native(store):
    """AAPL at $180 × 10 shares = $1800 native and USD."""
    pos, details = _apple_usd()
    fake_ib = FakeIB(
        positions=[pos], details={265598: details}, last_prices={265598: 180.0}
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_svc,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert len(positions) == 1
    p = positions[0]
    assert p.currency == "USD"
    assert p.market_value_usd == pytest.approx(1800.0)
    assert p.market_value_native == pytest.approx(1800.0)
    # Unrealized pnl: (180 - 150) * 10 = 300
    assert p.unrealized_pnl_usd == pytest.approx(300.0)


async def test_usd_position_has_no_fx_metadata_flags(store):
    pos, details = _apple_usd()
    fake_ib = FakeIB(
        positions=[pos], details={265598: details}, last_prices={265598: 180.0}
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_svc,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    p = positions[0]
    assert p.fx_is_stale is False
    assert p.fx_is_fallback is False
    assert p.fx_unavailable is False


# Non-USD position with known FX rate ---------------------------------------


async def test_hkd_position_converts_to_usd_via_fx_service(store):
    """100 shares Tencent @ HKD 420 = HKD 42,000. At 0.1283 USD/HKD = USD 5,388.60."""
    pos, details = _tencent_hkd()
    fake_ib = FakeIB(
        positions=[pos], details={76792991: details}, last_prices={76792991: 420.0}
    )
    fx_svc = FxService(store=store, api_fetcher=None, clock=lambda: _at(14, 0))
    await fx_svc.start()
    await fx_svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=_at(14, 0),
        is_stale=False, source="IB",
    ))

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_svc,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    p = positions[0]
    assert p.currency == "HKD"
    assert p.market_value_native == pytest.approx(42000.0)
    assert p.market_value_usd == pytest.approx(42000.0 * 0.1283)
    # PnL: (420 - 400) * 100 = HKD 2000; * 0.1283 = USD 256.60
    assert p.unrealized_pnl_native == pytest.approx(2000.0)
    assert p.unrealized_pnl_usd == pytest.approx(2000.0 * 0.1283)


# Non-USD position with no FX rate available -------------------------------


async def test_position_with_no_fx_rate_has_unavailable_flag(store):
    """CNH-denominated A-share with no IB tick and API doesn't supply CNH:
    market_value_usd should be 0.0 with fx_unavailable=True so the
    template renders — instead of $0.00."""
    contract = FakeContract(conId=999, symbol="9988", secType="STK", currency="CNH")
    details = FakeContractDetails(
        contract=FakeContract(
            conId=999, symbol="9988", secType="STK", currency="CNH",
            primaryExchange="SEHK",
        ),
        longName="ALIBABA GROUP HOLDING LTD CNH",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=100.0, avgCost=80.0)
    fake_ib = FakeIB(
        positions=[pos], details={999: details}, last_prices={999: 90.0}
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    # Deliberately do NOT set a CNH rate

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_svc,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    p = positions[0]
    assert p.fx_unavailable is True
    assert p.market_value_usd == 0.0
    assert p.unrealized_pnl_usd == 0.0


# Stale FX rate propagates to Position --------------------------------------


async def test_stale_fx_rate_sets_position_fx_is_stale(store):
    """Mid-market HKD quote 5 min old → stale → row should show ⚠️."""
    from datetime import timedelta

    pos, details = _tencent_hkd()
    fake_ib = FakeIB(
        positions=[pos], details={76792991: details}, last_prices={76792991: 420.0}
    )
    now = _at(14, 0)
    fx_svc = FxService(store=store, api_fetcher=None, clock=lambda: now)
    await fx_svc.start()
    await fx_svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=now - timedelta(minutes=5),
        is_stale=False, source="IB",
    ))

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_svc,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    p = positions[0]
    assert p.fx_is_stale is True
    assert p.fx_is_fallback is False  # source was IB even if stale


# API_FALLBACK source propagates ---------------------------------------------


async def test_api_fallback_source_sets_position_fx_is_fallback(store):
    pos, details = _tencent_hkd()
    fake_ib = FakeIB(
        positions=[pos], details={76792991: details}, last_prices={76792991: 420.0}
    )
    now = _at(14, 0)
    fx_svc = FxService(store=store, api_fetcher=None, clock=lambda: now)
    await fx_svc.start()
    await fx_svc.set_rate(FxRate(
        pair="HKDUSD", rate=0.1283, quoted_at=now,
        is_stale=False, source="API_FALLBACK",
    ))

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_svc,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    p = positions[0]
    assert p.fx_is_fallback is True
    assert p.fx_is_stale is False


# Subscription registration -------------------------------------------------


async def test_get_positions_subscribes_fx_for_each_non_usd_currency(store):
    """Adapter calls ensure_subscribed() so the FX feed warms up."""
    pos, details = _tencent_hkd()
    fake_ib = FakeIB(
        positions=[pos], details={76792991: details}, last_prices={76792991: 420.0}
    )
    fx_svc = FxService(
        store=store, ib=fake_ib, api_fetcher=None,
        forex_factory=lambda c: FakeContract(
            conId=hash(c) & 0xFFFF, symbol=c, secType="CASH", currency=c,
        ),
    )
    await fx_svc.start()

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_svc,
    )
    await adapter.connect()
    await adapter.get_positions()

    # One reqMktData should have been called for HKD
    subscribed_currencies = {c.currency for c in fake_ib.req_mkt_data_calls}
    assert "HKD" in subscribed_currencies
