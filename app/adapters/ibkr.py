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

import asyncio
import logging
from dataclasses import replace
from typing import Callable, Protocol, Sequence

from app.core.broker import AccountSummary, ConnectionState, Position
from app.core.live_positions import LivePositions
from app.core.names import NameResolver
from app.core.symbols import canonical_symbol
from app.db.store import Store


_LOG = logging.getLogger(__name__)

# Production backoff schedule: ~5s, 15s, 60s, then stay at 60s. IBKR's daily
# restart usually completes within 1-2 minutes; capping at 60s avoids hammering
# the gateway while keeping recovery within a few minutes worst-case.
_DEFAULT_RECONNECT_DELAYS: Sequence[float] = (5.0, 15.0, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0)


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
        live_positions: LivePositions | None = None,
        reconnect_delays: Sequence[float] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib_factory = ib_factory
        self._store = store
        self._live_positions = live_positions
        self._reconnect_delays: Sequence[float] = tuple(
            reconnect_delays if reconnect_delays is not None else _DEFAULT_RECONNECT_DELAYS
        )
        self._ib: _IBLike | None = None
        self._name_resolver: NameResolver | None = None
        # Streaming-mode state — populated only when live_positions is provided.
        # Maps conId -> (Position, contract, ticker) so tick callbacks can recompute.
        self._streaming: dict[int, tuple[Position, object, object]] = {}
        self._connection_state: ConnectionState = ConnectionState.DISCONNECTED
        self._reconnect_task: asyncio.Task | None = None

    # ---- lifecycle (slice 1) -------------------------------------------------

    async def start(self) -> None:
        """Boot-path entry: connect, and if that fails, drop into the reconnect
        loop instead of leaving the adapter permanently DISCONNECTED.

        Why this exists: slice 9's auto-reconnect is wired to
        ``disconnectedEvent``, which only fires AFTER a successful connect. If
        the gateway is still doing 2FA when the dashboard boots, the initial
        connect times out and there's no event to drive recovery. ``start()``
        bridges that gap by treating an initial failure as a synthetic
        disconnect — same backoff schedule, same eventual recovery.

        Unlike ``connect()``, this never raises. The FastAPI lifespan can call
        it freely without wrapping in try/except.
        """
        try:
            await self.connect()
            return
        except Exception as exc:
            _LOG.warning("Initial gateway connect failed; entering RECONNECTING: %s", exc)
            self._handle_disconnect()

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
        self._connection_state = ConnectionState.CONNECTED
        if self._store is not None:
            self._name_resolver = NameResolver(
                store=self._store, fetcher=self._fetch_contract_details
            )
        # Register the disconnect handler so we can auto-reconnect when IBKR's
        # daily restart drops the session (or any other transient failure).
        disconnected_event = getattr(ib, "disconnectedEvent", None)
        if disconnected_event is not None:
            disconnected_event += self._handle_disconnect
        if self._live_positions is not None:
            await self._start_streaming()

    async def disconnect(self) -> None:
        # Cancel any in-flight reconnect loop first so it can't race ahead and
        # re-open the connection right after we close it.
        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ib is None:
            self._connection_state = ConnectionState.DISCONNECTED
            return
        if self._live_positions is not None:
            self._stop_streaming()
        self._ib.disconnect()
        self._ib = None
        self._connection_state = ConnectionState.DISCONNECTED

    async def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    async def get_connection_state(self) -> ConnectionState:
        return self._connection_state

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

    # ---- reconnect (slice 9) ------------------------------------------------

    def _handle_disconnect(self) -> None:
        """Called by ib_async when the TCP connection drops.

        Transitions immediately to RECONNECTING and spawns the reconnect loop.
        Idempotent — if we're already reconnecting, leaves the existing task alone.
        """
        if self._connection_state == ConnectionState.RECONNECTING:
            return
        self._connection_state = ConnectionState.RECONNECTING
        _LOG.warning("Gateway disconnected; entering RECONNECTING state")
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Retry connect() with the configured backoff schedule until success.

        After backoff exhaustion (the last delay has been tried), transitions
        to DISCONNECTED — the user can manually restart at that point.
        If the task is cancelled (explicit disconnect()), exits silently.
        """
        try:
            for attempt, delay in enumerate(self._reconnect_delays, start=1):
                _LOG.info("Reconnect attempt %d will fire in %.1fs", attempt, delay)
                await asyncio.sleep(delay)
                if self._connection_state == ConnectionState.CONNECTED:
                    # Something else reconnected us (manual connect()), stop.
                    return
                try:
                    # Tear down any stale IB ref before trying fresh
                    self._ib = None
                    self._streaming.clear()
                    await self.connect()
                    _LOG.info("Reconnect attempt %d succeeded", attempt)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _LOG.warning("Reconnect attempt %d failed: %s", attempt, exc)
                    continue
            _LOG.error("Backoff exhausted after %d attempts; staying DISCONNECTED", len(self._reconnect_delays))
            self._connection_state = ConnectionState.DISCONNECTED
        except asyncio.CancelledError:
            _LOG.info("Reconnect loop cancelled")
            raise

    # ---- streaming (slice 4) ------------------------------------------------

    async def _start_streaming(self) -> None:
        """Seed live_positions with the initial snapshot and subscribe to
        streaming reqMktData for each STK contract. Each tick updates the
        corresponding Position in live_positions."""
        assert self._ib is not None and self._live_positions is not None
        initial_positions = await self.get_positions()
        for position in initial_positions:
            self._live_positions.set_position(position)
            try:
                conid = int(position.native_key)
            except ValueError:
                continue
            # Re-fetch the Contract object — we need it for reqMktData
            contract = await self._contract_for_position(position)
            if contract is None:
                continue
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._streaming[conid] = (position, contract, ticker)
            # Subscribe to update events; ib_async fires this on every tick
            update_event = getattr(ticker, "updateEvent", None)
            if update_event is not None:
                update_event += self._on_ticker_update

    async def _contract_for_position(self, position: Position):
        """Recover the Contract for a Position by looking up via conId in IB.

        ib_async stores recent contracts internally; reqContractDetails is the
        portable way to round-trip from a conId back to a usable Contract object.
        """
        if self._ib is None:
            return None
        from ib_async import Contract  # imported lazily so unit tests don't need ib_async

        c = Contract(conId=int(position.native_key))
        try:
            details_list = await self._ib.reqContractDetailsAsync(c)
        except Exception:
            return None
        if not details_list:
            return None
        return details_list[0].contract

    def _stop_streaming(self) -> None:
        if self._ib is None:
            return
        for conid, (_pos, contract, ticker) in list(self._streaming.items()):
            update_event = getattr(ticker, "updateEvent", None)
            if update_event is not None:
                try:
                    update_event -= self._on_ticker_update
                except Exception:
                    pass
            try:
                self._ib.cancelMktData(contract)
            except Exception:
                pass
        self._streaming.clear()

    def _on_ticker_update(self, ticker) -> None:
        """Callback invoked by ib_async on every tick for a subscribed contract."""
        if self._live_positions is None:
            return
        try:
            conid = int(ticker.contract.conId)
        except (AttributeError, TypeError, ValueError):
            return
        entry = self._streaming.get(conid)
        if entry is None:
            return
        old_position, contract, _ticker_ref = entry
        new_last = _coerce_last(ticker)
        if new_last == old_position.last_price:
            return
        new_position = replace(
            old_position,
            last_price=new_last,
            market_value_native=old_position.quantity * new_last,
            unrealized_pnl_native=(new_last - old_position.avg_cost) * old_position.quantity,
        )
        self._streaming[conid] = (new_position, contract, ticker)
        self._live_positions.set_position(new_position)

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
