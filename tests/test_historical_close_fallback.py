"""When the live ticker doesn't return a last price (international markets
without paid market-data subscriptions: TSEJ, SBF, IBIS, KRX...), fall
back to the daily-bar close via reqHistoricalDataAsync.

reqHistoricalDataAsync is subscription-independent for end-of-day data
on most exchanges, so this gives us a usable MV for any holding,
labeled with a "prev close" subtext so users know it's not live.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

import pytest


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
class FakeBar:
    date: datetime
    close: float


class _NullTicker:
    """A ticker with no live data — like what TSEJ/SBF/IBIS return without a sub."""
    def __init__(self, contract):
        self.contract = contract
        self.last = -1.0  # IB's "no data" sentinel
        self.close = -1.0

    def marketPrice(self):
        return None


class FakeIB:
    def __init__(self, positions, details, historical_closes):
        self._positions = positions
        self._details = details
        self._historical_closes = historical_closes  # {conId: float} or {conId: Exception}
        self._connected = False
        self.historical_calls = []

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
        # Return null tickers — no live data
        return [_NullTicker(c) for c in contracts]

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        self.historical_calls.append(contract.conId)
        result = self._historical_closes.get(contract.conId)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return []
        return [FakeBar(date=datetime(2026, 5, 14), close=float(result))]


def _tse_position():
    contract = FakeContract(conId=14016494, symbol="6315", secType="STK", currency="JPY")
    details = FakeContractDetails(
        contract=FakeContract(
            conId=14016494, symbol="6315", secType="STK", currency="JPY",
            primaryExchange="TSEJ",
        ),
        longName="TOYO ENGINEERING CORP",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=200.0, avgCost=1000.0)
    return pos, details


def _tse_position_with_conid(conid: int, symbol: str):
    contract = FakeContract(conId=conid, symbol=symbol, secType="STK", currency="JPY")
    details = FakeContractDetails(
        contract=FakeContract(
            conId=conid, symbol=symbol, secType="STK", currency="JPY",
            primaryExchange="TSEJ",
        ),
        longName=f"{symbol} CORP",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=100.0, avgCost=1000.0)
    return pos, details


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


async def _no_yahoo(_symbol: str) -> float | None:
    return None


# Historical fallback kicks in when ticker has no data ----------------------


async def test_uses_historical_close_when_ticker_has_no_last(store):
    """Japanese stock with no live data → historical close = 1100 JPY → MV = 220,000."""
    pos, details = _tse_position()
    fake_ib = FakeIB(
        positions=[pos],
        details={14016494: details},
        historical_closes={14016494: 1100.0},
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=_no_yahoo,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    p = positions[0]
    assert p.last_price == pytest.approx(1100.0)
    assert p.market_value_native == pytest.approx(200 * 1100.0)


async def test_historical_close_sets_previous_close_flag(store):
    """The Position carries a flag so the template can show 'prev close' subtext."""
    pos, details = _tse_position()
    fake_ib = FakeIB(
        positions=[pos],
        details={14016494: details},
        historical_closes={14016494: 1100.0},
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=_no_yahoo,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].last_price_is_previous_close is True


# Live ticker bypasses the fallback ----------------------------------------


async def test_live_ticker_used_for_last_price_not_for_fallback(store):
    """Live ticker wins for last_price; the historical fetch still happens
    once (cache-amortized across the trading day) so previous_close is
    available for the intraday %, but it doesn't override the live tick."""
    class LiveIB(FakeIB):
        async def reqTickersAsync(self, *contracts):
            tickers = []
            for c in contracts:
                t = _NullTicker(c)
                t.last = 420.0  # Real live price
                tickers.append(t)
            return tickers

    pos, details = _tse_position()
    fake_ib = LiveIB(
        positions=[pos],
        details={14016494: details},
        historical_closes={14016494: 410.0},  # yesterday's close
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=_no_yahoo,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    # Live tick wins for last_price
    assert positions[0].last_price == pytest.approx(420.0)
    assert positions[0].last_price_is_previous_close is False
    # …but the historical fetch still populated previous_close so the
    # intraday-change % can be computed downstream.
    assert positions[0].previous_close == pytest.approx(410.0)
    assert fake_ib.historical_calls == [14016494]


# Historical fetch failure → safe degradation ------------------------------


async def test_historical_fetch_exception_falls_back_to_zero(store):
    """If reqHistoricalData raises (network error, no data permission),
    don't crash — leave last_price=0 so the row renders — like before."""
    pos, details = _tse_position()
    fake_ib = FakeIB(
        positions=[pos],
        details={14016494: details},
        historical_closes={14016494: ValueError("no data permission")},
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=_no_yahoo,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].last_price == 0.0
    assert positions[0].last_price_is_previous_close is False


async def test_no_historical_bars_returns_zero(store):
    """Empty bar list (some exchanges have no historical data at all)
    should also degrade gracefully."""
    pos, details = _tse_position()
    fake_ib = FakeIB(
        positions=[pos],
        details={14016494: details},
        historical_closes={14016494: None},  # FakeIB returns [] for None
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=_no_yahoo,
    )
    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].last_price == 0.0


async def test_missing_previous_close_fetches_run_concurrently(store):
    """A portfolio with many unsubscribed markets should not block startup
    one historical timeout at a time."""
    pos1, details1 = _tse_position_with_conid(1, "1111")
    pos2, details2 = _tse_position_with_conid(2, "2222")

    class SlowHistoricalIB(FakeIB):
        async def reqHistoricalDataAsync(self, contract, **kwargs):
            self.historical_calls.append(contract.conId)
            await asyncio.sleep(0.3)
            return [FakeBar(date=datetime(2026, 5, 14), close=1100.0 + contract.conId)]

    fake_ib = SlowHistoricalIB(
        positions=[pos1, pos2],
        details={1: details1, 2: details2},
        historical_closes={},
    )
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store,
        yahoo_quote_fetcher=_no_yahoo,
    )
    await adapter.connect()
    positions = await asyncio.wait_for(adapter.get_positions(), timeout=0.45)

    assert {p.native_symbol: p.last_price for p in positions} == {
        "1111": pytest.approx(1101.0),
        "2222": pytest.approx(1102.0),
    }
    assert sorted(fake_ib.historical_calls) == [1, 2]
