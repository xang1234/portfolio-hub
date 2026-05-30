"""Pure Tiger OpenAPI payload mapping helpers."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.adapters.tiger_config import TigerConfigError
from app.core.symbols import IB_EXCHANGE_TO_SUFFIX, canonical_symbol


_SUPPORTED_ACCOUNT_TYPES = frozenset({"GLOBAL", "STANDARD", "PAPER"})

_MARKET_TO_EXCHANGE: dict[str, str] = {
    "US": "NYSE",
    "HK": "SEHK",
    "SG": "SGX",
    "AU": "ASX",
}


@dataclass(frozen=True)
class TigerAccountRef:
    account_id: str
    account_type: str


@dataclass(frozen=True)
class TigerAssetSnapshot:
    account: TigerAccountRef
    currency: str
    net_liquidation_native: float
    cash_native: float
    buying_power_native: float
    gross_position_value_native: float
    cash_amounts: Mapping[str, float]


class TigerDataError(RuntimeError):
    """A Tiger payload row cannot be mapped into dashboard data."""


def account_is_active(profile: Any) -> bool:
    status = normalise_enum_text(getattr(profile, "status", ""))
    return status in ("", "FUNDED", "OPEN")


def account_type(profile: Any, account_id: str) -> str:
    value = normalise_enum_text(getattr(profile, "account_type", ""))
    if not value:
        raise TigerConfigError(f"Tiger account {account_id!r} is missing account_type")
    if value not in _SUPPORTED_ACCOUNT_TYPES:
        raise TigerConfigError(
            f"Unsupported Tiger account_type {value!r} for account {account_id!r}"
        )
    return value


def normalise_contract_symbol(
    *,
    symbol: str,
    market: str,
    exchange_hint: str,
) -> tuple[str, str, str]:
    raw_symbol = symbol.strip()
    if not raw_symbol:
        raise TigerDataError("position contract missing symbol")
    exchange = primary_exchange_for_contract(
        raw_symbol=raw_symbol,
        market=market,
        exchange_hint=exchange_hint,
    )
    native_symbol = (
        raw_symbol.lstrip("0")
        if IB_EXCHANGE_TO_SUFFIX[exchange] == "HK"
        else raw_symbol
    )
    native_symbol = native_symbol or "0"
    try:
        canonical = canonical_symbol(native_symbol, exchange)
    except ValueError as exc:
        raise TigerDataError(str(exc)) from exc
    return native_symbol, canonical, exchange


def primary_exchange_for_contract(
    *,
    raw_symbol: str,
    market: str,
    exchange_hint: str,
) -> str:
    exchange = exchange_hint.strip().upper()
    if exchange in IB_EXCHANGE_TO_SUFFIX:
        return exchange
    if market == "CN":
        return "SSE" if raw_symbol.startswith("6") else "SZSE"
    try:
        return _MARKET_TO_EXCHANGE[market]
    except KeyError:
        raise TigerDataError(f"unknown Tiger market {market!r}") from None


def prime_cash_amounts(segment: Any) -> dict[str, float]:
    currency_assets = getattr(segment, "currency_assets", None) or {}
    out: dict[str, float] = {}
    for currency, asset in dict(currency_assets).items():
        code = text(getattr(asset, "currency", None), str(currency)).upper()
        out[code] = out.get(code, 0.0) + safe_float(
            getattr(asset, "cash_balance", 0)
        )
    return out


def global_cash_amounts(assets: Any) -> dict[str, float]:
    values = (
        getattr(assets, "market_values", None)
        or getattr(assets, "market_value", None)
        or {}
    )
    out: dict[str, float] = {}
    for currency, value in dict(values).items():
        code = text(getattr(value, "currency", None), str(currency)).upper()
        out[code] = out.get(code, 0.0) + safe_float(
            getattr(value, "cash_balance", 0)
        )
    return out


def security_segment(assets: Any | None) -> Any | None:
    if assets is None:
        return None
    segments = getattr(assets, "segments", None) or {}
    if not isinstance(segments, dict):
        return None
    return segments.get("S") or segments.get("SEC")


def records(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    to_dict = getattr(data, "to_dict", None)
    if callable(to_dict):
        return list(to_dict("records"))
    if isinstance(data, list):
        return [dict(item) for item in data]
    return []


def enum_value(sdk: Any, container: str, name: str) -> Any:
    enum = getattr(sdk, container, None)
    if enum is None:
        return name
    return getattr(enum, name, name)


def normalise_enum_text(value: Any) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if name:
        return str(name).upper()
    return str(value).strip().upper()


def safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def first_attr_float(obj: Any, *attrs: str) -> float:
    for attr in attrs:
        value = getattr(obj, attr, None)
        if value in (None, "", "N/A"):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def first_float(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = row.get(key)
        if value in (None, "", "N/A"):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def text(value: Any, default: str) -> str:
    if value is None:
        return default
    text_value = str(value).strip()
    return text_value or default
