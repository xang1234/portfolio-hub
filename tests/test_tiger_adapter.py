from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import pytest

from app.core.broker import ConnectionState
from app.core.fx import FxRate
from app.core.live_positions import LivePositions


@dataclass
class _AccountProfile:
    account: str
    status: str = "Funded"
    account_type: str = "STANDARD"
    capability: str | None = None


@dataclass
class _Contract:
    symbol: str
    currency: str
    market: str
    identifier: str = ""
    sec_type: str = "STK"
    name: str = ""
    primary_exchange: str = ""


@dataclass
class _TigerPosition:
    account: str
    contract: _Contract
    quantity: float
    average_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float


@dataclass
class _CurrencyAsset:
    currency: str
    cash_balance: float = 0.0
    gross_position_value: float = 0.0
    cash_available_for_trade: float = 0.0


@dataclass
class _Segment:
    currency: str = "USD"
    cash_balance: float = 0.0
    cash_available_for_trade: float = 0.0
    buying_power: float = 0.0
    gross_position_value: float = 0.0
    net_liquidation: float = 0.0
    currency_assets: dict[str, _CurrencyAsset] | None = None


@dataclass
class _PrimeAssets:
    account: str
    segments: dict[str, _Segment]


@dataclass
class _GlobalSummary:
    currency: str = "USD"
    net_liquidation: float = 0.0
    cash: float = 0.0
    buying_power: float = 0.0
    gross_position_value: float = 0.0


@dataclass
class _MarketValue:
    currency: str
    cash_balance: float = 0.0
    stock_market_value: float = 0.0
    net_liquidation: float = 0.0


@dataclass
class _GlobalAssets:
    account: str
    summary: _GlobalSummary
    market_values: dict[str, _MarketValue]
    segments: dict[str, _Segment] | None = None


class _Frame:
    def __init__(self, records):
        self._records = list(records)

    @property
    def empty(self):
        return not self._records

    def to_dict(self, orient):
        assert orient == "records"
        return list(self._records)


class _Sdk:
    class TigerOpenClientConfig:
        def __init__(self, props_path=None):
            self.props_path = props_path
            self.tiger_id = None
            self.account = None
            self.private_key = None

    class SecurityType:
        STK = "STK"

    class Currency:
        ALL = "ALL"

    class Market:
        ALL = "ALL"


class _TradeClient:
    def __init__(
        self,
        *,
        accounts=None,
        positions=None,
        prime_assets=None,
        global_assets=None,
        fail_positions=False,
        delay_s=0.0,
    ):
        self.accounts = accounts or []
        self.positions = positions or {}
        self.prime_assets = prime_assets or {}
        self.global_assets = global_assets or {}
        self.fail_positions = fail_positions
        self.delay_s = delay_s
        self.position_calls = []
        self.prime_asset_calls = []
        self.asset_calls = []
        self._lock = threading.Lock()
        self._active_positions = 0
        self._active_assets = 0
        self.max_position_concurrency = 0
        self.max_asset_concurrency = 0

    def _enter_position_call(self):
        with self._lock:
            self._active_positions += 1
            self.max_position_concurrency = max(
                self.max_position_concurrency,
                self._active_positions,
            )

    def _leave_position_call(self):
        with self._lock:
            self._active_positions -= 1

    def _enter_asset_call(self):
        with self._lock:
            self._active_assets += 1
            self.max_asset_concurrency = max(
                self.max_asset_concurrency,
                self._active_assets,
            )

    def _leave_asset_call(self):
        with self._lock:
            self._active_assets -= 1

    def get_managed_accounts(self, account=None):
        if account:
            return [profile for profile in self.accounts if profile.account == account]
        return list(self.accounts)

    def get_positions(self, **kwargs):
        account = kwargs.get("account")
        self._enter_position_call()
        try:
            self.position_calls.append(kwargs)
            if self.delay_s:
                time.sleep(self.delay_s)
            if self.fail_positions:
                raise RuntimeError("positions unavailable")
            return list(self.positions.get(account, []))
        finally:
            self._leave_position_call()

    def get_prime_assets(self, **kwargs):
        account = kwargs.get("account")
        self._enter_asset_call()
        try:
            self.prime_asset_calls.append(kwargs)
            if self.delay_s:
                time.sleep(self.delay_s)
            return self.prime_assets.get(account)
        finally:
            self._leave_asset_call()

    def get_assets(self, **kwargs):
        account = kwargs.get("account")
        self._enter_asset_call()
        try:
            self.asset_calls.append(kwargs)
            if self.delay_s:
                time.sleep(self.delay_s)
            value = self.global_assets.get(account)
            return [] if value is None else [value]
        finally:
            self._leave_asset_call()


