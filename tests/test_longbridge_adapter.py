from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.core.broker import ConnectionState
from app.core.fx import FxRate


@dataclass
class _StockPosition:
    symbol: str
    symbol_name: str
    quantity: Decimal
    currency: str
    cost_price: Decimal
    market: object = None


@dataclass
class _Channel:
    account_channel: str
    positions: list[_StockPosition]


@dataclass
class _StockPositionsResponse:
    channels: list[_Channel]


@dataclass
class _Quote:
    symbol: str
    last_done: Decimal
    prev_close: Decimal


@dataclass
class _StaticInfo:
    symbol: str
    name_en: str
    exchange: str
    currency: str


@dataclass
class _CashInfo:
    currency: str
    available_cash: Decimal = Decimal("0")
    frozen_cash: Decimal = Decimal("0")
    settling_cash: Decimal = Decimal("0")


@dataclass
class _Balance:
    currency: str
    net_assets: Decimal
    total_cash: Decimal
    buy_power: Decimal
    cash_infos: list[_CashInfo]


class _TradeContext:
    def __init__(
        self,
        *,
        response: _StockPositionsResponse | None = None,
        balances: list[_Balance] | None = None,
        fail: bool = False,
    ) -> None:
        self._response = response or _StockPositionsResponse([])
        self._balances = balances or []
        self.fail = fail
        self.balance_currencies: list[str | None] = []

    async def stock_positions(self):
        if self.fail:
            raise RuntimeError("stock positions unavailable")
        return self._response

    async def account_balance(self, currency=None):
        if self.fail:
            raise RuntimeError("account balance unavailable")
        self.balance_currencies.append(currency)
        return list(self._balances)


class _QuoteContext:
    def __init__(
        self,
        *,
        quotes: list[_Quote] | None = None,
        static: list[_StaticInfo] | None = None,
        fail: bool = False,
        reject_symbols: set[str] | None = None,
    ) -> None:
        self._quotes = quotes or []
        self._static = static or []
        self.fail = fail
        self.reject_symbols = reject_symbols or set()
        self.quoted_symbols: list[list[str]] = []
        self.static_symbols: list[list[str]] = []

    async def quote(self, symbols):
        if self.fail:
            raise RuntimeError("quotes unavailable")
        self.quoted_symbols.append(list(symbols))
        bad = self.reject_symbols.intersection(symbols)
        if bad:
            raise RuntimeError(f"quote rejected {sorted(bad)}")
        return list(self._quotes)

    async def static_info(self, symbols):
        if self.fail:
            raise RuntimeError("static info unavailable")
        self.static_symbols.append(list(symbols))
        bad = self.reject_symbols.intersection(symbols)
        if bad:
            raise RuntimeError(f"static info rejected {sorted(bad)}")
        return list(self._static)


class _Sdk:
    class Config:
        @staticmethod
        def from_apikey_env():
            return "config"

    class AsyncTradeContext:
        @staticmethod
        def create(config):
            return _TradeContext()

    class AsyncQuoteContext:
        @staticmethod
        def create(config):
            return _QuoteContext()


class _ConfigErrorSdk:
    class OpenApiException(Exception):
        pass

    class Config:
        @staticmethod
        def from_apikey_env():
            raise _ConfigErrorSdk.OpenApiException("missing API key")

    class AsyncTradeContext:
        @staticmethod
        def create(config):
            raise AssertionError("trade context should not be built")

    class AsyncQuoteContext:
        @staticmethod
        def create(config):
            raise AssertionError("quote context should not be built")


class _Fx:
    def get_rate_sync(self, currency):
        if currency == "HKD":
            return FxRate(
                pair="HKDUSD",
                rate=0.128,
                quoted_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
                is_stale=False,
                source="IB",
            )
        return None


def _stock(symbol="01810.HK", *, qty="400", cost="53.975", currency="HKD"):
    return _StockPosition(
        symbol=symbol,
        symbol_name=symbol,
        quantity=Decimal(qty),
        currency=currency,
        cost_price=Decimal(cost),
    )


def _balance(*, currency="HKD", cash="50000", net_assets="125000", buy_power="30000"):
    return _Balance(
        currency=currency,
        net_assets=Decimal(net_assets),
        total_cash=Decimal(cash),
        buy_power=Decimal(buy_power),
        cash_infos=[
            _CashInfo(
                currency=currency,
                available_cash=Decimal(cash),
                frozen_cash=Decimal("10"),
                settling_cash=Decimal("20"),
            )
        ],
    )


