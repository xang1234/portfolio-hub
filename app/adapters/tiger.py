"""Tiger Brokers OpenAPI Broker adapter.

Read-only polling adapter for Tiger's Python SDK. The SDK is synchronous, so
network calls run through asyncio.to_thread to keep FastAPI's event loop free.
"""

import asyncio
import importlib
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.adapters.tiger_config import (
    TigerAdapterSettings,
    TigerConfigError,
    clean_path as _clean_path,
)
from app.adapters.tiger_mapping import (
    TigerAccountRef as _TigerAccountRef,
    TigerAssetSnapshot as _TigerAssetSnapshot,
    TigerDataError,
    account_is_active as _account_is_active,
    account_type as _account_type,
    enum_value as _enum_value,
    first_attr_float as _first_attr_float,
    first_float as _first_float,
    global_cash_amounts as _global_cash_amounts,
    normalise_contract_symbol as _normalise_contract_symbol,
    prime_cash_amounts as _prime_cash_amounts,
    records as _records,
    safe_float as _safe_float,
    security_segment as _security_segment,
    text as _text,
)
from app.core.broker import AccountSummary, ConnectionState, Position
from app.core.fx import FxConversion, FxService
from app.core.live_positions import LivePositions
from app.core.symbols import CURRENCY_NAMES


_LOG = logging.getLogger(__name__)

_DEFAULT_RECONNECT_DELAYS: Sequence[float] = (
    5.0,
    15.0,
    60.0,
    60.0,
    60.0,
    60.0,
    60.0,
    60.0,
)

_QUOTE_BATCH_SIZE = 50

@dataclass(frozen=True)
class _TigerSdk:
    TigerOpenClientConfig: Any
    TradeClient: Any
    QuoteClient: Any
    SecurityType: Any
    Currency: Any
    Market: Any


