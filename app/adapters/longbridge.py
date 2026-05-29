"""Longbridge OpenAPI Broker adapter.

Read-only holdings adapter for the dashboard's Broker Protocol. Longbridge's
SDK is async, so unlike the Futu adapter this does not need asyncio.to_thread.
"""

import asyncio
import importlib
import inspect
import logging
from collections.abc import Callable, Sequence
from typing import Any

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

_SUFFIX_TO_EXCHANGE: dict[str, str] = {
    "HK": "SEHK",
    "US": "NASDAQ",
    "SG": "SGX",
    "SH": "SSE",
    "SZ": "SZSE",
    "JP": "TSEJ",
    "AU": "ASX",
    "UK": "LSE",
}


class LongbridgeConfigError(RuntimeError):
    """Longbridge adapter configuration is invalid and should not be retried."""


class LongbridgeDataError(RuntimeError):
    """A Longbridge payload row cannot be mapped into dashboard data."""


class LongbridgeAdapter:
    name = "Longbridge"

    def __init__(
        self,
        *,
        account_id: str,
        position_channel: str | None = None,
        base_currency: str = "USD",
        sdk: Any | None = None,
        config_factory: Callable[[], Any] | None = None,
        trade_context_factory: Callable[[Any], Any] | None = None,
        quote_context_factory: Callable[[Any], Any] | None = None,
        fx_service: FxService | None = None,
        live_positions: LivePositions | None = None,
        poll_interval_s: float = 30.0,
        reconnect_delays: Sequence[float] | None = None,
    ) -> None:
        self._account_id = account_id.strip() or self.name
        self._position_channel = (position_channel or "").strip()
        self._position_channel_key = _normalise_channel(self._position_channel)
        self._base_currency = base_currency.strip().upper() or "USD"
        self._sdk = sdk
        self._config_factory = config_factory
        self._trade_context_factory = trade_context_factory
        self._quote_context_factory = quote_context_factory
        self._fx_service = fx_service
        self._live_positions = live_positions
        self._poll_interval_s = poll_interval_s
        self._reconnect_delays: Sequence[float] = tuple(
            reconnect_delays if reconnect_delays is not None else _DEFAULT_RECONNECT_DELAYS
        )
        self._trade_ctx: Any | None = None
        self._quote_ctx: Any | None = None
        self._connection_state = ConnectionState.DISCONNECTED
        self._poll_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._current_backoff_delay: float | None = None
        self._last_positions: list[Position] = []

    async def start(self) -> None:
        try:
            await self.connect()
        except LongbridgeConfigError as exc:
            _LOG.error("Longbridge OpenAPI configuration invalid; adapter disabled: %s", exc)
            await self._close_contexts()
            self._connection_state = ConnectionState.DISCONNECTED
            self._current_backoff_delay = None
        except Exception as exc:
            _LOG.warning("Longbridge OpenAPI connect failed; entering reconnect loop: %s", exc)
            await self._close_contexts()
            self._enter_reconnecting()

    async def connect(self) -> None:
        if self._reconnect_task is not asyncio.current_task():
            await self._stop_reconnecting()
        await self._stop_polling()
        await self._close_contexts()
        sdk = self._get_sdk()
        config = self._build_config(sdk)
        self._trade_ctx = self._build_trade_context(sdk, config)
        self._quote_ctx = self._build_quote_context(sdk, config)
        try:
            positions = await self._fetch_positions()
        except Exception:
            await self._close_contexts()
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
        await self._close_contexts()
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
        balance = await self._base_balance()
        if balance is None:
            return []
        currency = _text(getattr(balance, "currency", None), self._base_currency).upper()
        nlv_native = _safe_float(getattr(balance, "net_assets", 0))
        cash_native = _safe_float(getattr(balance, "total_cash", 0))
        buying_power_native = _safe_float(getattr(balance, "buy_power", 0))
        gross_native = max(0.0, nlv_native - cash_native)
        return [
            AccountSummary(
                broker=self.name,
                account_id=self._account_id,
                base_currency=currency,
                net_liquidation_usd=self._to_usd(nlv_native, currency),
                cash_usd=self._to_usd(cash_native, currency),
                buying_power_usd=self._to_usd(buying_power_native, currency),
                net_liquidation_native=nlv_native,
                gross_position_value_usd=self._to_usd(gross_native, currency),
            )
        ]

    async def _fetch_positions(self) -> list[Position]:
        if self._trade_ctx is None or self._quote_ctx is None:
            return []
        stocks: list[Any] = []
        symbols: list[str] = []
        for row in await self._stock_position_rows():
            row_symbol = str(getattr(row, "symbol", "")).strip()
            try:
                _validate_symbol_for_lookup(row_symbol)
            except LongbridgeDataError as exc:
                _LOG.warning(
                    "Skipping invalid Longbridge stock position %s: %s",
                    row_symbol or "<unknown>",
                    exc,
                )
                continue
            stocks.append(row)
            symbols.append(row_symbol)
        quote_by_symbol, quote_failed = await self._quotes_by_symbol(symbols)
        static_by_symbol = await self._static_by_symbol(symbols)

        out: list[Position] = []
        for row in stocks:
            row_symbol = str(getattr(row, "symbol", "")).strip()
            if row_symbol in quote_failed:
                _LOG.warning(
                    "Skipping Longbridge stock position %s after quote lookup failure",
                    row_symbol,
                )
                continue
            try:
                position = self._position_from_stock_row(
                    row,
                    quote_by_symbol.get(row_symbol),
                    static_by_symbol.get(row_symbol),
                )
            except LongbridgeDataError as exc:
                _LOG.warning(
                    "Skipping invalid Longbridge stock position %s: %s",
                    row_symbol or "<unknown>",
                    exc,
                )
                continue
            if position is not None:
                out.append(position)
        out.extend(await self._cash_positions())
        return out

    async def _stock_position_rows(self) -> list[Any]:
        if self._trade_ctx is None:
            return []
        response = await self._trade_ctx.stock_positions()
        channels = list(getattr(response, "channels", []) or [])
        if not channels:
            return []
        if self._position_channel_key:
            matches = [
                channel
                for channel in channels
                if _normalise_channel(_channel_name(channel)) == self._position_channel_key
            ]
            if len(matches) != 1:
                raise LongbridgeConfigError(
                    "Longbridge position channel "
                    f"{self._position_channel!r} matched {len(matches)} channels; "
                    f"available channels: {_format_channel_names(channels)}"
                )
            channels = matches
        if len(channels) > 1:
            raise LongbridgeConfigError(
                "Longbridge returned multiple stock position channels; set "
                "LONGBRIDGE_POSITION_CHANNEL to one of: "
                f"{_format_channel_names(channels)}"
            )
        return [
            row
            for row in list(getattr(channels[0], "positions", []) or [])
            if _safe_float(getattr(row, "quantity", 0)) != 0.0
        ]

    async def _quotes_by_symbol(self, symbols: list[str]) -> tuple[dict[str, Any], set[str]]:
        if not symbols or self._quote_ctx is None:
            return {}, set()
        try:
            quotes = await self._quote_ctx.quote(symbols)
            return _rows_by_symbol(quotes), set()
        except Exception as exc:
            _LOG.warning("Longbridge quote batch failed; retrying per symbol: %s", exc)
            return await self._quotes_by_symbol_individually(symbols, exc)

    async def _static_by_symbol(self, symbols: list[str]) -> dict[str, Any]:
        if not symbols or self._quote_ctx is None:
            return {}
        try:
            infos = await self._quote_ctx.static_info(symbols)
            return _rows_by_symbol(infos)
        except Exception as exc:
            _LOG.warning("Longbridge static info batch failed; retrying per symbol: %s", exc)
        out: dict[str, Any] = {}
        for symbol in symbols:
            try:
                infos = await self._quote_ctx.static_info([symbol])
            except Exception as exc:
                _LOG.warning(
                    "Longbridge static info lookup failed for %s; using row fallback: %s",
                    symbol,
                    exc,
                )
                continue
            out.update(_rows_by_symbol(infos))
        return out

    async def _quotes_by_symbol_individually(
        self, symbols: list[str], batch_exc: Exception,
    ) -> tuple[dict[str, Any], set[str]]:
        if self._quote_ctx is None:
            return {}, set(symbols)
        out: dict[str, Any] = {}
        failed: set[str] = set()
        for symbol in symbols:
            try:
                quotes = await self._quote_ctx.quote([symbol])
            except Exception as exc:
                _LOG.warning("Longbridge quote lookup failed for %s: %s", symbol, exc)
                failed.add(symbol)
                continue
            out.update(_rows_by_symbol(quotes))
        if failed == set(symbols):
            raise batch_exc
        return out, failed

    def _position_from_stock_row(
        self,
        row: Any,
        quote: Any | None,
        static: Any | None,
    ) -> Position | None:
        native_key = str(getattr(row, "symbol", "") or "").strip()
        if not native_key:
            return None
        quantity = _safe_float(getattr(row, "quantity", 0))
        if quantity == 0.0:
            return None
        native_symbol, canonical_symbol, exchange = _normalise_symbol(
            native_key,
            _text(getattr(static, "exchange", None), ""),
        )
        currency = _text(
            getattr(static, "currency", None),
            _text(getattr(row, "currency", None), "USD"),
        ).upper()
        name_en = _text(
            getattr(static, "name_en", None),
            _text(getattr(row, "symbol_name", None), native_key),
        )
        avg_cost = _safe_float(getattr(row, "cost_price", 0))
        last_price = _safe_float(getattr(quote, "last_done", 0)) if quote is not None else 0.0
        previous_close = (
            _safe_float(getattr(quote, "prev_close", 0)) if quote is not None else 0.0
        )
        market_value_native = quantity * last_price
        pnl_native = (last_price - avg_cost) * quantity if last_price != 0.0 else 0.0
        conv = self._convert_to_usd(currency, market_value_native, pnl_native)
        return Position(
            broker=self.name,
            account_id=self._account_id,
            native_key=native_key,
            canonical_symbol=canonical_symbol,
            native_symbol=native_symbol,
            exchange=exchange,
            currency=currency,
            name_en=name_en,
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

    async def _cash_positions(self) -> list[Position]:
        balances = await self._account_balance_rows()
        amounts: dict[str, float] = {}
        for balance in balances:
            for cash in list(getattr(balance, "cash_infos", []) or []):
                currency = _text(getattr(cash, "currency", None), "").upper()
                if not currency:
                    continue
                amount = (
                    _safe_float(getattr(cash, "available_cash", 0))
                    + _safe_float(getattr(cash, "frozen_cash", 0))
                    + _safe_float(getattr(cash, "settling_cash", 0))
                )
                if amount == 0.0:
                    continue
                amounts[currency] = amounts.get(currency, 0.0) + amount
        out: list[Position] = []
        for currency, amount in sorted(amounts.items()):
            conv = self._convert_to_usd(currency, amount, 0.0)
            out.append(
                Position(
                    broker=self.name,
                    account_id=self._account_id,
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

    async def _account_balance_rows(self) -> list[Any]:
        if self._trade_ctx is None:
            return []
        return list(await self._trade_ctx.account_balance(currency=self._base_currency))

    async def _base_balance(self) -> Any | None:
        balances = await self._account_balance_rows()
        if not balances:
            return None
        for balance in balances:
            if _text(getattr(balance, "currency", None), "").upper() == self._base_currency:
                return balance
        return balances[0]

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
            _LOG.warning("Invalid FX currency on Longbridge position: %s", currency)
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
                _LOG.warning("Longbridge position refresh failed: %s", exc)
                if self._connection_state is not ConnectionState.CONNECTED:
                    return

    def _enter_reconnecting(self) -> None:
        if self._connection_state is ConnectionState.RECONNECTING:
            return
        self._connection_state = ConnectionState.RECONNECTING
        if not self._reconnect_delays:
            self._connection_state = ConnectionState.DISCONNECTED
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
                    _LOG.info("Longbridge reconnect attempt %d succeeded", attempt)
                    self._current_backoff_delay = None
                    return
                except asyncio.CancelledError:
                    raise
                except LongbridgeConfigError as exc:
                    _LOG.error("Longbridge configuration invalid; stopping reconnect: %s", exc)
                    self._connection_state = ConnectionState.DISCONNECTED
                    self._current_backoff_delay = None
                    return
                except Exception as exc:
                    _LOG.warning(
                        "Longbridge reconnect attempt %d failed: %s",
                        attempt,
                        exc,
                    )
                    self._connection_state = ConnectionState.RECONNECTING
                    continue
            _LOG.error(
                "Longbridge reconnect exhausted after %d attempts; staying DISCONNECTED",
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

    async def _close_contexts(self) -> None:
        contexts = [ctx for ctx in (self._trade_ctx, self._quote_ctx) if ctx is not None]
        self._trade_ctx = None
        self._quote_ctx = None
        for ctx in contexts:
            close = getattr(ctx, "close", None)
            if not callable(close):
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                _LOG.debug("Longbridge context close failed: %s", exc)

    def _get_sdk(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        try:
            self._sdk = importlib.import_module("longbridge.openapi")
            return self._sdk
        except ImportError as exc:
            raise LongbridgeConfigError(
                "Longbridge SDK not installed; install longbridge>=4.2.1"
            ) from exc

    def _build_config(self, sdk: Any) -> Any:
        if self._config_factory is not None:
            try:
                return self._config_factory()
            except Exception as exc:
                raise LongbridgeConfigError(str(exc)) from exc
        config = getattr(sdk, "Config", None)
        factory = getattr(config, "from_apikey_env", None)
        if not callable(factory):
            raise LongbridgeConfigError(
                "Longbridge SDK Config.from_apikey_env unavailable"
            )
        try:
            return factory()
        except Exception as exc:
            raise LongbridgeConfigError(str(exc)) from exc

    def _build_trade_context(self, sdk: Any, config: Any) -> Any:
        if self._trade_context_factory is not None:
            return self._trade_context_factory(config)
        context = getattr(sdk, "AsyncTradeContext", None)
        factory = getattr(context, "create", None)
        if not callable(factory):
            raise LongbridgeConfigError(
                "Longbridge SDK AsyncTradeContext.create unavailable"
            )
        return factory(config)

    def _build_quote_context(self, sdk: Any, config: Any) -> Any:
        if self._quote_context_factory is not None:
            return self._quote_context_factory(config)
        context = getattr(sdk, "AsyncQuoteContext", None)
        factory = getattr(context, "create", None)
        if not callable(factory):
            raise LongbridgeConfigError(
                "Longbridge SDK AsyncQuoteContext.create unavailable"
            )
        return factory(config)


def _normalise_symbol(symbol: str, exchange_hint: str = "") -> tuple[str, str, str]:
    native = symbol.strip()
    if "." not in native:
        raise LongbridgeDataError(f"unknown Longbridge symbol format: {symbol!r}")
    raw_symbol, suffix = native.rsplit(".", 1)
    suffix = suffix.upper()
    native_symbol = raw_symbol.lstrip("0") if suffix == "HK" else raw_symbol
    native_symbol = native_symbol or "0"
    canonical = f"{native_symbol}.{suffix}"
    exchange = exchange_hint.strip().upper()
    if not exchange:
        try:
            exchange = _SUFFIX_TO_EXCHANGE[suffix]
        except KeyError:
            raise LongbridgeDataError(
                f"unknown Longbridge symbol suffix: {suffix!r}"
            ) from None
    return native_symbol, canonical, exchange


def _validate_symbol_for_lookup(symbol: str) -> None:
    native = symbol.strip()
    if "." not in native:
        raise LongbridgeDataError(f"unknown Longbridge symbol format: {symbol!r}")
    raw_symbol, suffix = native.rsplit(".", 1)
    if not raw_symbol.strip() or not suffix.strip():
        raise LongbridgeDataError(f"unknown Longbridge symbol format: {symbol!r}")
    if suffix.upper() not in _SUFFIX_TO_EXCHANGE:
        raise LongbridgeDataError(f"unknown Longbridge symbol suffix: {suffix!r}")


def _rows_by_symbol(rows: Sequence[Any]) -> dict[str, Any]:
    return {str(getattr(row, "symbol", "")).strip(): row for row in rows}


def _channel_name(channel: Any) -> str:
    for attr in ("account_channel", "channel", "account_channel_type", "name", "id"):
        value = getattr(channel, attr, None)
        text = str(value).strip() if value is not None else ""
        if text:
            return text
    return ""


def _normalise_channel(value: str) -> str:
    return str(value or "").strip().upper()


def _format_channel_names(channels: Sequence[Any]) -> str:
    names = [_channel_name(channel) or "<unknown>" for channel in channels]
    return ", ".join(names) if names else "<none>"


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default
