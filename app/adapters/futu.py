"""Futu / Moomoo OpenD Broker adapter.

This adapter intentionally implements the read-only surfaces the dashboard
needs: account discovery, positions, and account funds. The official SDK is
synchronous, so calls run through asyncio.to_thread to keep FastAPI's event
loop responsive.
"""

import asyncio
import importlib
import logging
from collections.abc import Callable, Iterable
from typing import Any

from app.core.broker import AccountSummary, ConnectionState, Position
from app.core.fx import FxConversion, FxService
from app.core.live_positions import LivePositions


_LOG = logging.getLogger(__name__)


_FUTU_EXCHANGE_TO_IB: dict[str, str] = {
    "HK": "SEHK",
    "US": "NYSE",
    "SG": "SGX",
    "SH": "SSE",
    "SZ": "SZSE",
    "JP": "TSEJ",
    "AU": "ASX",
    "CA": "TSX",
}

_FUTU_SUFFIX: dict[str, str] = {
    "HK": "HK",
    "US": "US",
    "SG": "SG",
    "SH": "SH",
    "SZ": "SZ",
    "JP": "JP",
    "AU": "AU",
    "CA": "CA",
}


class FutuAdapter:
    name = "Futu"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        markets: Iterable[str] = ("HK",),
        security_firm: str = "FUTUSG",
        trd_env: str = "REAL",
        is_encrypt: bool | None = None,
        refresh_cache: bool = False,
        sdk: Any | None = None,
        context_factory: Callable[..., Any] | None = None,
        fx_service: FxService | None = None,
        live_positions: LivePositions | None = None,
        poll_interval_s: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._markets = tuple(dict.fromkeys(m.strip().upper() for m in markets if m.strip()))
        self._security_firm = security_firm.strip().upper()
        self._trd_env = trd_env.strip().upper()
        self._is_encrypt = is_encrypt
        self._refresh_cache = refresh_cache
        self._sdk = sdk
        self._context_factory = context_factory
        self._fx_service = fx_service
        self._live_positions = live_positions
        self._poll_interval_s = poll_interval_s
        self._contexts: list[Any] = []
        self._account_contexts: dict[str, list[Any]] = {}
        self._connection_state = ConnectionState.DISCONNECTED
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        try:
            await self.connect()
        except Exception as exc:
            _LOG.warning("Futu OpenD connect failed; adapter disabled for now: %s", exc)
            await self._close_contexts()
            self._connection_state = ConnectionState.DISCONNECTED

    async def connect(self) -> None:
        await self._stop_polling()
        await self._close_contexts()
        sdk = self._get_sdk()
        contexts: list[Any] = []
        try:
            for market in self._markets:
                contexts.append(await asyncio.to_thread(self._build_context, sdk, market))
            self._contexts = contexts
            self._account_contexts = {}
            for ctx in contexts:
                await self._load_accounts_from_context(sdk, ctx)
            self._connection_state = ConnectionState.CONNECTED
            await self._refresh_live_positions()
            if self._live_positions is not None and self._poll_interval_s > 0:
                self._poll_task = asyncio.create_task(self._poll_loop())
        except Exception:
            self._contexts = contexts
            self._connection_state = ConnectionState.DISCONNECTED
            await self._close_contexts()
            raise

    async def disconnect(self) -> None:
        await self._stop_polling()
        await self._close_contexts()
        self._connection_state = ConnectionState.DISCONNECTED

    async def is_connected(self) -> bool:
        return self._connection_state is ConnectionState.CONNECTED

    async def get_connection_state(self) -> ConnectionState:
        return self._connection_state

    async def get_positions(self) -> list[Position]:
        if self._connection_state is not ConnectionState.CONNECTED:
            return []
        sdk = self._get_sdk()
        by_key: dict[tuple[str, str, str], Position] = {}
        query_failures: list[str] = []
        for account_id, contexts in self._account_contexts.items():
            for ctx in contexts:
                rows = await self._position_rows_for_context(sdk, account_id, ctx)
                if rows is None:
                    query_failures.append(account_id)
                    continue
                for row in rows:
                    position = self._position_from_row(account_id, row)
                    if position is not None:
                        by_key[
                            (
                                position.broker,
                                position.account_id,
                                position.native_key,
                            )
                        ] = position
        if query_failures:
            raise RuntimeError(
                "Futu position_list_query failed for "
                f"{len(query_failures)} account/market context(s)"
            )
        return list(by_key.values())

    async def _position_rows_for_context(
        self,
        sdk: Any,
        account_id: str,
        ctx: Any,
    ) -> list[dict[str, Any]] | None:
        kwargs = {
            "trd_env": _enum_value(sdk, "TrdEnv", self._trd_env),
            "acc_id": int(account_id),
            "refresh_cache": self._refresh_cache,
        }
        ret, data = await asyncio.to_thread(ctx.position_list_query, **kwargs)
        if not _is_ret_ok(sdk, ret):
            _LOG.warning("Futu position_list_query failed for %s: %s", account_id, data)
            return None
        return _records(data)

    async def get_account_summary(self) -> list[AccountSummary]:
        if self._connection_state is not ConnectionState.CONNECTED:
            return []
        sdk = self._get_sdk()
        out: list[AccountSummary] = []
        for account_id, contexts in self._account_contexts.items():
            if not contexts:
                continue
            ctx = contexts[0]
            kwargs = {
                "trd_env": _enum_value(sdk, "TrdEnv", self._trd_env),
                "acc_id": int(account_id),
            }
            currency = _optional_enum_value(sdk, "Currency", "USD")
            if currency is not None:
                kwargs["currency"] = currency
            ret, data = await asyncio.to_thread(ctx.accinfo_query, **kwargs)
            if not _is_ret_ok(sdk, ret):
                _LOG.warning("Futu accinfo_query failed for %s: %s", account_id, data)
                continue
            rows = _records(data)
            if not rows:
                continue
            out.append(self._summary_from_row(account_id, rows[0]))
        return out

    def _get_sdk(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        for module_name in ("moomoo", "futu"):
            try:
                self._sdk = importlib.import_module(module_name)
                return self._sdk
            except ImportError:
                continue
        raise RuntimeError(
            "Futu/Moomoo SDK not installed; install moomoo-api or futu-api"
        )

    def _build_context(self, sdk: Any, market: str) -> Any:
        factory = self._context_factory or sdk.OpenSecTradeContext
        kwargs = {
            "filter_trdmarket": _enum_value(sdk, "TrdMarket", market),
            "host": self._host,
            "port": self._port,
            "security_firm": _enum_value(sdk, "SecurityFirm", self._security_firm),
        }
        if self._is_encrypt is not None:
            kwargs["is_encrypt"] = self._is_encrypt
        return factory(**kwargs)

    async def _load_accounts_from_context(self, sdk: Any, ctx: Any) -> None:
        ret, data = await asyncio.to_thread(ctx.get_acc_list)
        if not _is_ret_ok(sdk, ret):
            raise RuntimeError(f"get_acc_list failed: {data}")
        for row in _records(data):
            if not _account_is_usable(row, self._trd_env):
                continue
            account_id = _clean_account_id(row.get("acc_id"))
            if account_id:
                contexts = self._account_contexts.setdefault(account_id, [])
                if ctx not in contexts:
                    contexts.append(ctx)

    async def _close_contexts(self) -> None:
        contexts = self._contexts
        self._contexts = []
        self._account_contexts = {}
        for ctx in contexts:
            close = getattr(ctx, "close", None)
            if callable(close):
                try:
                    await asyncio.to_thread(close)
                except Exception as exc:
                    _LOG.debug("Futu context close failed: %s", exc)

    async def _stop_polling(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _refresh_live_positions(self) -> None:
        if self._live_positions is None:
            return
        if self._connection_state is not ConnectionState.CONNECTED:
            return
        try:
            positions = await self.get_positions()
        except Exception:
            self._connection_state = ConnectionState.DISCONNECTED
            raise
        self._live_positions.replace_broker(self.name, positions)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_interval_s)
                await self._refresh_live_positions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.warning("Futu position refresh failed: %s", exc)
                if self._connection_state is ConnectionState.DISCONNECTED:
                    return

    def _position_from_row(self, account_id: str, row: dict[str, Any]) -> Position | None:
        code = str(row.get("code") or "").strip()
        if not code:
            return None
        quantity = _safe_float(row.get("qty"))
        if quantity == 0:
            return None
        try:
            native_symbol, canonical, exchange = _normalise_code(
                code, str(row.get("position_market") or "")
            )
        except ValueError as exc:
            _LOG.warning("Skipping Futu position %s: %s", code, exc)
            return None

        currency = str(row.get("currency") or "USD").upper()
        last_price = _first_float(row, "nominal_price", "last_price")
        market_value_native = _first_float(row, "market_val", "market_value")
        if market_value_native == 0.0 and last_price != 0.0:
            market_value_native = quantity * last_price
        if last_price == 0.0 and quantity != 0.0:
            last_price = market_value_native / quantity

        avg_cost = _first_float(row, "average_cost", "cost_price", "diluted_cost")
        pnl_native = _first_float(row, "unrealized_pl", "pl_val")
        if pnl_native == 0.0 and avg_cost != 0.0 and last_price != 0.0:
            pnl_native = (last_price - avg_cost) * quantity

        conv = self._convert_to_usd(currency, market_value_native, pnl_native)
        return Position(
            broker=self.name,
            account_id=account_id,
            native_key=code,
            canonical_symbol=canonical,
            native_symbol=native_symbol,
            exchange=exchange,
            currency=currency,
            name_en=str(row.get("stock_name") or code),
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
        )

    def _summary_from_row(self, account_id: str, row: dict[str, Any]) -> AccountSummary:
        currency = str(row.get("currency") or "USD").upper()
        nlv_native = _first_float(row, "total_assets", "net_assets")
        cash_native = _first_float(row, "cash", "us_cash")
        buying_power_native = _first_float(row, "power", "max_power")
        market_value_native = _first_float(row, "market_val", "securities_assets")
        return AccountSummary(
            broker=self.name,
            account_id=account_id,
            base_currency=currency,
            net_liquidation_usd=self._to_usd(nlv_native, currency),
            cash_usd=self._to_usd(cash_native, currency),
            buying_power_usd=self._to_usd(buying_power_native, currency),
            net_liquidation_native=nlv_native,
            gross_position_value_usd=self._to_usd(market_value_native, currency),
        )

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
            _LOG.warning("Invalid FX currency on Futu position: %s", currency)
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


def _records(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    to_dict = getattr(data, "to_dict", None)
    if callable(to_dict):
        return list(to_dict("records"))
    if isinstance(data, list):
        return [dict(item) for item in data]
    return []


def _enum_value(sdk: Any, container: str, name: str) -> Any:
    enum = getattr(sdk, container, None)
    if enum is None:
        return name
    return getattr(enum, name, name)


def _optional_enum_value(sdk: Any, container: str, name: str) -> Any | None:
    enum = getattr(sdk, container, None)
    if enum is None:
        return None
    return getattr(enum, name, None)


def _is_ret_ok(sdk: Any, ret: Any) -> bool:
    return ret == getattr(sdk, "RET_OK", 0)


def _account_is_usable(row: dict[str, Any], trd_env: str) -> bool:
    env = _normalise_enum_text(row.get("trd_env"))
    if env and env != trd_env:
        return False
    status = _normalise_enum_text(row.get("acc_status"))
    return status in ("", "ACTIVE")


def _normalise_enum_text(value: Any) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if name:
        return str(name).upper()
    return str(value).strip().upper()


def _clean_account_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _normalise_code(code: str, market_hint: str) -> tuple[str, str, str]:
    if "." in code:
        prefix, raw_symbol = code.split(".", 1)
    else:
        prefix, raw_symbol = market_hint, code
    market = (prefix or market_hint).upper()
    if market == "CN":
        market = "SH" if raw_symbol.startswith("6") else "SZ"
    try:
        exchange = _FUTU_EXCHANGE_TO_IB[market]
        suffix = _FUTU_SUFFIX[market]
    except KeyError:
        raise ValueError(f"unknown Futu market {market!r}") from None
    native_symbol = raw_symbol.lstrip("0") if market == "HK" else raw_symbol
    native_symbol = native_symbol or "0"
    return native_symbol, f"{native_symbol}.{suffix}", exchange


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first_float(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = row.get(key)
        if value in (None, "", "N/A"):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0