class _QuoteClient:
    def __init__(self, quotes=None, fail=False):
        self.quotes = quotes or {}
        self.fail = fail
        self.calls = []

    def get_stock_briefs(self, symbols, **kwargs):
        self.calls.append(list(symbols))
        if self.fail:
            raise RuntimeError("quotes unavailable")
        return _Frame(
            [
                {"symbol": symbol, **self.quotes[symbol]}
                for symbol in symbols
                if symbol in self.quotes
            ]
        )


class _Fx:
    def get_rate_sync(self, currency):
        rates = {
            "HKD": 0.128,
            "SGD": 0.74,
            "AUD": 0.66,
            "CNH": 0.138,
        }
        rate = rates.get(currency)
        if rate is None:
            return None
        return FxRate(
            pair=f"{currency}USD",
            rate=rate,
            quoted_at=datetime.now(timezone.utc),
            is_stale=False,
            source="IB",
        )


def _position(account, symbol, market, currency, *, qty=10, price=20, cost=15, **contract_kwargs):
    contract = _Contract(
        symbol=symbol,
        market=market,
        currency=currency,
        **contract_kwargs,
    )
    return _TigerPosition(
        account=account,
        contract=contract,
        quantity=qty,
        average_cost=cost,
        market_price=price,
        market_value=qty * price,
        unrealized_pnl=(price - cost) * qty,
    )


def _adapter(*, trade, quote=None, **kwargs):
    from app.adapters.tiger import TigerAdapter

    quote = quote or _QuoteClient()
    return TigerAdapter(
        sdk=_Sdk,
        trade_client_factory=lambda _config: trade,
        quote_client_factory=lambda _config: quote,
        fx_service=_Fx(),
        poll_interval_s=0,
        **kwargs,
    ), quote


@pytest.mark.asyncio
async def test_tiger_adapter_builds_config_from_properties_dir_and_direct_fields():
    from app.adapters.tiger import TigerAdapter

    built_configs = []

    def config_factory(**kwargs):
        config = _Sdk.TigerOpenClientConfig(props_path=kwargs.get("props_path"))
        built_configs.append((config, kwargs))
        return config

    trade = _TradeClient(accounts=[_AccountProfile("TIGER-1")])
    adapter = TigerAdapter(
        sdk=_Sdk,
        config_dir="/secure/tiger",
        tiger_id="developer-id",
        account="TIGER-1",
        private_key="PRIVATE KEY",
        config_factory=config_factory,
        trade_client_factory=lambda config: trade,
        quote_client_factory=lambda _config: _QuoteClient(),
        poll_interval_s=0,
    )

    await adapter.connect()

    assert built_configs[0][1] == {"props_path": "/secure/tiger"}
    config = built_configs[0][0]
    assert config.tiger_id == "developer-id"
    assert config.account == "TIGER-1"
    assert config.private_key == "PRIVATE KEY"