@pytest.mark.asyncio
async def test_longbridge_adapter_maps_stock_positions_with_quotes_and_static_info():
    from app.adapters.longbridge import LongbridgeAdapter

    trade = _TradeContext(
        response=_StockPositionsResponse([
            _Channel("SECURITIES", [_stock()])
        ])
    )
    quote = _QuoteContext(
        quotes=[_Quote("01810.HK", Decimal("49.4"), Decimal("48.0"))],
        static=[_StaticInfo("01810.HK", "XIAOMI-W", "SEHK", "HKD")],
    )
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_Sdk,
        trade_context_factory=lambda _config: trade,
        quote_context_factory=lambda _config: quote,
        fx_service=_Fx(),
        poll_interval_s=0,
    )

    await adapter.connect()
    positions = await adapter.get_positions()

    assert len(positions) == 1
    p = positions[0]
    assert p.broker == "Longbridge"
    assert p.account_id == "LB-1"
    assert p.native_key == "01810.HK"
    assert p.canonical_symbol == "1810.HK"
    assert p.native_symbol == "1810"
    assert p.exchange == "SEHK"
    assert p.currency == "HKD"
    assert p.name_en == "XIAOMI-W"
    assert p.asset_class == "STK"
    assert p.quantity == pytest.approx(400.0)
    assert p.avg_cost == pytest.approx(53.975)
    assert p.last_price == pytest.approx(49.4)
    assert p.previous_close == pytest.approx(48.0)
    assert p.market_value_native == pytest.approx(19760.0)
    assert p.market_value_usd == pytest.approx(2529.28)
    assert p.unrealized_pnl_native == pytest.approx(-1830.0)
    assert p.unrealized_pnl_usd == pytest.approx(-234.24)


@pytest.mark.asyncio
async def test_longbridge_adapter_falls_back_to_suffix_exchange_mapping():
    from app.adapters.longbridge import LongbridgeAdapter

    trade = _TradeContext(
        response=_StockPositionsResponse([
            _Channel("SECURITIES", [_stock("AAPL.US", qty="2", cost="150", currency="USD")])
        ])
    )
    quote = _QuoteContext(
        quotes=[_Quote("AAPL.US", Decimal("200"), Decimal("190"))],
        static=[_StaticInfo("AAPL.US", "", "", "USD")],
    )
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_Sdk,
        trade_context_factory=lambda _config: trade,
        quote_context_factory=lambda _config: quote,
        poll_interval_s=0,
    )

    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].canonical_symbol == "AAPL.US"
    assert positions[0].exchange == "NASDAQ"
    assert positions[0].name_en == "AAPL.US"


@pytest.mark.asyncio
async def test_longbridge_adapter_skips_bad_stock_symbols_without_disabling():
    from app.adapters.longbridge import LongbridgeAdapter

    trade = _TradeContext(
        response=_StockPositionsResponse([
            _Channel(
                "SECURITIES",
                [
                    _stock("BADSYMBOL", qty="2", cost="1", currency="USD"),
                    _stock("1234.XY", qty="3", cost="1", currency="USD"),
                    _stock("AAPL.US", qty="2", cost="150", currency="USD"),
                ],
            )
        ])
    )
    quote = _QuoteContext(
        quotes=[_Quote("AAPL.US", Decimal("200"), Decimal("190"))],
        static=[_StaticInfo("AAPL.US", "APPLE", "", "USD")],
    )
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_Sdk,
        trade_context_factory=lambda _config: trade,
        quote_context_factory=lambda _config: quote,
        poll_interval_s=0,
    )

    await adapter.connect()
    positions = await adapter.get_positions()

    assert await adapter.get_connection_state() is ConnectionState.CONNECTED
    assert [p.canonical_symbol for p in positions] == ["AAPL.US"]


