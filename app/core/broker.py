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
    # FX-rate metadata — defaults preserve back-compat for test fixtures.
    fx_is_stale: bool = False         # IB rate older than 60s during market hours → ⚠️
    fx_is_fallback: bool = False      # rate came from public-API fallback → 📡
    fx_unavailable: bool = False      # no rate found at all → render —
    # When the live ticker had no last/close, last_price was filled from
    # reqHistoricalData (daily-bar close). Template renders a "prev close"
    # subtext so users know the number isn't ticking live.
    last_price_is_previous_close: bool = False
    # Set during the reconnect window (broker dropped, last tick is from
    # before the disconnect). Row template renders ⚠️ + reduced opacity.
    # Naturally clears when the next live tick replaces the Position.
    last_price_is_stale: bool = False
    # IB's price-unit divisor. For most contracts = 1. For pence-quoted UK
    # equities (e.g. IQE on LSE) = 100: IB returns last/avgCost in pence,
    # so we divide by 100 to compute mv_native/pnl_native in pounds.
    price_magnifier: int = 1
    # Previous-session close in native currency. Used to compute the
    # intraday change % and the hero's "today" P&L. 0.0 when we have no
    # value (cash rows, IBKR adapter hasn't backfilled yet, or the
    # historical fetch failed). Default keeps test fixtures compatible.
    previous_close: float = 0.0

    @property
    def intraday_change_pct(self) -> float | None:
        """% change of last_price vs previous_close, in native currency.

        Returns None when:
          - no previous_close on file,
          - no live last_price,
          - last_price was itself filled FROM previous_close (the
            unsubscribed-market fallback), where a 0 % delta would be
            misleading noise rather than a real reading.
        """
        if self.previous_close <= 0 or self.last_price <= 0:
            return None
        if self.last_price_is_previous_close:
            return None
        return (self.last_price - self.previous_close) / self.previous_close * 100.0

    @property
    def intraday_pnl_usd(self) -> float:
        """Today's contribution to USD P&L: (last - prev_close) * qty / mag,
        converted to USD by the same FX the row used for mv_usd.

        Returns 0.0 when we can't compute (no prev close, no live tick,
        FX unavailable, or the prev-close fallback supplied last_price).
        Backing out FX from mv_usd / mv_native preserves whatever rate the
        adapter actually applied — including the fallback rate from
        FxService — so the hero "Today" total is consistent with the row.
        """
        if (
            self.previous_close <= 0
            or self.last_price <= 0
            or self.fx_unavailable
            or self.last_price_is_previous_close
            or self.market_value_native == 0
        ):
            return 0.0
        fx = self.market_value_usd / self.market_value_native
        return (
            (self.last_price - self.previous_close)
            * self.quantity
            / self.price_magnifier
            * fx
        )


@dataclass
class AccountSummary:
    broker: str
    account_id: str
    base_currency: str                # IB lets each account set its own base
    net_liquidation_usd: float
    cash_usd: float
    buying_power_usd: float
    # Slice 10: equity_snapshots needs both the raw native NLV (the
    # number IB reports in `base_currency`) and the gross stock-position
    # value in USD for the future equity-curve / TWR / XIRR computations.
    # Defaults preserve back-compat for stubs that pre-date slice 10.
    net_liquidation_native: float = 0.0
    gross_position_value_usd: float = 0.0


@runtime_checkable
class Broker(Protocol):
    name: str

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_connected(self) -> bool: ...

    async def get_connection_state(self) -> ConnectionState: ...

    # Optional adapter capability (NOT part of the Protocol's required
    # surface — adding it here would force every adapter to implement it
    # and would break @runtime_checkable isinstance checks for stubs that
    # omit it). Production callers duck-type with
    #   getattr(broker, "current_backoff_delay", None)
    # and IbkrAdapter implements it. Future adapters opt in by adding:
    #   def current_backoff_delay(self) -> float | None: ...
    # Returns the seconds the reconnect loop is currently sleeping while
    # in RECONNECTING state, else None. Drives the badge text
    # "🟡 IBKR reconnecting (5s)" / "(15s)" / "(60s)".

    async def get_positions(self) -> list[Position]: ...

    async def get_account_summary(self) -> list[AccountSummary]: ...