@pytest.mark.asyncio
async def test_tiger_adapter_expands_user_in_config_dir():
    from app.adapters.tiger import TigerAdapter

    built_configs = []

    def config_factory(**kwargs):
        config = _Sdk.TigerOpenClientConfig(props_path=kwargs.get("props_path"))
        built_configs.append((config, kwargs))
        return config

    trade = _TradeClient(accounts=[_AccountProfile("TIGER-1")])
    adapter = TigerAdapter(
        sdk=_Sdk,
        config_dir="~/.tigeropen/",
        config_factory=config_factory,
        trade_client_factory=lambda config: trade,
        quote_client_factory=lambda _config: _QuoteClient(),
        poll_interval_s=0,
    )

    await adapter.connect()

    assert built_configs[0][1] == {
        "props_path": str(Path("~/.tigeropen/").expanduser())
    }


def test_tiger_adapter_from_env_parses_tiger_settings():
    from app.adapters.tiger import TigerAdapter, TigerAdapterSettings

    live = LivePositions()
    fx = _Fx()
    env = {
        "TIGER_CONFIG_DIR": "/secure/tiger",
        "TIGER_ID": "developer-id",
        "TIGER_ACCOUNT": "TIGER-1",
        "TIGER_PRIVATE_KEY": "PRIVATE KEY",
        "TIGER_PRIVATE_KEY_PATH": "/secure/key.pem",
        "TIGER_BASE_CURRENCY": "hkd",
        "TIGER_MARKETS": "US,HK,SG",
        "TIGER_POLL_INTERVAL_S": "45",
    }

    settings = TigerAdapterSettings.from_env(env)
    adapter = TigerAdapter.from_env(
        env,
        fx_service=fx,
        live_positions=live,
    )

    assert settings.config_dir == "/secure/tiger"
    assert settings.tiger_id == "developer-id"
    assert settings.account == "TIGER-1"
    assert settings.private_key == "PRIVATE KEY"
    assert settings.private_key_path == "/secure/key.pem"
    assert settings.base_currency == "HKD"
    assert settings.markets == ("US", "HK", "SG")
    assert settings.poll_interval_s == 45.0
    assert isinstance(adapter, TigerAdapter)


@pytest.mark.asyncio
async def test_tiger_adapter_discovers_only_open_or_funded_accounts_unless_explicit():
    trade = _TradeClient(
        accounts=[
            _AccountProfile("FUNDED", status="Funded"),
            _AccountProfile("OPEN", status="Open"),
            _AccountProfile("PENDING", status="Pending"),
        ],
        positions={
            "FUNDED": [_position("FUNDED", "AAPL", "US", "USD")],
            "OPEN": [_position("OPEN", "MSFT", "US", "USD")],
            "PENDING": [_position("PENDING", "TSLA", "US", "USD")],
        },
    )
    adapter, _quote = _adapter(trade=trade)

    await adapter.connect()
    positions = await adapter.get_positions()

    assert [p.account_id for p in positions] == ["FUNDED", "OPEN"]
    assert Counter(call["account"] for call in trade.position_calls) == Counter(
        {"FUNDED": 2, "OPEN": 2}
    )

    explicit_trade = _TradeClient(
        accounts=[_AccountProfile("PENDING", status="Pending")],
        positions={"PENDING": [_position("PENDING", "TSLA", "US", "USD")]},
    )
    explicit_adapter, _quote = _adapter(trade=explicit_trade, account="PENDING")

    await explicit_adapter.connect()
    explicit_positions = await explicit_adapter.get_positions()

    assert [p.account_id for p in explicit_positions] == ["PENDING"]


@pytest.mark.asyncio
async def test_tiger_adapter_rejects_explicit_account_not_returned_by_tiger():
    from app.adapters.tiger import TigerConfigError

    trade = _TradeClient(accounts=[])
    adapter, _quote = _adapter(trade=trade, account="MISSING")

    with pytest.raises(TigerConfigError, match="MISSING"):
        await adapter.connect()


@pytest.mark.asyncio
async def test_tiger_adapter_rejects_unknown_account_type():
    from app.adapters.tiger import TigerConfigError

    trade = _TradeClient(accounts=[_AccountProfile("TIGER-1", account_type="FUTURE")])
    adapter, _quote = _adapter(trade=trade)

    with pytest.raises(TigerConfigError, match="FUTURE"):
        await adapter.connect()