@pytest.mark.asyncio
async def test_longbridge_adapter_isolates_quote_static_failures_by_symbol():
    from app.adapters.longbridge import LongbridgeAdapter

    trade = _TradeContext(
        response=_StockPositionsResponse([
            _Channel(
                "SECURITIES",
                [
                    _stock("MSFT.US", qty="3", cost="250", currency="USD"),
                    _stock("AAPL.US", qty="2", cost="150", currency="USD"),
                ],
            )
        ])
    )
    quote = _QuoteContext(
        quotes=[_Quote("AAPL.US", Decimal("200"), Decimal("190"))],
        static=[_StaticInfo("AAPL.US", "APPLE", "", "USD")],
        reject_symbols={"MSFT.US"},
    )
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_Sdk,
        trade_context_factory=lambda _config: trade,
        quote_context_factory=lambda _config: quote,
        poll_interval_s=0,
    )

    await adapter.connect()
    positions = await adapter.get_positions()

    assert [p.canonical_symbol for p in positions] == ["AAPL.US"]
    assert quote.quoted_symbols[:3] == [
        ["MSFT.US", "AAPL.US"],
        ["MSFT.US"],
        ["AAPL.US"],
    ]
    assert quote.static_symbols[:3] == [
        ["MSFT.US", "AAPL.US"],
        ["MSFT.US"],
        ["AAPL.US"],
    ]


@pytest.mark.asyncio
async def test_longbridge_adapter_synthesizes_cash_rows_from_cash_infos():
    from app.adapters.longbridge import LongbridgeAdapter

    trade = _TradeContext(balances=[_balance()])
    quote = _QuoteContext()
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_Sdk,
        trade_context_factory=lambda _config: trade,
        quote_context_factory=lambda _config: quote,
        fx_service=_Fx(),
        poll_interval_s=0,
    )

    await adapter.connect()
    positions = await adapter.get_positions()

    assert len(positions) == 1
    p = positions[0]
    assert p.broker == "Longbridge"
    assert p.account_id == "LB-1"
    assert p.asset_class == "CASH"
    assert p.native_key == "HKD"
    assert p.canonical_symbol == "HKD"
    assert p.name_en == "Hong Kong Dollar"
    assert p.quantity == pytest.approx(50030.0)
    assert p.market_value_native == pytest.approx(50030.0)
    assert p.market_value_usd == pytest.approx(6403.84)


@pytest.mark.asyncio
async def test_longbridge_adapter_maps_account_summary_to_usd():
    from app.adapters.longbridge import LongbridgeAdapter

    trade = _TradeContext(balances=[_balance()])
    quote = _QuoteContext()
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        base_currency="HKD",
        sdk=_Sdk,
        trade_context_factory=lambda _config: trade,
        quote_context_factory=lambda _config: quote,
        fx_service=_Fx(),
        poll_interval_s=0,
    )

    await adapter.connect()
    summaries = await adapter.get_account_summary()

    assert trade.balance_currencies == ["HKD", "HKD"]
    assert len(summaries) == 1
    s = summaries[0]
    assert s.broker == "Longbridge"
    assert s.account_id == "LB-1"
    assert s.base_currency == "HKD"
    assert s.net_liquidation_native == pytest.approx(125000.0)
    assert s.net_liquidation_usd == pytest.approx(16000.0)
    assert s.cash_usd == pytest.approx(6400.0)
    assert s.buying_power_usd == pytest.approx(3840.0)


@pytest.mark.asyncio
async def test_longbridge_adapter_marks_fx_unavailable_when_rate_missing():
    from app.adapters.longbridge import LongbridgeAdapter

    trade = _TradeContext(
        response=_StockPositionsResponse([
            _Channel("SECURITIES", [_stock("7203.JP", qty="10", cost="2000", currency="JPY")])
        ])
    )
    quote = _QuoteContext(
        quotes=[_Quote("7203.JP", Decimal("2500"), Decimal("2400"))],
        static=[_StaticInfo("7203.JP", "TOYOTA", "TSEJ", "JPY")],
    )
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_Sdk,
        trade_context_factory=lambda _config: trade,
        quote_context_factory=lambda _config: quote,
        fx_service=_Fx(),
        poll_interval_s=0,
    )

    await adapter.connect()
    positions = await adapter.get_positions()

    assert positions[0].market_value_usd == 0.0
    assert positions[0].unrealized_pnl_usd == 0.0
    assert positions[0].fx_unavailable is True


