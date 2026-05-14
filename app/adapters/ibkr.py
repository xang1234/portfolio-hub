"""IBKR concrete Broker adapter.

Slice 2 surface:
  - connect / disconnect / is_connected (from slice 1)
  - get_positions(): STK rows only, with English name resolution and primary
    exchange derivation via reqContractDetails. CASH support arrives in slice 6.
  - get_account_summary(): minimal stub returning a list with one entry per
    distinct account_id seen in positions. Slice 7 expands this with real NLV.

Out of scope for this slice:
  - FX conversion to USD (slice 3) — USD fields remain 0.0
  - Live updates / market data subscriptions (slice 4)
  - Reconnection on disconnect (slice 9)
"""

import logging
from typing import Callable, Protocol

from app.core.broker import AccountSummary, Position
from app.core.names import NameResolver
from app.core.symbols import canonical_symbol
from app.db.store import Store


_LOG = logging.getLogger(__name__)


class _IBLike(Protocol):
    async def connectAsync(self, host: str, port: int, clientId: int) -> None: ...

    def disconnect(self) -> None: ...

    def isConnected(self) -> bool: ...

    async def reqPositionsAsync(self): ...

    async def reqContractDetailsAsync(self, contract): ...

    async def reqTickersAsync(self, *contracts): ...


def _default_ib_factory() -> _IBLike:
    from ib_async import IB

    return IB()


class IbkrAdapter:
    name = "IBKR"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        client_id: int,
        ib_factory: Callable[[], _IBLike] = _default_ib_factory,
        store: Store | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib_factory = ib_factory
        self._store = store
        self._ib: _IBLike | None = None
        self._name_resolver: NameResolver | None = None

    # ---- lifecycle (slice 1) -------------------------------------------------

    async def connect(self) -> None:
        ib = self._ib_factory()
        await ib.connectAsync(self._host, self._port, clientId=self._client_id)
        # Use delayed-frozen market data so accounts without live subscriptions
        # still get a previous-close price for held positions. Live data still
        # arrives for instruments the account IS subscribed to.
        req_market_data_type = getattr(ib, "reqMarketDataType", None)
        if callable(req_market_data_type):
            try:
                req_market_data_type(4)
            except Exception:
                pass
        self._ib = ib
        if self._store is not None:
            self._name_resolver = NameResolver(
                store=self._store, fetcher=self._fetch_contract_details
            )

    async def disconnect(self) -> None:
        if self._ib is None:
            return
        self._ib.disconnect()
        self._ib = None

    async def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    # ---- positions (slice 2) -------------------------------------------------

    async def get_positions(self) -> list[Position]:
        if self._ib is None:
            return []
        ib_positions = await self._ib.reqPositionsAsync()
        stk_positions = [p for p in ib_positions if p.contract.secType == "STK"]
        if not stk_positions:
            return []

        # Snapshot last prices in one round-trip rather than one per row
        contracts = [p.contract for p in stk_positions]
        tickers = await self._ib.reqTickersAsync(*contracts)
        last_prices = {c.conId: _coerce_last(t) for c, t in zip(contracts, tickers)}

        out: list[Position] = []
        for ib_pos in stk_positions:
            position = await self._build_position(ib_pos, last_prices)
            if position is not None:
                out.append(position)
        return out

    async def _build_position(
        self,
        ib_pos,
        last_prices: dict[int, float],
    ) -> Position | None:
        contract = ib_pos.contract
        native_key = str(contract.conId)

        resolved = await self._resolve_name(native_key, contract)
        if resolved is None:
            return None
        canonical, name_en, primary_exchange = resolved

        last_price = last_prices.get(contract.conId, 0.0)
        quantity = float(ib_pos.position)
        avg_cost = float(ib_pos.avgCost)
        mv_native = quantity * last_price
        pnl_native = (last_price - avg_cost) * quantity

        return Position(
            broker=self.name,
            account_id=ib_pos.account,
            native_key=native_key,
            canonical_symbol=canonical,
            native_symbol=contract.symbol,
            exchange=primary_exchange,
            currency=contract.currency,
            name_en=name_en,
            asset_class="STK",
            quantity=quantity,
            avg_cost=avg_cost,
            last_price=last_price,
            market_value_native=mv_native,
            market_value_usd=0.0,        # filled in slice 3
            unrealized_pnl_native=pnl_native,
            unrealized_pnl_usd=0.0,      # filled in slice 3
        )

    async def _resolve_name(
        self,
        native_key: str,
        contract,
    ) -> tuple[str, str, str] | None:
        """Return (canonical_symbol, name_en, primary_exchange) or None to skip.

        Uses the NameResolver cache when a Store was injected; otherwise fetches
        on every call (acceptable for unit tests but not for production).
        """
        if self._name_resolver is not None:
            cached = await self._name_resolver.resolve(self.name, native_key)
            if cached is not None:
                # cache stores (canonical_symbol, name_en) — but we still need
                # the primary_exchange. Recover it from canonical_symbol's suffix.
                canonical, name_en = cached
                primary_exchange = _primary_exchange_from_canonical(canonical)
                if primary_exchange is None:
                    return None
                return canonical, name_en, primary_exchange

        fetched = await self._fetch_contract_details(self.name, native_key, contract=contract)
        if fetched is None:
            return None
        canonical, name_en = fetched
        primary_exchange = _primary_exchange_from_canonical(canonical)
        if primary_exchange is None:
            return None
        if self._store is not None:
            await self._store.put_name_cache(self.name, native_key, canonical, name_en)
        return canonical, name_en, primary_exchange

    async def _fetch_contract_details(
        self,
        broker: str,
        native_key: str,
        contract=None,
    ) -> tuple[str, str] | None:
        """Fetcher passed to NameResolver. Calls reqContractDetailsAsync and
        returns (canonical_symbol, name_en) or None."""
        if self._ib is None:
            return None
        if contract is None:
            # Cache-driven path: NameResolver hands us only the native_key.
            # We can't reconstruct the Contract object — return None so the
            # caller falls back to its own contract reference.
            return None

        details_list = await self._ib.reqContractDetailsAsync(contract)
        if not details_list:
            _LOG.warning("reqContractDetails returned no details for conId=%s", native_key)
            return None

        details = details_list[0]
        primary_exchange = getattr(details.contract, "primaryExchange", "") or ""
        if not primary_exchange:
            _LOG.warning(
                "Contract conId=%s has no primaryExchange; dropping (would produce ambiguous canonical_symbol)",
                native_key,
            )
            return None

        try:
            canonical = canonical_symbol(contract.symbol, primary_exchange)
        except ValueError:
            _LOG.warning(
                "Unknown primary_exchange %r for conId=%s; dropping",
                primary_exchange,
                native_key,
            )
            return None

        name_en = (getattr(details, "longName", "") or "").strip()
        return canonical, name_en

    # ---- account summary (slice 2 minimum; slice 7 fleshes out) -------------

    async def get_account_summary(self) -> list[AccountSummary]:
        positions = await self.get_positions() if self._ib is not None else []
        account_ids = {p.account_id for p in positions} or {"UNKNOWN"}
        return [
            AccountSummary(
                broker=self.name,
                account_id=acc,
                base_currency="USD",       # slice 7 will pull this from accountValues()
                net_liquidation_usd=0.0,
                cash_usd=0.0,
                buying_power_usd=0.0,
            )
            for acc in sorted(account_ids)
        ]