@pytest.mark.asyncio
async def test_tiger_adapter_uses_queried_account_when_position_row_omits_account():
    queried_account = "TIGER-1"
    row = _position("", "AAPL", "US", "USD")
    trade = _TradeClient(
        accounts=[_AccountProfile(queried_account)],
        positions={queried_account: [row]},
    )
    adapter, _quote = _adapter(trade=trade)

    await adapter.connect()
    positions = [p for p in await adapter.get_positions() if p.asset_class == "STK"]

    assert [p.account_id for p in positions] == [queried_account]


@pytest.mark.asyncio
async def test_tiger_adapter_skips_position_row_from_unexpected_account():
    queried_account = "TIGER-1"
    row = _position("OTHER", "AAPL", "US", "USD")
    trade = _TradeClient(
        accounts=[_AccountProfile(queried_account)],
        positions={queried_account: [row]},
    )
    adapter, _quote = _adapter(trade=trade)

    await adapter.connect()
    positions = [p for p in await adapter.get_positions() if p.asset_class == "STK"]

    assert positions == []


@pytest.mark.asyncio
async def test_tiger_adapter_fetches_linked_account_positions_concurrently():
    trade = _TradeClient(
        accounts=[
            _AccountProfile("TIGER-1"),
            _AccountProfile("TIGER-2"),
        ],
        positions={
            "TIGER-1": [_position("TIGER-1", "AAPL", "US", "USD")],
            "TIGER-2": [_position("TIGER-2", "MSFT", "US", "USD")],
        },
        delay_s=0.03,
    )
    adapter, _quote = _adapter(trade=trade)

    await adapter.connect()

    assert trade.max_position_concurrency > 1


@pytest.mark.asyncio
async def test_tiger_adapter_fetches_linked_account_assets_concurrently():
    trade = _TradeClient(
        accounts=[
            _AccountProfile("TIGER-1", account_type="STANDARD"),
            _AccountProfile("TIGER-2", account_type="STANDARD"),
        ],
        prime_assets={
            "TIGER-1": _PrimeAssets("TIGER-1", {"S": _Segment()}),
            "TIGER-2": _PrimeAssets("TIGER-2", {"S": _Segment()}),
        },
        delay_s=0.03,
    )
    adapter, _quote = _adapter(trade=trade)

    await adapter.connect()

    assert trade.max_asset_concurrency > 1


@pytest.mark.asyncio
async def test_tiger_adapter_maps_stock_positions_for_supported_markets_and_quotes():
    account = "TIGER-1"
    rows = [
        _position(
            account,
            "AAPL",
            "US",
            "USD",
            identifier="US-AAPL",
            name="Apple Inc",
            primary_exchange="NASDAQ",
        ),
        _position(account, "00700", "HK", "HKD", identifier="HK-00700", name="Tencent"),
        _position(account, "D05", "SG", "SGD", identifier="SG-D05", name="DBS"),
        _position(account, "BHP", "AU", "AUD", identifier="AU-BHP", name="BHP Group"),
        _position(account, "600519", "CN", "CNH", identifier="CN-600519", name="Kweichow Moutai"),
        _position(account, "000001", "CN", "CNH", identifier="CN-000001", name="Ping An Bank"),
    ]
    trade = _TradeClient(accounts=[_AccountProfile(account)], positions={account: rows})
    quote = _QuoteClient(
        quotes={
            "AAPL": {"latest_price": 210.0, "pre_close": 200.0},
            "00700": {"latest_price": 380.0, "pre_close": 370.0},
            "D05": {"latest_price": 44.0, "pre_close": 43.0},
            "BHP": {"latest_price": 40.0, "pre_close": 39.5},
            "600519": {"latest_price": 1500.0, "pre_close": 1490.0},
            "000001": {"latest_price": 12.0, "pre_close": 11.5},
        }
    )
    adapter, _quote = _adapter(trade=trade, quote=quote)

    await adapter.connect()
    positions = await adapter.get_positions()

    by_key = {p.native_key: p for p in positions}
    assert by_key["US-AAPL"].canonical_symbol == "AAPL.US"
    assert by_key["US-AAPL"].exchange == "NASDAQ"
    assert by_key["US-AAPL"].last_price == pytest.approx(210.0)
    assert by_key["US-AAPL"].previous_close == pytest.approx(200.0)
    assert by_key["HK-00700"].canonical_symbol == "700.HK"
    assert by_key["HK-00700"].exchange == "SEHK"
    assert by_key["HK-00700"].market_value_usd == pytest.approx(380.0 * 10 * 0.128)
    assert by_key["SG-D05"].canonical_symbol == "D05.SG"
    assert by_key["SG-D05"].exchange == "SGX"
    assert by_key["AU-BHP"].canonical_symbol == "BHP.AU"
    assert by_key["AU-BHP"].exchange == "ASX"
    assert by_key["CN-600519"].canonical_symbol == "600519.SH"
    assert by_key["CN-600519"].exchange == "SSE"
    assert by_key["CN-000001"].canonical_symbol == "000001.SZ"
    assert by_key["CN-000001"].exchange == "SZSE"