@pytest.mark.asyncio
async def test_longbridge_start_enters_reconnecting_and_recovers_after_initial_failure():
    from app.adapters.longbridge import LongbridgeAdapter

    attempts = 0

    def trade_factory(_config):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _TradeContext(fail=True)
        return _TradeContext(
            response=_StockPositionsResponse([
                _Channel("SECURITIES", [_stock("AAPL.US", qty="2", cost="150", currency="USD")])
            ])
        )

    quote = _QuoteContext(
        quotes=[_Quote("AAPL.US", Decimal("200"), Decimal("190"))],
        static=[_StaticInfo("AAPL.US", "APPLE", "NASDAQ", "USD")],
    )
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_Sdk,
        trade_context_factory=trade_factory,
        quote_context_factory=lambda _config: quote,
        poll_interval_s=0,
        reconnect_delays=(0.01, 0.01),
    )

    await adapter.start()
    assert await adapter.get_connection_state() is ConnectionState.RECONNECTING

    await __import__("asyncio").sleep(0.04)

    assert await adapter.get_connection_state() is ConnectionState.CONNECTED
    assert [p.canonical_symbol for p in await adapter.get_positions()] == ["AAPL.US"]
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_longbridge_refresh_failure_preserves_last_known_live_rows():
    from app.adapters.longbridge import LongbridgeAdapter
    from app.core.live_positions import LivePositions

    live = LivePositions()
    trade = _TradeContext(
        response=_StockPositionsResponse([
            _Channel("SECURITIES", [_stock("AAPL.US", qty="2", cost="150", currency="USD")])
        ])
    )
    quote = _QuoteContext(
        quotes=[_Quote("AAPL.US", Decimal("200"), Decimal("190"))],
        static=[_StaticInfo("AAPL.US", "APPLE", "NASDAQ", "USD")],
    )
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_Sdk,
        trade_context_factory=lambda _config: trade,
        quote_context_factory=lambda _config: quote,
        live_positions=live,
        poll_interval_s=0,
        reconnect_delays=(0.01,),
    )

    await adapter.connect()
    assert [p.canonical_symbol for p in live.get_all()] == ["AAPL.US"]

    trade.fail = True
    with pytest.raises(RuntimeError):
        await adapter._refresh_live_positions()

    assert [p.canonical_symbol for p in live.get_all()] == ["AAPL.US"]
    assert await adapter.get_connection_state() is ConnectionState.RECONNECTING
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_longbridge_start_does_not_retry_missing_api_env():
    from app.adapters.longbridge import LongbridgeAdapter

    calls = 0

    def config_factory():
        nonlocal calls
        calls += 1
        raise ValueError("missing Longbridge API credentials")

    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_Sdk,
        config_factory=config_factory,
        poll_interval_s=0,
        reconnect_delays=(0.01,),
    )

    await adapter.start()

    assert await adapter.get_connection_state() is ConnectionState.DISCONNECTED
    assert adapter.current_backoff_delay() is None
    assert calls == 1


@pytest.mark.asyncio
async def test_longbridge_start_does_not_retry_sdk_config_error():
    from app.adapters.longbridge import LongbridgeAdapter

    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_ConfigErrorSdk,
        poll_interval_s=0,
        reconnect_delays=(0.01,),
    )

    await adapter.start()

    assert await adapter.get_connection_state() is ConnectionState.DISCONNECTED
    assert adapter.current_backoff_delay() is None


@pytest.mark.asyncio
async def test_longbridge_multiple_position_channels_require_selector():
    from app.adapters.longbridge import LongbridgeAdapter, LongbridgeConfigError

    trade = _TradeContext(
        response=_StockPositionsResponse([
            _Channel("SECURITIES", [_stock("AAPL.US", qty="1", currency="USD")]),
            _Channel("FUND", [_stock("MSFT.US", qty="1", currency="USD")]),
        ])
    )
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        sdk=_Sdk,
        trade_context_factory=lambda _config: trade,
        quote_context_factory=lambda _config: _QuoteContext(),
        poll_interval_s=0,
    )

    with pytest.raises(LongbridgeConfigError, match="multiple stock position channels"):
        await adapter.connect()


@pytest.mark.asyncio
async def test_longbridge_position_channel_selector_chooses_single_channel():
    from app.adapters.longbridge import LongbridgeAdapter

    trade = _TradeContext(
        response=_StockPositionsResponse([
            _Channel("SECURITIES", [_stock("AAPL.US", qty="1", currency="USD")]),
            _Channel("FUND", [_stock("MSFT.US", qty="1", currency="USD")]),
        ])
    )
    quote = _QuoteContext(
        quotes=[_Quote("MSFT.US", Decimal("300"), Decimal("290"))],
        static=[_StaticInfo("MSFT.US", "MICROSOFT", "", "USD")],
    )
    adapter = LongbridgeAdapter(
        account_id="LB-1",
        position_channel="fund",
        sdk=_Sdk,
        trade_context_factory=lambda _config: trade,
        quote_context_factory=lambda _config: quote,
        poll_interval_s=0,
    )

    await adapter.connect()

    assert [p.canonical_symbol for p in await adapter.get_positions()] == ["MSFT.US"]
