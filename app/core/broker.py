"""Broker abstraction layer.

The Broker Protocol is the keystone of multi-broker support. Each adapter
(IBKR in v1, Futu/Tiger/Longbridge later) implements this contract; the rest
of the app operates on the normalized Position / AccountSummary types and
never touches vendor SDKs directly.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class ConnectionState(str, Enum):
    """The broker connection's state from the dashboard's perspective.

    CONNECTED:    gateway responsive, market data flowing
    RECONNECTING: lost the gateway, auto-retrying with exponential backoff
    DISCONNECTED: not connected (initial state or after backoff exhausted)
    """

    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DISCONNECTED = "DISCONNECTED"


@dataclass
class Position:
    broker: str                       # "IBKR" | "Futu" | "Tiger" | "Longbridge"
    account_id: str                   # broker-specific account identifier (IB: "U1234567")
    native_key: str                   # broker-stable instrument PK (IB: conId as str)
    canonical_symbol: str             # Longbridge-style "<native>.<country>" e.g. "700.HK"
    native_symbol: str                # broker-native symbol e.g. "700"
    exchange: str                     # IB primary exchange code e.g. "SEHK"
    currency: str                     # ISO 4217 (IB returns "CNH" for offshore renminbi)
    name_en: str                      # resolved English company name
    asset_class: str                  # "STK" | "CASH" (ETF arrives as STK)
    quantity: float
    avg_cost: float                   # native currency
    last_price: float                 # native currency
    market_value_native: float
    market_value_usd: float           # 0.0 when fx_unavailable=True
    unrealized_pnl_native: float
    unrealized_pnl_usd: float         # 0.0 when fx_unavailable=True
    # Slice 3 FX metadata — defaults keep older test fixtures working
    fx_is_stale: bool = False         # IB rate older than 60s during market hours → ⚠️
    fx_is_fallback: bool = False      # rate came from public-API fallback → 📡
    fx_unavailable: bool = False      # no rate found at all → render —
    # When the live ticker had no last/close, last_price was filled from
    # reqHistoricalData (daily-bar close). Template renders a "prev close"
    # subtext so users know the number isn't ticking live.
    last_price_is_previous_close: bool = False
    # IB's price-unit divisor. For most contracts = 1. For pence-quoted UK
    # equities (e.g. IQE on LSE) = 100: IB returns last/avgCost in pence,
    # so we divide by 100 to compute mv_native/pnl_native in pounds.
    price_magnifier: int = 1


@dataclass
class AccountSummary:
    broker: str
    account_id: str
    base_currency: str                # IB lets each account set its own base
    net_liquidation_usd: float
    cash_usd: float
    buying_power_usd: float


@runtime_checkable
class Broker(Protocol):
    name: str

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_connected(self) -> bool: ...

    async def get_connection_state(self) -> ConnectionState: ...

    async def get_positions(self) -> list[Position]: ...

    async def get_account_summary(self) -> list[AccountSummary]: ...
