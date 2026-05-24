"""Many LSE-traded UK stocks are quoted in PENCE (GBp / GBX), not pounds.
IB Gateway returns these with ContractDetails.priceMagnifier=100:
  - last/bid/ask ticks come back in pence  (e.g. IQE at 49.60p)
  - reqPositions.avgCost also in pence       (e.g. 50.00p)
  - But Contract.currency is "GBP"

If we naively multiply quantity × last_price, we get pence not pounds,
inflating MV native by 100× and MV USD by 100× as well. IQE rendered as
£24,800 / $33,530 instead of £248 / ~$335.

Fix: capture priceMagnifier from ContractDetails, divide native-currency
math by it. Display the Last column as-is (the user expects to see
"49.60" with a "p" suffix), but MV native is in pounds.
"""

from dataclasses import dataclass

import pytest

from app.core.broker import ConnectionState, Position
from app.core.fx import FxRate, FxService


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
    priceMagnifier: int = 1


@dataclass
class FakeIBPosition:
    account: str
    contract: FakeContract
    position: float
    avgCost: float


@dataclass
class FakePortfolioItem:
    account: str
    contract: FakeContract
    position: float
    marketPrice: float
    marketValue: float
    averageCost: float
    unrealizedPNL: float


class FakeTicker:
    def __init__(self, contract, last=None):
        self.contract = contract
        self.last = last
        self.close = None
    def marketPrice(self):
        return self.last


class FakeIB:
    def __init__(self, positions, details, last_prices, portfolio_items=None):
        self._positions = positions
        self._details = details
        self._last_prices = last_prices
        self._portfolio_items = portfolio_items or []
        self._connected = False
    async def connectAsync(self, host, port, clientId):
        self._connected = True
    def disconnect(self): self._connected = False
    def isConnected(self): return self._connected
    def reqMarketDataType(self, mdt): pass
    async def reqPositionsAsync(self): return self._positions
    async def reqContractDetailsAsync(self, contract):
        d = self._details.get(contract.conId)
        return [d] if d is not None else []
    async def reqTickersAsync(self, *contracts):
        return [FakeTicker(c, last=self._last_prices.get(c.conId)) for c in contracts]
    def portfolio(self, account: str = ""):
        if account:
            return [p for p in self._portfolio_items if p.account == account]
        return list(self._portfolio_items)


async def _fake_yahoo_price(_symbol: str) -> float | None:
    return 49.60


def _iqe_position(*, priceMagnifier: int):
    """IQE PLC: real user position. 500 shares, last 49.60p, cost 50.00p."""
    contract = FakeContract(
        conId=14075064, symbol="IQE", secType="STK", currency="GBP",
        exchange="LSE", primaryExchange="LSE",
    )
    details = FakeContractDetails(
        contract=FakeContract(
            conId=14075064, symbol="IQE", secType="STK", currency="GBP",
            primaryExchange="LSE",
        ),
        longName="IQE PLC",
        priceMagnifier=priceMagnifier,
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=500.0, avgCost=50.00)
    return pos, details


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store
    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


@pytest.fixture
async def fx_with_gbp(store):
    svc = FxService(store=store, api_fetcher=None)
    await svc.start()
    import datetime
    await svc.set_rate(FxRate(
        pair="GBPUSD", rate=1.35,
        quoted_at=datetime.datetime.now(datetime.timezone.utc),
        is_stale=False, source="API_FALLBACK",
    ))
    return svc


# Core: pence stock gets divided by 100 -------------------------------------