@pytest.mark.asyncio
async def test_tiger_adapter_batches_quotes_and_falls_back_to_position_market_price():
    account = "TIGER-1"
    rows = [
        _position(account, f"SYM{i}", "US", "USD", identifier=f"ID{i}", price=100 + i)
        for i in range(55)
    ]
    trade = _TradeClient(accounts=[_AccountProfile(account)], positions={account: rows})
    quote = _QuoteClient(
        quotes={f"SYM{i}": {"latest_price": 200 + i, "pre_close": 190 + i} for i in range(54)}
    )
    adapter, _quote = _adapter(trade=trade, quote=quote)

    await adapter.connect()
    positions = await adapter.get_positions()

    assert [len(call) for call in quote.calls[-2:]] == [50, 5]
    by_symbol = {p.native_symbol: p for p in positions}
    assert by_symbol["SYM0"].last_price == pytest.approx(200.0)
    assert by_symbol["SYM54"].last_price == pytest.approx(154.0)
    assert by_symbol["SYM54"].previous_close == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_tiger_adapter_maps_prime_and_global_account_summaries_and_cash_rows():
    prime_account = "TIGER-PRIME"
    global_account = "U1234567"
    prime_segment = _Segment(
        currency="USD",
        cash_balance=1000.0,
        cash_available_for_trade=900.0,
        buying_power=5000.0,
        gross_position_value=12000.0,
        net_liquidation=13000.0,
        currency_assets={
            "USD": _CurrencyAsset("USD", cash_balance=1000.0),
            "HKD": _CurrencyAsset("HKD", cash_balance=8000.0),
        },
    )
    global_assets = _GlobalAssets(
        account=global_account,
        summary=_GlobalSummary(
            currency="USD",
            net_liquidation=20000.0,
            cash=2500.0,
            buying_power=6000.0,
            gross_position_value=17000.0,
        ),
        market_values={
            "USD": _MarketValue(
                "USD",
                cash_balance=2000.0,
                stock_market_value=16000.0,
                net_liquidation=18000.0,
            ),
            "HKD": _MarketValue(
                "HKD",
                cash_balance=3900.0,
                stock_market_value=7800.0,
                net_liquidation=11700.0,
            ),
        },
    )
    trade = _TradeClient(
        accounts=[
            _AccountProfile(prime_account, account_type="STANDARD"),
            _AccountProfile(global_account, account_type="GLOBAL"),
        ],
        prime_assets={prime_account: _PrimeAssets(prime_account, {"S": prime_segment})},
        global_assets={global_account: global_assets},
    )
    live = LivePositions()
    adapter, _quote = _adapter(trade=trade, live_positions=live)

    await adapter.connect()
    summaries = await adapter.get_account_summary()
    cash_rows = [p for p in live.get_all() if p.asset_class == "CASH"]

    by_account = {s.account_id: s for s in summaries}
    assert by_account[prime_account].net_liquidation_usd == pytest.approx(13000.0)
    assert by_account[prime_account].cash_usd == pytest.approx(1000.0)
    assert by_account[prime_account].gross_position_value_usd == pytest.approx(12000.0)
    assert by_account[global_account].net_liquidation_usd == pytest.approx(20000.0)
    assert by_account[global_account].cash_usd == pytest.approx(2500.0)
    assert by_account[global_account].gross_position_value_usd == pytest.approx(17000.0)
    assert sorted((p.account_id, p.currency, p.market_value_usd) for p in cash_rows) == [
        (prime_account, "HKD", pytest.approx(1024.0)),
        (prime_account, "USD", pytest.approx(1000.0)),
        (global_account, "HKD", pytest.approx(499.2)),
        (global_account, "USD", pytest.approx(2000.0)),
    ]


