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


class _NullTicker:
    def __init__(self, contract):
        self.contract = contract
        self.last = -1.0
        self.close = -1.0
    def marketPrice(self):
        return None


class FakeIB:
    """IB instance where reqHistoricalDataAsync always raises (no permissions)."""
    def __init__(self, positions, details):
        self._positions = positions
        self._details = details
        self._connected = False
        self.historical_calls = []
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
        return [_NullTicker(c) for c in contracts]
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