# Helpers ---------------------------------------------------------------------


def _coerce_last(ticker) -> float:
    """Extract a usable last-price float from an ib_async Ticker (or test double).

    Falls back through: ticker.last -> ticker.close -> ticker.marketPrice() -> 0.0.
    Treats None, NaN, and IB's "no quote" sentinel (-1.0) as missing.

    Slice 2 needs *some* number to render; live ticking arrives in slice 4.
    """
    for value in (
        getattr(ticker, "last", None),
        getattr(ticker, "close", None),
    ):
        coerced = _to_positive_float(value)
        if coerced is not None:
            return coerced

    market_price = getattr(ticker, "marketPrice", None)
    if callable(market_price):
        try:
            coerced = _to_positive_float(market_price())
            if coerced is not None:
                return coerced
        except Exception:
            pass
    return 0.0


def _to_positive_float(value) -> float | None:
    """Return a positive float, or None if the value is missing / NaN / sentinel."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:           # NaN
        return None
    if f <= 0:           # IB's "no quote" sentinel is -1.0; 0 also unusable
        return None
    return f


def _primary_exchange_from_canonical(canonical: str) -> str | None:
    """Reverse-lookup an IB exchange code from a canonical symbol's suffix.

    We pick the *first* IB exchange code that maps to that suffix. For multi-
    code cases (NASDAQ/NYSE/ARCA/AMEX all → US), the choice is arbitrary but
    consistent — only the suffix matters downstream for display purposes.
    """
    if "." not in canonical:
        return None
    _, suffix = canonical.rsplit(".", 1)
    from app.core.symbols import IB_EXCHANGE_TO_SUFFIX

    for ib_code, s in IB_EXCHANGE_TO_SUFFIX.items():
        if s == suffix:
            return ib_code
    return None