@pytest.mark.asyncio
async def test_tiger_account_summary_fetches_fresh_assets_after_position_snapshot():
    account = "TIGER-1"
    initial_segment = _Segment(
        currency="USD",
        cash_balance=1000.0,
        buying_power=5000.0,
        gross_position_value=12000.0,
        net_liquidation=13000.0,
    )
    updated_segment = _Segment(
        currency="USD",
        cash_balance=2000.0,
        buying_power=6000.0,
        gross_position_value=22000.0,
        net_liquidation=24000.0,
    )
    trade = _TradeClient(
        accounts=[_AccountProfile(account, account_type="STANDARD")],
        prime_assets={account: _PrimeAssets(account, {"S": initial_segment})},
    )
    adapter, _quote = _adapter(trade=trade)

    await adapter.connect()
    trade.prime_assets[account] = _PrimeAssets(account, {"S": updated_segment})

    summary = (await adapter.get_account_summary())[0]

    assert summary.net_liquidation_usd == pytest.approx(24000.0)
    assert summary.cash_usd == pytest.approx(2000.0)


@pytest.mark.asyncio
async def test_tiger_adapter_seeds_live_positions_and_enters_reconnect_on_refresh_failure():
    account = "TIGER-1"
    live = LivePositions()
    trade = _TradeClient(
        accounts=[_AccountProfile(account)],
        positions={account: [_position(account, "AAPL", "US", "USD")]},
        fail_positions=False,
    )
    from app.adapters.tiger import TigerAdapter

    adapter = TigerAdapter(
        sdk=_Sdk,
        trade_client_factory=lambda _config: trade,
        quote_client_factory=lambda _config: _QuoteClient(),
        fx_service=_Fx(),
        live_positions=live,
        poll_interval_s=0,
        reconnect_delays=(),
    )

    await adapter.connect()
    assert [p.broker for p in live.get_all()] == ["Tiger"]

    trade.fail_positions = True
    with pytest.raises(RuntimeError, match="positions unavailable"):
        await adapter._refresh_live_positions()

    assert await adapter.get_connection_state() is ConnectionState.DISCONNECTED
    assert [p.broker for p in live.get_all()] == ["Tiger"]


@pytest.mark.asyncio
async def test_tiger_start_does_not_retry_configuration_errors():
    from app.adapters.tiger import TigerAdapter

    adapter = TigerAdapter(
        sdk=_Sdk,
        config_factory=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("missing Tiger config")
        ),
        trade_client_factory=lambda _config: _TradeClient(),
        quote_client_factory=lambda _config: _QuoteClient(),
        poll_interval_s=0,
    )

    await adapter.start()

    assert await adapter.get_connection_state() is ConnectionState.DISCONNECTED
    assert adapter.current_backoff_delay() is None