class TigerAdapter:
    name = "Tiger"

    def __init__(
        self,
        *,
        config_dir: str | None = None,
        tiger_id: str | None = None,
        account: str | None = None,
        private_key: str | None = None,
        private_key_path: str | None = None,
        base_currency: str = "USD",
        markets: Sequence[str] = ("US", "HK", "SG", "AU", "CN"),
        sdk: Any | None = None,
        config_factory: Callable[..., Any] | None = None,
        trade_client_factory: Callable[[Any], Any] | None = None,
        quote_client_factory: Callable[[Any], Any] | None = None,
        fx_service: FxService | None = None,
        live_positions: LivePositions | None = None,
        poll_interval_s: float = 30.0,
        reconnect_delays: Sequence[float] | None = None,
    ) -> None:
        self._config_dir = _clean_path(config_dir)
        self._tiger_id = (tiger_id or "").strip() or None
        self._account = (account or "").strip() or None
        self._private_key = (private_key or "").strip() or None
        self._private_key_path = (private_key_path or "").strip() or None
        self._base_currency = base_currency.strip().upper() or "USD"
        self._markets = tuple(dict.fromkeys(m.strip().upper() for m in markets if m.strip()))
        self._sdk = sdk
        self._config_factory = config_factory
        self._trade_client_factory = trade_client_factory
        self._quote_client_factory = quote_client_factory
        self._fx_service = fx_service
        self._live_positions = live_positions
        self._poll_interval_s = poll_interval_s
        self._reconnect_delays: Sequence[float] = tuple(
            reconnect_delays if reconnect_delays is not None else _DEFAULT_RECONNECT_DELAYS
        )
        self._config: Any | None = None
        self._trade_client: Any | None = None
        self._quote_client: Any | None = None
        self._account_refs: dict[str, _TigerAccountRef] = {}
        self._connection_state = ConnectionState.DISCONNECTED
        self._poll_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._current_backoff_delay: float | None = None
        self._last_positions: list[Position] = []

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        fx_service: FxService | None = None,
        live_positions: LivePositions | None = None,
    ) -> "TigerAdapter":
        settings = TigerAdapterSettings.from_env(env)
        return cls(
            config_dir=settings.config_dir,
            tiger_id=settings.tiger_id,
            account=settings.account,
            private_key=settings.private_key,
            private_key_path=settings.private_key_path,
            base_currency=settings.base_currency,
            markets=settings.markets,
            fx_service=fx_service,
            live_positions=live_positions,
            poll_interval_s=settings.poll_interval_s,
        )

    async def start(self) -> None:
        try:
            await self.connect()
        except TigerConfigError as exc:
            _LOG.error("Tiger OpenAPI configuration invalid; adapter disabled: %s", exc)
            await self._close_clients()
            self._connection_state = ConnectionState.DISCONNECTED
            self._current_backoff_delay = None
        except Exception as exc:
            _LOG.warning("Tiger OpenAPI connect failed; entering reconnect loop: %s", exc)
            await self._close_clients()
            self._enter_reconnecting()

    async def connect(self) -> None:
        if self._reconnect_task is not asyncio.current_task():
            await self._stop_reconnecting()
        await self._stop_polling()
        await self._close_clients()
        sdk = self._get_sdk()
        config = self._build_config(sdk)
        self._config = config
        self._trade_client = self._build_trade_client(sdk, config)
        self._quote_client = self._build_quote_client(sdk, config)
        try:
            await self._load_accounts()
            positions = await self._fetch_positions()
        except Exception:
            await self._close_clients()
            self._connection_state = ConnectionState.DISCONNECTED
            raise
        self._last_positions = positions
        self._connection_state = ConnectionState.CONNECTED
        self._current_backoff_delay = None
        if self._live_positions is not None:
            self._live_positions.replace_broker(self.name, positions)
        if self._live_positions is not None and self._poll_interval_s > 0:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def disconnect(self) -> None:
        await self._stop_reconnecting()
        await self._stop_polling()
        await self._close_clients()
        self._connection_state = ConnectionState.DISCONNECTED

    async def is_connected(self) -> bool:
        return self._connection_state is ConnectionState.CONNECTED

    async def get_connection_state(self) -> ConnectionState:
        return self._connection_state

    def current_backoff_delay(self) -> float | None:
        return self._current_backoff_delay

    async def get_positions(self) -> list[Position]:
        if self._connection_state is not ConnectionState.CONNECTED:
            return []
        positions = await self._fetch_positions()
        self._last_positions = positions
        return positions

    async def get_account_summary(self) -> list[AccountSummary]:
        if self._connection_state is not ConnectionState.CONNECTED:
            return []
        asset_snapshots = await self._fetch_asset_snapshots()
        out: list[AccountSummary] = []
        for snapshot in asset_snapshots.values():
            out.append(self._summary_from_snapshot(snapshot))
        return out

    async def _fetch_positions(self) -> list[Position]:
        if self._trade_client is None:
            return []
        sdk = self._get_sdk()
        rows: list[tuple[str, Any]] = []
        account_results = await asyncio.gather(
            *(
                self._position_rows_for_account(sdk, account_id)
                for account_id in self._account_refs
            )
        )
        for account_id, account_rows in account_results:
            rows.extend((account_id, row) for row in account_rows)

        symbols = []
        for _account_id, row in rows:
            symbol = _text(getattr(getattr(row, "contract", None), "symbol", None), "")
            if symbol:
                symbols.append(symbol)
        quote_by_symbol = await self._quotes_by_symbol(symbols)
        asset_snapshots = await self._fetch_asset_snapshots()

        out: list[Position] = []
        for account_id, row in rows:
            try:
                position = self._position_from_row(account_id, row, quote_by_symbol)
            except TigerDataError as exc:
                _LOG.warning("Skipping invalid Tiger position: %s", exc)
                continue
            if position is not None:
                out.append(position)
        out.extend(self._cash_positions_from_snapshots(asset_snapshots.values()))
        return out

    async def _position_rows_for_account(
        self,
        sdk: Any,
        account_id: str,
    ) -> tuple[str, list[Any]]:
        if self._trade_client is None:
            return account_id, []
        account_rows = await asyncio.to_thread(
            self._trade_client.get_positions,
            account=account_id,
            sec_type=_enum_value(sdk, "SecurityType", "STK"),
            currency=_enum_value(sdk, "Currency", "ALL"),
            market=_enum_value(sdk, "Market", "ALL"),
        )
        rows = [
            row
            for row in list(account_rows or [])
            if _safe_float(getattr(row, "quantity", 0)) != 0.0
        ]
        return account_id, rows

    async def _quotes_by_symbol(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if self._quote_client is None:
            return {}
        unique = list(dict.fromkeys(symbols))
        out: dict[str, dict[str, Any]] = {}
        for idx in range(0, len(unique), _QUOTE_BATCH_SIZE):
            batch = unique[idx: idx + _QUOTE_BATCH_SIZE]
            try:
                records = await asyncio.to_thread(
                    self._quote_client.get_stock_briefs,
                    batch,
                    include_hour_trading=False,
                )
            except Exception as exc:
                _LOG.warning("Tiger quote batch failed; using position marks: %s", exc)
                continue
            for record in _records(records):
                symbol = str(record.get("symbol") or "").strip()
                if symbol:
                    out[symbol] = record
        return out

    def _position_from_row(
        self,
        account_id: str,
        row: Any,
        quote_by_symbol: dict[str, dict[str, Any]],
    ) -> Position | None:
        contract = getattr(row, "contract", None)
        if contract is None:
            raise TigerDataError("position missing contract")
        symbol = _text(getattr(contract, "symbol", None), "")
        market = _text(getattr(contract, "market", None), "").upper()
        if self._markets and market and market not in self._markets:
            return None
        quantity = _safe_float(getattr(row, "quantity", 0))
        if quantity == 0.0:
            return None
        row_account = _text(getattr(row, "account", None), "")
        if row_account and row_account != account_id:
            raise TigerDataError(
                "position row account "
                f"{row_account!r} does not match queried account {account_id!r}"
            )
        native_symbol, canonical_symbol, exchange = _normalise_contract_symbol(
            symbol=symbol,
            market=market,
            exchange_hint=_text(getattr(contract, "primary_exchange", None), ""),
        )
        currency = _text(getattr(contract, "currency", None), "USD").upper()
        quote = quote_by_symbol.get(symbol, {})
        quote_last = _first_float(quote, "latest_price", "close")
        row_last = _safe_float(getattr(row, "market_price", 0))
        last_price = quote_last or row_last
        previous_close = _first_float(quote, "pre_close", "adj_pre_close")
        avg_cost = _first_attr_float(
            row,
            "average_cost",
            "average_cost_by_average",
            "average_cost_of_carry",
        )
        market_value_native = (
            quantity * last_price
            if last_price
            else _safe_float(getattr(row, "market_value", 0))
        )
        pnl_native = (
            (last_price - avg_cost) * quantity
            if last_price and avg_cost
            else _safe_float(getattr(row, "unrealized_pnl", 0))
        )
        conv = self._convert_to_usd(currency, market_value_native, pnl_native)
        native_key = _text(getattr(contract, "identifier", None), "") or f"{market}:{symbol}"
        return Position(
            broker=self.name,
            account_id=account_id,
            native_key=native_key,
            canonical_symbol=canonical_symbol,
            native_symbol=native_symbol,
            exchange=exchange,
            currency=currency,
            name_en=_text(getattr(contract, "name", None), symbol),
            asset_class="STK",
            quantity=quantity,
            avg_cost=avg_cost,
            last_price=last_price,
            market_value_native=market_value_native,
            market_value_usd=conv.mv_usd,
            unrealized_pnl_native=pnl_native,
            unrealized_pnl_usd=conv.pnl_usd,
            fx_is_stale=conv.fx_is_stale,
            fx_is_fallback=conv.fx_is_fallback,
            fx_unavailable=conv.fx_unavailable,
            previous_close=previous_close,
        )

    async def _fetch_asset_snapshots(self) -> dict[str, _TigerAssetSnapshot]:
        snapshots = await asyncio.gather(
            *(
                self._asset_snapshot_for_account(account_ref)
                for account_ref in self._account_refs.values()
            )
        )
        return {
            snapshot.account.account_id: snapshot
            for snapshot in snapshots
            if snapshot is not None
        }

    async def _asset_snapshot_for_account(
        self,
        account_ref: _TigerAccountRef,
    ) -> _TigerAssetSnapshot | None:
        if account_ref.account_type == "GLOBAL":
            assets = await self._global_assets(account_ref.account_id)
            return self._global_asset_snapshot(account_ref, assets)
        assets = await self._prime_assets(account_ref.account_id)
        return self._prime_asset_snapshot(account_ref, assets)

    def _prime_asset_snapshot(
        self,
        account_ref: _TigerAccountRef,
        assets: Any | None,
    ) -> _TigerAssetSnapshot | None:
        segment = _security_segment(assets)
        if segment is None:
            return None
        return _TigerAssetSnapshot(
            account=account_ref,
            currency=_text(getattr(segment, "currency", None), self._base_currency).upper(),
            net_liquidation_native=_safe_float(getattr(segment, "net_liquidation", 0)),
            cash_native=_safe_float(getattr(segment, "cash_balance", 0)),
            buying_power_native=_first_attr_float(
                segment,
                "buying_power",
                "cash_available_for_trade",
            ),
            gross_position_value_native=_safe_float(
                getattr(segment, "gross_position_value", 0)
            ),
            cash_amounts=_prime_cash_amounts(segment),
        )

    def _global_asset_snapshot(
        self,
        account_ref: _TigerAccountRef,
        assets: Any | None,
    ) -> _TigerAssetSnapshot | None:
        if assets is None:
            return None
        summary = getattr(assets, "summary", None)
        if summary is None:
            return None
        return _TigerAssetSnapshot(
            account=account_ref,
            currency=_text(getattr(summary, "currency", None), self._base_currency).upper(),
            net_liquidation_native=_safe_float(getattr(summary, "net_liquidation", 0)),
            cash_native=_safe_float(getattr(summary, "cash", 0)),
            buying_power_native=_safe_float(getattr(summary, "buying_power", 0)),
            gross_position_value_native=_safe_float(
                getattr(summary, "gross_position_value", 0)
            ),
            cash_amounts=_global_cash_amounts(assets),
        )

    def _summary_from_snapshot(self, snapshot: _TigerAssetSnapshot) -> AccountSummary:
        currency = snapshot.currency
        return AccountSummary(
            broker=self.name,
            account_id=snapshot.account.account_id,
            base_currency=currency,
            net_liquidation_usd=self._to_usd(
                snapshot.net_liquidation_native,
                currency,
            ),
            cash_usd=self._to_usd(snapshot.cash_native, currency),
            buying_power_usd=self._to_usd(snapshot.buying_power_native, currency),
            net_liquidation_native=snapshot.net_liquidation_native,
            gross_position_value_usd=self._to_usd(
                snapshot.gross_position_value_native,
                currency,
            ),
        )

    def _cash_positions_from_snapshots(
        self,
        snapshots: Iterable[_TigerAssetSnapshot],
    ) -> list[Position]:
        out: list[Position] = []
        for snapshot in snapshots:
            for currency, amount in sorted(snapshot.cash_amounts.items()):
                if amount == 0.0:
                    continue
                conv = self._convert_to_usd(currency, amount, 0.0)
                out.append(
                    Position(
                        broker=self.name,
                        account_id=snapshot.account.account_id,
                        native_key=currency,
                        canonical_symbol=currency,
                        native_symbol=currency,
                        exchange="",
                        currency=currency,
                        name_en=CURRENCY_NAMES.get(currency, currency),
                        asset_class="CASH",
                        quantity=amount,
                        avg_cost=1.0,
                        last_price=1.0,
                        market_value_native=amount,
                        market_value_usd=conv.mv_usd,
                        unrealized_pnl_native=0.0,
                        unrealized_pnl_usd=0.0,
                        fx_is_stale=conv.fx_is_stale,
                        fx_is_fallback=conv.fx_is_fallback,
                        fx_unavailable=conv.fx_unavailable,
                    )
                )
        return out

    async def _prime_assets(self, account_id: str) -> Any | None:
        if self._trade_client is None:
            return None
        result = await asyncio.to_thread(
            self._trade_client.get_prime_assets,
            account=account_id,
            base_currency=self._base_currency,
        )
        if isinstance(result, list):
            return result[0] if result else None
        return result

    async def _global_assets(self, account_id: str) -> Any | None:
        if self._trade_client is None:
            return None
        result = await asyncio.to_thread(
            self._trade_client.get_assets,
            account=account_id,
            segment=True,
            market_value=True,
        )
        if isinstance(result, list):
            return result[0] if result else None
        return result

    async def _load_accounts(self) -> None:
        if self._trade_client is None:
            self._account_refs = {}
            return
        profiles = await asyncio.to_thread(
            self._trade_client.get_managed_accounts,
            account=self._account,
        )
        out: dict[str, _TigerAccountRef] = {}
        for profile in list(profiles or []):
            account_id = _text(getattr(profile, "account", None), "")
            if not account_id:
                continue
            if self._account is None and not _account_is_active(profile):
                continue
            out[account_id] = _TigerAccountRef(
                account_id=account_id,
                account_type=_account_type(profile, account_id),
            )
        if self._account is not None and self._account not in out:
            raise TigerConfigError(
                f"Tiger account {self._account!r} was not returned by managed account discovery"
            )
        if not out:
            raise TigerConfigError("Tiger managed account discovery returned no usable accounts")
        self._account_refs = out

    async def _refresh_live_positions(self) -> None:
        if self._live_positions is None:
            return
        try:
            positions = await self._fetch_positions()
        except Exception:
            self._enter_reconnecting()
            raise
        self._last_positions = positions
        self._live_positions.replace_broker(self.name, positions)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_interval_s)
                await self._refresh_live_positions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.warning("Tiger position refresh failed: %s", exc)
                if self._connection_state is not ConnectionState.CONNECTED:
                    return

    def _enter_reconnecting(self) -> None:
        if self._connection_state is ConnectionState.RECONNECTING:
            return
        self._connection_state = ConnectionState.RECONNECTING
        if not self._reconnect_delays:
            self._connection_state = ConnectionState.DISCONNECTED
            self._current_backoff_delay = None
            return
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        try:
            for attempt, delay in enumerate(self._reconnect_delays, start=1):
                self._current_backoff_delay = delay
                await asyncio.sleep(delay)
                if self._connection_state is ConnectionState.CONNECTED:
                    return
                try:
                    await self.connect()
                    _LOG.info("Tiger reconnect attempt %d succeeded", attempt)
                    self._current_backoff_delay = None
                    return
                except asyncio.CancelledError:
                    raise
                except TigerConfigError as exc:
                    _LOG.error("Tiger configuration invalid; stopping reconnect: %s", exc)
                    self._connection_state = ConnectionState.DISCONNECTED
                    self._current_backoff_delay = None
                    return
                except Exception as exc:
                    _LOG.warning("Tiger reconnect attempt %d failed: %s", attempt, exc)
                    self._connection_state = ConnectionState.RECONNECTING
                    continue
            _LOG.error(
                "Tiger reconnect exhausted after %d attempts; staying DISCONNECTED",
                len(self._reconnect_delays),
            )
            self._connection_state = ConnectionState.DISCONNECTED
            self._current_backoff_delay = None
        except asyncio.CancelledError:
            self._current_backoff_delay = None
            raise

    async def _stop_polling(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _stop_reconnecting(self) -> None:
        task = self._reconnect_task
        self._reconnect_task = None
        self._current_backoff_delay = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _close_clients(self) -> None:
        clients = [
            client
            for client in (self._trade_client, self._quote_client)
            if client is not None
        ]
        self._trade_client = None
        self._quote_client = None
        self._config = None
        self._account_refs = {}
        for client in clients:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    await asyncio.to_thread(close)
                except Exception as exc:
                    _LOG.debug("Tiger client close failed: %s", exc)

    def _get_sdk(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        try:
            config_mod = importlib.import_module("tigeropen.tiger_open_config")
            trade_mod = importlib.import_module("tigeropen.trade.trade_client")
            quote_mod = importlib.import_module("tigeropen.quote.quote_client")
            consts_mod = importlib.import_module("tigeropen.common.consts")
        except ImportError as exc:
            raise TigerConfigError(
                "TigerOpen SDK not installed; install tigeropen>=3.5.8"
            ) from exc
        self._sdk = _TigerSdk(
            TigerOpenClientConfig=config_mod.TigerOpenClientConfig,
            TradeClient=trade_mod.TradeClient,
            QuoteClient=quote_mod.QuoteClient,
            SecurityType=consts_mod.SecurityType,
            Currency=consts_mod.Currency,
            Market=consts_mod.Market,
        )
        return self._sdk

    def _build_config(self, sdk: Any) -> Any:
        kwargs = {"props_path": self._config_dir} if self._config_dir else {}
        try:
            if self._config_factory is not None:
                config = self._config_factory(**kwargs)
            else:
                config_cls = getattr(sdk, "TigerOpenClientConfig", None)
                if config_cls is None:
                    raise TigerConfigError("TigerOpenClientConfig unavailable")
                config = config_cls(**kwargs)
            if self._tiger_id is not None:
                setattr(config, "tiger_id", self._tiger_id)
            if self._account is not None:
                setattr(config, "account", self._account)
            private_key = self._private_key
            if private_key is None and self._private_key_path is not None:
                private_key = Path(self._private_key_path).expanduser().read_text().strip()
            if private_key is not None:
                setattr(config, "private_key", private_key)
            return config
        except TigerConfigError:
            raise
        except Exception as exc:
            raise TigerConfigError(str(exc)) from exc

    def _build_trade_client(self, sdk: Any, config: Any) -> Any:
        if self._trade_client_factory is not None:
            return self._trade_client_factory(config)
        factory = getattr(sdk, "TradeClient", None)
        if not callable(factory):
            raise TigerConfigError("Tiger SDK TradeClient unavailable")
        return factory(config)

    def _build_quote_client(self, sdk: Any, config: Any) -> Any:
        if self._quote_client_factory is not None:
            return self._quote_client_factory(config)
        factory = getattr(sdk, "QuoteClient", None)
        if not callable(factory):
            raise TigerConfigError("Tiger SDK QuoteClient unavailable")
        return factory(config)

    def _convert_to_usd(
        self, currency: str, mv_native: float, pnl_native: float,
    ) -> FxConversion:
        if currency == "USD":
            return FxConversion(mv_native, pnl_native, False, False, False)
        if self._fx_service is None:
            return FxConversion(0.0, 0.0, False, False, True)
        try:
            rate = self._fx_service.get_rate_sync(currency)
        except ValueError:
            _LOG.warning("Invalid FX currency on Tiger position: %s", currency)
            return FxConversion(0.0, 0.0, False, False, True)
        if rate is None:
            return FxConversion(0.0, 0.0, False, False, True)
        return FxConversion(
            mv_usd=mv_native * rate.rate,
            pnl_usd=pnl_native * rate.rate,
            fx_is_stale=rate.is_stale,
            fx_is_fallback=rate.source == "API_FALLBACK",
            fx_unavailable=False,
        )

    def _to_usd(self, amount: float, currency: str) -> float:
        return self._convert_to_usd(currency, amount, 0.0).mv_usd