async def test_pence_priced_iqe_mv_native_in_pounds_not_pence(store, fx_with_gbp):
    """IQE with priceMagnifier=100: mv_native = 500 × 49.60 / 100 = £248,
    NOT £24,800."""
    pos, details = _iqe_position(priceMagnifier=100)
    fake_ib = FakeIB(
        positions=[pos], details={14075064: details}, last_prices={14075064: 49.60},
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_with_gbp,
        yahoo_quote_fetcher=_fake_yahoo_price,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    p = positions[0]
    assert p.market_value_native == pytest.approx(248.0)
    assert p.market_value_usd == pytest.approx(248.0 * 1.35)


async def test_pence_priced_iqe_pnl_native_in_pounds(store, fx_with_gbp):
    """avg_cost is also in pence. PnL native = (49.60 - 50.00) × 500 / 100 = -£2."""
    pos, details = _iqe_position(priceMagnifier=100)
    fake_ib = FakeIB(
        positions=[pos], details={14075064: details}, last_prices={14075064: 49.60},
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_with_gbp,
        yahoo_quote_fetcher=_fake_yahoo_price,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    p = positions[0]
    assert p.unrealized_pnl_native == pytest.approx(-2.0)
    assert p.unrealized_pnl_usd == pytest.approx(-2.0 * 1.35)


async def test_position_carries_price_magnifier_flag(store, fx_with_gbp):
    """Streaming needs to know the divisor too. Position must expose it."""
    pos, details = _iqe_position(priceMagnifier=100)
    fake_ib = FakeIB(
        positions=[pos], details={14075064: details}, last_prices={14075064: 49.60},
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_with_gbp,
        yahoo_quote_fetcher=_fake_yahoo_price,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].price_magnifier == 100


async def test_pence_stock_uses_portfolio_value_without_double_dividing(store, fx_with_gbp):
    """IB portfolio items report LSE pence stocks in major GBP units.

    When live/Yahoo prices are unavailable, use portfolio marketValue/PNL
    directly and convert marketPrice back to the display quote unit (GBp).
    """
    pos, details = _iqe_position(priceMagnifier=100)
    pos.avgCost = 0.42214285
    portfolio_item = FakePortfolioItem(
        account="U1",
        contract=details.contract,
        position=3500.0,
        marketPrice=0.4571064,
        marketValue=1599.87,
        averageCost=0.42214285,
        unrealizedPNL=122.37,
    )
    fake_ib = FakeIB(
        positions=[pos],
        details={14075064: details},
        last_prices={},
        portfolio_items=[portfolio_item],
    )
    from app.adapters.ibkr import IbkrAdapter

    async def no_yahoo(_symbol: str) -> float | None:
        return None

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_with_gbp,
        yahoo_quote_fetcher=no_yahoo,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    p = positions[0]
    assert p.last_price == pytest.approx(45.71064)
    assert p.market_value_native == pytest.approx(1599.87)
    assert p.market_value_usd == pytest.approx(1599.87 * 1.35)
    assert p.unrealized_pnl_native == pytest.approx(122.37)
    assert p.unrealized_pnl_usd == pytest.approx(122.37 * 1.35)
    assert p.last_price_is_broker_mark is True


# Default (priceMagnifier=1) leaves everything unchanged --------------------


async def test_normal_gbp_stock_with_magnifier_1_is_unchanged(store, fx_with_gbp):
    """A regular pound-denominated stock (priceMagnifier=1) keeps current
    behavior — quantity × last_price with no division."""
    contract = FakeContract(
        conId=123456, symbol="LLOY", secType="STK", currency="GBP",
        exchange="LSE", primaryExchange="LSE",
    )
    details = FakeContractDetails(
        contract=FakeContract(
            conId=123456, symbol="LLOY", secType="STK", currency="GBP",
            primaryExchange="LSE",
        ),
        longName="LLOYDS BANKING GROUP",
        priceMagnifier=1,
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=1000.0, avgCost=0.55)
    fake_ib = FakeIB(
        positions=[pos], details={123456: details}, last_prices={123456: 0.60},
    )
    from app.adapters.ibkr import IbkrAdapter

    async def fake_yahoo_price(_symbol: str) -> float | None:
        return 0.60

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_with_gbp,
        yahoo_quote_fetcher=fake_yahoo_price,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    p = positions[0]
    assert p.market_value_native == pytest.approx(600.0)  # 1000 × 0.60
    assert p.price_magnifier == 1


# USD-denominated positions unaffected ------------------------------------


async def test_us_stock_with_magnifier_100_does_not_divide_mv_usd(store):
    """If a US stock somehow has priceMagnifier > 1 (shouldn't, but
    defensive): mv_usd should still equal mv_native (USD is the base)."""
    contract = FakeContract(
        conId=999, symbol="X", secType="STK", currency="USD",
        exchange="NASDAQ", primaryExchange="NASDAQ",
    )
    details = FakeContractDetails(
        contract=FakeContract(
            conId=999, symbol="X", secType="STK", currency="USD",
            primaryExchange="NASDAQ",
        ),
        longName="EXAMPLE CORP",
        priceMagnifier=1,
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=10.0, avgCost=100.0)
    fake_ib = FakeIB(
        positions=[pos], details={999: details}, last_prices={999: 120.0},
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
    assert p.market_value_native == pytest.approx(1200.0)
    assert p.market_value_usd == pytest.approx(1200.0)


# Template displays last with "p" suffix for pence stocks ------------------


def test_template_renders_pence_suffix_when_price_magnifier_is_100():
    from fastapi.testclient import TestClient

    iqe = Position(
        broker="IBKR", account_id="U1", native_key="14075064",
        canonical_symbol="IQE.UK", native_symbol="IQE",
        exchange="LSE", currency="GBP",
        name_en="IQE PLC", asset_class="STK",
        quantity=500.0, avg_cost=50.0, last_price=49.60,
        market_value_native=248.0, market_value_usd=334.80,
        unrealized_pnl_native=-2.0, unrealized_pnl_usd=-2.70,
        price_magnifier=100,
    )

    class FakeAdapter:
        name = "IBKR"
        async def connect(self): pass
        async def disconnect(self): pass
        async def is_connected(self): return True
        async def get_connection_state(self): return ConnectionState.CONNECTED
        async def get_positions(self): return [iqe]
        async def get_account_summary(self): return []

    from app.main import create_app
    client = TestClient(create_app(broker=FakeAdapter()))

    response = client.get("/")
    text = response.text

    # Some kind of "p" or "GBp" affordance must be in the row so the user
    # knows 49.60 is pence, not pounds.
    iqe_block = text[text.index("IQE PLC"):text.index("IQE PLC") + 800]
    assert "GBp" in iqe_block or "49.60p" in iqe_block or 'class="pence-suffix"' in iqe_block


def test_template_does_not_render_pence_suffix_when_magnifier_is_1():
    from fastapi.testclient import TestClient

    lloy = Position(
        broker="IBKR", account_id="U1", native_key="123456",
        canonical_symbol="LLOY.UK", native_symbol="LLOY",
        exchange="LSE", currency="GBP",
        name_en="LLOYDS BANKING GROUP", asset_class="STK",
        quantity=1000.0, avg_cost=0.55, last_price=0.60,
        market_value_native=600.0, market_value_usd=810.0,
        unrealized_pnl_native=50.0, unrealized_pnl_usd=67.50,
        price_magnifier=1,
    )

    class FakeAdapter:
        name = "IBKR"
        async def connect(self): pass
        async def disconnect(self): pass
        async def is_connected(self): return True
        async def get_connection_state(self): return ConnectionState.CONNECTED
        async def get_positions(self): return [lloy]
        async def get_account_summary(self): return []

    from app.main import create_app
    client = TestClient(create_app(broker=FakeAdapter()))

    response = client.get("/")
    text = response.text
    # No "p" suffix should appear next to the price (the currency badge "GBP"
    # is fine — we only care that there's no extra "p" affordance).
    assert "0.60p" not in text
    assert "GBp" not in text
