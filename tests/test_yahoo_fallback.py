"""When even IB's historical data is gated (no market data permissions on
some exchanges), fall back to Yahoo Finance's free chart endpoint for
end-of-day close. That covers TSEJ, SBF, IBIS, SFB and most other
international exchanges where IB charges for data.

Yahoo uses its own symbol convention — for Tokyo it's `6315.T`, for
Paris Euronext it's `ALRIB.PA`, for Xetra it's `M7U.DE`, etc. The
yahoo_symbol_for function maps from our canonical IB exchange code +
native symbol to the Yahoo format.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

import pytest


# Symbol mapping (pure function) --------------------------------------------


def test_yahoo_symbol_for_tokyo():
    from app.core.yahoo_quotes import yahoo_symbol_for

    assert yahoo_symbol_for("6315", "TSEJ") == "6315.T"


def test_yahoo_symbol_for_paris_euronext():
    from app.core.yahoo_quotes import yahoo_symbol_for

    assert yahoo_symbol_for("ALRIB", "SBF") == "ALRIB.PA"


def test_yahoo_symbol_for_xetra():
    from app.core.yahoo_quotes import yahoo_symbol_for

    assert yahoo_symbol_for("M7U", "IBIS") == "M7U.DE"


def test_yahoo_symbol_for_stockholm():
    from app.core.yahoo_quotes import yahoo_symbol_for

    assert yahoo_symbol_for("SIVE", "SFB") == "SIVE.ST"


def test_yahoo_symbol_for_hong_kong():
    """700.HK (Tencent) maps to 0700.HK on Yahoo (zero-padded to 4 digits)."""
    from app.core.yahoo_quotes import yahoo_symbol_for

    assert yahoo_symbol_for("700", "SEHK") == "0700.HK"


def test_yahoo_symbol_for_us_nasdaq_has_no_suffix():
    from app.core.yahoo_quotes import yahoo_symbol_for

    assert yahoo_symbol_for("AAPL", "NASDAQ") == "AAPL"


def test_yahoo_symbol_for_us_nyse_has_no_suffix():
    from app.core.yahoo_quotes import yahoo_symbol_for

    assert yahoo_symbol_for("JPM", "NYSE") == "JPM"


def test_yahoo_symbol_for_korea():
    from app.core.yahoo_quotes import yahoo_symbol_for

    assert yahoo_symbol_for("005930", "KRX") == "005930.KS"


def test_yahoo_symbol_for_taiwan():
    from app.core.yahoo_quotes import yahoo_symbol_for

    assert yahoo_symbol_for("2330", "TWSE") == "2330.TW"


def test_yahoo_symbol_for_unmapped_exchange_returns_none():
    """If we don't have a Yahoo suffix for this exchange, skip rather than
    sending Yahoo a bogus symbol."""
    from app.core.yahoo_quotes import yahoo_symbol_for

    assert yahoo_symbol_for("FOO", "PRIVATE_EXCHANGE_XYZ") is None


# Yahoo fetcher behavior ----------------------------------------------------


def _yahoo_chart_response(closes: list[float]) -> dict:
    """Mimic the relevant slice of v8/finance/chart response."""
    return {
        "chart": {
            "result": [{
                "indicators": {
                    "quote": [{
                        "close": closes,
                    }],
                },
            }],
        },
    }


async def test_extracts_most_recent_non_null_close():
    from app.core.yahoo_quotes import extract_latest_close

    payload = _yahoo_chart_response([1000.0, 1050.0, None])
    assert extract_latest_close(payload) == pytest.approx(1050.0)


async def test_handles_all_null_closes():
    from app.core.yahoo_quotes import extract_latest_close

    payload = _yahoo_chart_response([None, None])
    assert extract_latest_close(payload) is None


async def test_handles_malformed_payload():
    from app.core.yahoo_quotes import extract_latest_close

    assert extract_latest_close({}) is None
    assert extract_latest_close({"chart": {}}) is None
    assert extract_latest_close({"chart": {"result": []}}) is None


# Adapter wiring ------------------------------------------------------------


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


@dataclass
class FakePortfolioItem:
    account: str
    contract: FakeContract
    position: float
    marketPrice: float
    marketValue: float
    averageCost: float
    unrealizedPNL: float


class _NullTicker:
    def __init__(self, contract):
        self.contract = contract
        self.last = -1.0
        self.close = -1.0
    def marketPrice(self):
        return None


class FakeIB:
    """IB instance where reqHistoricalDataAsync always raises (no permissions)."""
    def __init__(self, positions, details, portfolio_items=None):
        self._positions = positions
        self._details = details
        self._portfolio_items = portfolio_items or []
        self._connected = False
        self.historical_calls = []
        self.ticker_calls = []
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
        self.ticker_calls.append([c.conId for c in contracts])
        return [_NullTicker(c) for c in contracts]
    def portfolio(self, account: str = ""):
        if account:
            return [p for p in self._portfolio_items if p.account == account]
        return list(self._portfolio_items)
    async def reqHistoricalDataAsync(self, contract, **kwargs):
        # IB Error 162 simulated: no market data permissions
        self.historical_calls.append(contract.conId)
        raise ValueError("No market data permissions for TSEJ STK")


def _tse_position():
    # Real IB positions surface the venue as `exchange` (not primaryExchange) —
    # see gateway logs showing Stock(symbol='6315', exchange='TSEJ', ...).
    contract = FakeContract(
        conId=14016494, symbol="6315", secType="STK", currency="JPY",
        exchange="TSEJ",
    )
    details = FakeContractDetails(
        contract=FakeContract(
            conId=14016494, symbol="6315", secType="STK", currency="JPY",
            primaryExchange="TSEJ",
        ),
        longName="TOYO ENGINEERING CORP",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=200.0, avgCost=1000.0)
    return pos, details


def _smart_routed_tse_position():
    contract = FakeContract(
        conId=14016494, symbol="6315", secType="STK", currency="JPY",
        exchange="SMART",
    )
    details = FakeContractDetails(
        contract=FakeContract(
            conId=14016494, symbol="6315", secType="STK", currency="JPY",
            primaryExchange="TSEJ",
        ),
        longName="TOYO ENGINEERING CORP",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=200.0, avgCost=1000.0)
    return pos, details


def _twse_position():
    contract = FakeContract(
        conId=38768770, symbol="2308", secType="STK", currency="TWD",
        exchange="TWSE",
    )
    details = FakeContractDetails(
        contract=FakeContract(
            conId=38768770, symbol="2308", secType="STK", currency="TWD",
            primaryExchange="TWSE",
        ),
        longName="DELTA ELECTRONICS INC",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=100.0, avgCost=300.0)
    return pos, details


def _krx_position():
    contract = FakeContract(
        conId=852105392, symbol="322310", secType="STK", currency="KRW",
        exchange="KRX",
    )
    details = FakeContractDetails(
        contract=FakeContract(
            conId=852105392, symbol="322310", secType="STK", currency="KRW",
            primaryExchange="KRX",
        ),
        longName="ARES CO LTD",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=300.0, avgCost=35240.0)
    return pos, details


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


async def test_yahoo_fallback_kicks_in_when_ib_historical_fails(store):
    """TSEJ stock: ticker returns null and Yahoo succeeds."""
    pos, details = _tse_position()
    fake_ib = FakeIB(positions=[pos], details={14016494: details})

    yahoo_calls = []
    async def fake_yahoo_fetcher(symbol: str) -> float | None:
        yahoo_calls.append(symbol)
        return 1234.5  # Yahoo returns a price

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=fake_yahoo_fetcher,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].last_price == pytest.approx(1234.5)
    assert positions[0].last_price_is_previous_close is True
    # Yahoo was called with the Tokyo symbol
    assert "6315.T" in yahoo_calls


async def test_yahoo_fallback_bypasses_slow_ib_historical_for_mapped_exchange(store):
    """When Yahoo knows the exchange, don't block startup on IB historical
    requests that commonly time out for unsubscribed international markets."""
    pos, details = _tse_position()

    class SlowHistoricalIB(FakeIB):
        async def reqHistoricalDataAsync(self, contract, **kwargs):
            self.historical_calls.append(contract.conId)
            await asyncio.sleep(10)
            return []

    fake_ib = SlowHistoricalIB(positions=[pos], details={14016494: details})

    yahoo_calls = []

    async def fake_yahoo_fetcher(symbol: str) -> float | None:
        yahoo_calls.append(symbol)
        return 1234.5

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=fake_yahoo_fetcher,
    )
    await adapter.connect()
    positions = await asyncio.wait_for(adapter.get_positions(), timeout=0.2)

    assert positions[0].last_price == pytest.approx(1234.5)
    assert yahoo_calls == ["6315.T"]
    assert fake_ib.historical_calls == []


async def test_gated_exchange_skips_live_snapshot_request(store):
    """Known permission-gated venues should not call IB live snapshot data."""
    pos, details = _tse_position()
    fake_ib = FakeIB(positions=[pos], details={14016494: details})

    async def fake_yahoo_fetcher(symbol: str) -> float | None:
        return 1234.5

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=fake_yahoo_fetcher,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].last_price == pytest.approx(1234.5)
    assert fake_ib.ticker_calls == []


async def test_taiwan_exchange_skips_live_snapshot_request(store):
    pos, details = _twse_position()
    fake_ib = FakeIB(positions=[pos], details={38768770: details})

    async def fake_yahoo_fetcher(symbol: str) -> float | None:
        return 380.0

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=fake_yahoo_fetcher,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].last_price == pytest.approx(380.0)
    assert fake_ib.ticker_calls == []


async def test_details_exchange_controls_gated_historical_skip(store):
    """SMART-routed positions must use ContractDetails.primaryExchange for
    permission-gated skip decisions."""
    pos, details = _smart_routed_tse_position()
    fake_ib = FakeIB(positions=[pos], details={14016494: details})

    async def fake_yahoo_fetcher(symbol: str) -> float | None:
        return None

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=fake_yahoo_fetcher,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].last_price == 0.0
    assert fake_ib.ticker_calls == []
    assert fake_ib.historical_calls == []


async def test_korean_exchange_skips_ib_historical_when_yahoo_missing(store):
    pos, details = _krx_position()
    fake_ib = FakeIB(positions=[pos], details={852105392: details})

    async def fake_yahoo_fetcher(symbol: str) -> float | None:
        return None

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=fake_yahoo_fetcher,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].last_price == 0.0
    assert fake_ib.ticker_calls == []
    assert fake_ib.historical_calls == []


async def test_gated_exchange_uses_portfolio_value_when_price_feeds_missing(store):
    """IB's account portfolio feed has broker-valued marketPrice/marketValue
    even when live and historical market data are not permitted."""
    pos, details = _smart_routed_tse_position()
    portfolio_item = FakePortfolioItem(
        account="U1",
        contract=details.contract,
        position=200.0,
        marketPrice=1100.0,
        marketValue=220_000.0,
        averageCost=1000.0,
        unrealizedPNL=20_000.0,
    )
    fake_ib = FakeIB(
        positions=[pos],
        details={14016494: details},
        portfolio_items=[portfolio_item],
    )

    async def fake_yahoo_fetcher(symbol: str) -> float | None:
        return None

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=fake_yahoo_fetcher,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].last_price == pytest.approx(1100.0)
    assert positions[0].market_value_native == pytest.approx(220_000.0)
    assert positions[0].unrealized_pnl_native == pytest.approx(20_000.0)
    assert positions[0].last_price_is_previous_close is False
    assert positions[0].last_price_is_broker_mark is True
    assert fake_ib.ticker_calls == []
    assert fake_ib.historical_calls == []


async def test_yahoo_fallback_skipped_when_exchange_not_mapped(store):
    """If we don't know the Yahoo suffix for this exchange, don't call Yahoo."""
    contract = FakeContract(conId=42, symbol="FOO", secType="STK", currency="JPY")
    details = FakeContractDetails(
        contract=FakeContract(
            conId=42, symbol="FOO", secType="STK", currency="JPY",
            primaryExchange="NOT_A_REAL_EXCHANGE",
        ),
        longName="FOO CORP",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=100.0, avgCost=10.0)
    fake_ib = FakeIB(positions=[pos], details={42: details})

    yahoo_calls = []
    async def fake_yahoo_fetcher(symbol: str) -> float | None:
        yahoo_calls.append(symbol)
        return 999.0

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=fake_yahoo_fetcher,
    )
    await adapter.connect()
    # NOT_A_REAL_EXCHANGE isn't in our IB_EXCHANGE_TO_SUFFIX, so the
    # position is dropped at name resolution. That's an existing behavior;
    # the assertion here is just that no Yahoo call was made.
    await adapter.get_positions()

    assert yahoo_calls == []


async def test_yahoo_fallback_returns_none_handled(store):
    """If Yahoo also can't price it, last_price stays 0 and we degrade."""
    pos, details = _tse_position()
    fake_ib = FakeIB(positions=[pos], details={14016494: details})

    async def fake_yahoo_fetcher(symbol: str) -> float | None:
        return None  # Yahoo also has no data

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=fake_yahoo_fetcher,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].last_price == 0.0
    assert positions[0].last_price_is_previous_close is False
    assert fake_ib.historical_calls == []


async def test_previous_close_misses_are_cached_for_the_day(store):
    pos, details = _tse_position()
    fake_ib = FakeIB(positions=[pos], details={14016494: details})

    yahoo_calls = []

    async def fake_yahoo_fetcher(symbol: str) -> float | None:
        yahoo_calls.append(symbol)
        return None

    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=fake_yahoo_fetcher,
    )
    await adapter.connect()
    await adapter.get_positions()
    await adapter.get_positions()

    assert yahoo_calls == ["6315.T"]
    assert fake_ib.historical_calls == []
