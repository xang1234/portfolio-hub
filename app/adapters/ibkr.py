"""IBKR concrete Broker adapter.

Public surface (Broker Protocol):
  - connect / disconnect / is_connected / get_connection_state
  - get_positions(): STK rows only (CASH still pending), with English name
    resolution via reqContractDetails, FX conversion to USD via FxService,
    previous-close fallback (IB historical → Yahoo) for unsubscribed markets,
    and pence-quoted UK equity normalization via priceMagnifier.
  - get_account_summary(): minimal stub; expanded later when accountValues()
    integration lands.

Internally, the adapter also runs:
  - A streaming layer that subscribes to reqMktData per position and
    pushes ticks into LivePositions for the SSE consumer.
  - An auto-reconnect loop on disconnectedEvent with exponential backoff.
"""

import asyncio
import logging
from dataclasses import replace
from typing import Callable, Protocol, Sequence

from app.core.broker import AccountSummary, ConnectionState, Position
from app.core.fx import FxConversion, FxService
from app.core.live_positions import LivePositions
from app.core.names import NameResolver
from app.core.symbols import CURRENCY_NAMES, canonical_symbol
from app.core.yahoo_quotes import default_yahoo_fetcher, yahoo_symbol_for
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
        fx_service: FxService | None = None,
        yahoo_quote_fetcher: Callable[[str], object] = default_yahoo_fetcher,
        reconnect_delays: Sequence[float] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib_factory = ib_factory
        self._store = store
        self._live_positions = live_positions
        self._fx_service = fx_service
        self._yahoo_quote_fetcher = yahoo_quote_fetcher
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
        # Hooks invoked once per successful reconnect (see on_reconnected).
        # Each callback gets the fresh IB instance as its only argument.
        self._reconnect_hooks: list[Callable[[object], None]] = []
        # Current step in the backoff schedule while RECONNECTING; None when
        # the loop isn't sleeping a delay. Surfaced via current_backoff_delay()
        # so the badge can render "reconnecting (5s)" / "(15s)" / "(60s)".
        self._current_backoff_delay: float | None = None

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
        if self._store is not None:
            self._name_resolver = NameResolver(
                store=self._store, fetcher=self._fetch_contract_details
            )
        if self._fx_service is not None:
            self._fx_service.attach_ib(ib)
        # Register the disconnect handler so we can auto-reconnect when IBKR's
        # daily restart drops the session (or any other transient failure).
        disconnected_event = getattr(ib, "disconnectedEvent", None)
        if disconnected_event is not None:
            disconnected_event += self._handle_disconnect
        if self._live_positions is not None:
            # If streaming setup fails (e.g. gateway accepted TCP but is still
            # loading accounts, reqPositionsAsync errors), bail out so the
            # reconnect loop sees the failure and retries. Setting state =
            # CONNECTED before this would wedge: the loop's `if state ==
            # CONNECTED: return` early-out would exit on the next iteration
            # without reattaching streaming, and hooks would never fire.
            try:
                await self._start_streaming()
            except Exception:
                self._ib = None
                if disconnected_event is not None:
                    try:
                        disconnected_event -= self._handle_disconnect
                    except Exception:
                        pass
                raise
        # State flips to CONNECTED only once everything above succeeded —
        # ensures the reconnect loop never sees a half-wired adapter.
        self._connection_state = ConnectionState.CONNECTED

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
        cash_positions = [p for p in ib_positions if p.contract.secType == "CASH"]
        if not stk_positions and not cash_positions:
            return []

        last_prices: dict[int, float] = {}
        previous_close_set: set[int] = set()
        if stk_positions:
            # Snapshot last prices in one round-trip rather than one per row
            contracts = [p.contract for p in stk_positions]
            tickers = await self._ib.reqTickersAsync(*contracts)
            last_prices = {c.conId: _coerce_last(t) for c, t in zip(contracts, tickers)}

            # For positions without a live/delayed last (international markets
            # without paid market-data subs — TSEJ, SBF, IBIS, etc.), fall back
            # to the most recent daily-bar close via reqHistoricalData. Mark
            # these so the row renderer can show a "prev close" subtext.
            for contract in contracts:
                if last_prices.get(contract.conId, 0.0) > 0:
                    continue
                prev_close = await self._fetch_previous_close(contract)
                if prev_close is not None and prev_close > 0:
                    last_prices[contract.conId] = prev_close
                    previous_close_set.add(contract.conId)

        # Make sure FxService has live subscriptions for every non-USD currency
        # we're about to render (STK and CASH both contribute). Idempotent.
        if self._fx_service is not None:
            currencies = (
                {p.contract.currency for p in stk_positions}
                | {p.contract.currency for p in cash_positions}
            ) - {"USD"}
            try:
                await self._fx_service.ensure_subscribed(currencies)
            except Exception as exc:
                _LOG.warning("Failed to ensure FX subscriptions: %s", exc)

        out: list[Position] = []
        for ib_pos in stk_positions:
            position = await self._build_position(
                ib_pos, last_prices, previous_close_set,
            )
            if position is not None:
                out.append(position)
        for ib_pos in cash_positions:
            out.append(self._build_cash_position(ib_pos))
        return out

    async def _fetch_previous_close(self, contract) -> float | None:
        """Fall back to last-known close for instruments without a live tick.

        Tries IB historical data first (subscription-independent on some
        exchanges), then Yahoo Finance for venues IB gates entirely
        (TSEJ, SBF, IBIS, SFB, etc.). Returns None on total failure so the
        row degrades to — instead of crashing.
        """
        # First try: IB historical. Subscription-independent on US/HK/some others.
        ib_close = await self._try_ib_historical(contract)
        if ib_close is not None:
            return ib_close
        # Second try: Yahoo Finance EOD. Covers most international venues
        # where IB gates data behind paid subscriptions.
        return await self._try_yahoo(contract)

    async def _try_ib_historical(self, contract) -> float | None:
        req_hist = getattr(self._ib, "reqHistoricalDataAsync", None)
        if not callable(req_hist):
            return None
        try:
            bars = await req_hist(
                contract,
                endDateTime="",
                durationStr="2 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        except Exception as exc:
            _LOG.debug(
                "IB historical fallback failed for %s (%s): %s",
                getattr(contract, "symbol", "?"),
                getattr(contract, "primaryExchange", "?"),
                exc,
            )
            return None
        if not bars:
            return None
        value = float(getattr(bars[-1], "close", 0.0))
        return value if value > 0 else None

    async def _try_yahoo(self, contract) -> float | None:
        if self._yahoo_quote_fetcher is None:
            return None
        exchange = getattr(contract, "primaryExchange", "") or getattr(contract, "exchange", "")
        symbol = yahoo_symbol_for(getattr(contract, "symbol", ""), exchange)
        if symbol is None:
            return None
        try:
            value = await self._yahoo_quote_fetcher(symbol)
        except Exception as exc:
            _LOG.debug("Yahoo fallback raised for %s: %s", symbol, exc)
            return None
        if value is None or value <= 0:
            return None
        _LOG.info("Used Yahoo EOD fallback for %s = %.4f", symbol, value)
        return float(value)

    async def _build_position(
        self,
        ib_pos,
        last_prices: dict[int, float],
        previous_close_set: set[int] | None = None,
    ) -> Position | None:
        contract = ib_pos.contract
        native_key = str(contract.conId)

        resolved = await self._resolve_name(native_key, contract)
        if resolved is None:
            return None
        canonical, name_en, primary_exchange, price_magnifier = resolved

        last_price = last_prices.get(contract.conId, 0.0)
        is_prev_close = bool(
            previous_close_set is not None and contract.conId in previous_close_set
        )
        quantity = float(ib_pos.position)
        avg_cost = float(ib_pos.avgCost)
        # mv/pnl in major currency — see Position.price_magnifier for the
        # divisor convention.
        mv_native = quantity * last_price / price_magnifier
        pnl_native = (last_price - avg_cost) * quantity / price_magnifier

        conv = self._convert_to_usd(contract.currency, mv_native, pnl_native)

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
            market_value_usd=conv.mv_usd,
            unrealized_pnl_native=pnl_native,
            unrealized_pnl_usd=conv.pnl_usd,
            fx_is_stale=conv.fx_is_stale,
            fx_is_fallback=conv.fx_is_fallback,
            fx_unavailable=conv.fx_unavailable,
            last_price_is_previous_close=is_prev_close,
            price_magnifier=price_magnifier,
        )

    def _build_cash_position(self, ib_pos) -> Position:
        """Synthesize a Position from an IB CASH balance.

        The instrument *is* the currency, so identity fields (native_key,
        canonical_symbol, native_symbol) are all the currency code; there
        is no exchange and no name to resolve. quantity is the balance,
        last_price/avg_cost are 1.0 (the currency's "price" against itself).

        v1 deliberately reports P&L as 0 — computing true USD P&L on FX
        cash needs FX cost-basis IB doesn't track reliably.
        """
        currency = ib_pos.contract.currency
        quantity = float(ib_pos.position)
        conv = self._convert_to_usd(currency, quantity, 0.0)
        name = CURRENCY_NAMES.get(currency, currency)
        return Position(
            broker=self.name,
            account_id=ib_pos.account,
            native_key=currency,
            canonical_symbol=currency,
            native_symbol=currency,
            exchange="",
            currency=currency,
            name_en=name,
            asset_class="CASH",
            quantity=quantity,
            avg_cost=1.0,
            last_price=1.0,
            market_value_native=quantity,
            market_value_usd=conv.mv_usd,
            unrealized_pnl_native=0.0,
            unrealized_pnl_usd=0.0,
            fx_is_stale=conv.fx_is_stale,
            fx_is_fallback=conv.fx_is_fallback,
            fx_unavailable=conv.fx_unavailable,
        )

    def _convert_to_usd(
        self, currency: str, mv_native: float, pnl_native: float,
    ) -> FxConversion:
        """Convert native amounts to USD. Pure sync — FxService.get_rate
        does no I/O so there's no need for an async variant.

        USD rows pass through; missing-rate rows return mv/pnl=0 with
        fx_unavailable=True so the template renders — instead of $0.00.
        """
        if currency == "USD":
            return FxConversion(mv_native, pnl_native, False, False, False)
        if self._fx_service is None:
            return FxConversion(0.0, 0.0, False, False, True)
        try:
            rate = self._fx_service.get_rate_sync(currency)
        except ValueError:
            _LOG.warning("Invalid FX currency on position: %s", currency)
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

    async def _resolve_name(
        self,
        native_key: str,
        contract,
    ) -> tuple[str, str, str, int] | None:
        """Return (canonical_symbol, name_en, primary_exchange, price_magnifier) or None.

        Uses the NameResolver cache when a Store was injected; otherwise fetches
        on every call (acceptable for unit tests but not for production).
        """
        if self._name_resolver is not None:
            cached = await self._name_resolver.resolve(self.name, native_key)
            if cached is not None:
                canonical, name_en, price_magnifier = cached
                primary_exchange = _primary_exchange_from_canonical(canonical)
                if primary_exchange is None:
                    return None
                return canonical, name_en, primary_exchange, price_magnifier

        fetched = await self._fetch_contract_details(self.name, native_key, contract=contract)
        if fetched is None:
            return None
        canonical, name_en, price_magnifier = fetched
        primary_exchange = _primary_exchange_from_canonical(canonical)
        if primary_exchange is None:
            return None
        if self._store is not None:
            await self._store.put_name_cache(
                self.name, native_key, canonical, name_en, price_magnifier,
            )
        return canonical, name_en, primary_exchange, price_magnifier

    async def _fetch_contract_details(
        self,
        broker: str,
        native_key: str,
        contract=None,
    ) -> tuple[str, str, int] | None:
        """Fetcher passed to NameResolver. Calls reqContractDetailsAsync and
        returns (canonical_symbol, name_en, price_magnifier) or None.

        priceMagnifier defaults to 1 for contracts where IB doesn't supply it
        (most). LSE pence-quoted equities get 100 — IB returns last/avgCost
        in pence for those.
        """
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
        try:
            price_magnifier = int(getattr(details, "priceMagnifier", 1) or 1)
        except (TypeError, ValueError):
            price_magnifier = 1
        return canonical, name_en, price_magnifier

    # ---- reconnect (slice 9) ------------------------------------------------

    def current_backoff_delay(self) -> float | None:
        """Return the backoff window the reconnect loop is currently sleeping,
        or None when not actively waiting for a retry. Drives the badge text
        "🟡 IBKR reconnecting (5s)" → "(15s)" → "(60s)" as the loop progresses.
        """
        return self._current_backoff_delay

    def on_reconnected(self, callback: Callable[[object], None]) -> None:
        """Register a callback fired once after each successful auto-reconnect.

        Used by features that hook IB events (slice 11's execDetailsEvent for
        fills) — the original handlers die with the dead IB session, so they
        need re-registering on the fresh one. The callback receives the new
        IB instance as its only argument.

        Registration is idempotent: passing the same callable twice keeps it
        in the list once, so features registering both at startup and on
        config reload don't get double-fired.

        IMPORTANT — this fires on RECONNECTS ONLY, never after the initial
        connect(). Callers that need a handler on the very first IB session
        must wire it independently at startup (e.g. attach the
        execDetailsEvent handler immediately after `await adapter.start()`)
        AND register an `on_reconnected` hook to re-attach the same handler
        across subsequent gateway restarts. A caller that registers only
        the hook will silently miss events on first boot until the first
        daily restart.
        """
        if callback in self._reconnect_hooks:
            return
        self._reconnect_hooks.append(callback)

    def _fire_reconnect_hooks(self) -> None:
        """Invoke every registered hook with the live IB instance.

        One bad hook must not block the others — log and continue. The
        reconnect itself is still considered successful even if hooks raise.
        """
        ib = self._ib
        for hook in list(self._reconnect_hooks):
            try:
                hook(ib)
            except Exception as exc:
                _LOG.warning("Reconnect hook %r raised: %s", hook, exc)

    def _handle_disconnect(self) -> None:
        """Called by ib_async when the TCP connection drops.

        Transitions immediately to RECONNECTING and spawns the reconnect loop.
        Also marks every Position in live_positions stale so the renderer can
        show ⚠️/dim until the next real tick replaces them.
        Idempotent — if we're already reconnecting, leaves the existing task alone.

        Order matters: deregister tick handlers BEFORE marking stale. Otherwise
        a pending tick callback queued before the disconnect can fire between
        the two and re-write the Position with `last_price_is_stale=False`,
        masking the disconnect from the user.
        """
        if self._connection_state == ConnectionState.RECONNECTING:
            return
        self._connection_state = ConnectionState.RECONNECTING
        _LOG.warning("Gateway disconnected; entering RECONNECTING state")
        self._deregister_tick_handlers()
        self._mark_live_positions_stale()
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    def _deregister_tick_handlers(self) -> None:
        """Unbind _on_ticker_update from every live ticker without touching IB.

        Done synchronously inside the disconnect handler so a pending ib_async
        callback can't sneak in between mark-stale and the _streaming.clear()
        that runs later in the reconnect loop. Skips cancelMktData (the IB
        session is dead anyway) and also breaks the ticker→handler→adapter
        reference cycle so the old tickers can be garbage-collected.
        """
        for conid, (_pos, _contract, ticker) in list(self._streaming.items()):
            update_event = getattr(ticker, "updateEvent", None)
            if update_event is None:
                continue
            try:
                update_event -= self._on_ticker_update
            except Exception as exc:
                _LOG.debug(
                    "Failed to deregister tick handler for conId=%s: %s",
                    conid, exc,
                )

    def _mark_live_positions_stale(self) -> None:
        """Flip last_price_is_stale=True on every currently-known STK Position.

        CASH rows are excluded: their "price" is the currency-vs-itself rate
        of 1.0 which never ticks, so labelling them stale (with a "price hasn't
        ticked since the disconnect" tooltip) is misleading. The dimmed-row
        signal alone doesn't apply to cash either; CASH rows simply pass
        through the reconnect window unchanged.

        Done synchronously inside the disconnectedEvent handler so the very
        next render (status badge swap, SSE delta from a pending tick that
        races us, etc.) sees the stale flag set. The flag clears naturally
        on the next live tick — see _on_ticker_update.
        """
        if self._live_positions is None:
            return
        for old in self._live_positions.get_all():
            if old.asset_class == "CASH":
                continue
            if old.last_price_is_stale:
                continue
            self._live_positions.set_position(replace(old, last_price_is_stale=True))

    async def _reconnect_loop(self) -> None:
        """Retry connect() with the configured backoff schedule until success.

        After backoff exhaustion (the last delay has been tried), transitions
        to DISCONNECTED — the user can manually restart at that point.
        If the task is cancelled (explicit disconnect()), exits silently.
        """
        try:
            for attempt, delay in enumerate(self._reconnect_delays, start=1):
                # `attempt` is 1-indexed, so `_reconnect_delays[attempt]` is
                # the delay AFTER this one (or end-of-schedule).
                next_after = (
                    self._reconnect_delays[attempt]
                    if attempt < len(self._reconnect_delays)
                    else None
                )
                _LOG.info(
                    "Reconnect attempt %d will fire in %.1fs (next delay: %s)",
                    attempt, delay,
                    f"{next_after:.1f}s" if next_after is not None else "exhausted",
                )
                self._current_backoff_delay = delay
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
                    self._current_backoff_delay = None
                    self._fire_reconnect_hooks()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _LOG.warning("Reconnect attempt %d failed: %s", attempt, exc)
                    continue
            _LOG.error("Backoff exhausted after %d attempts; staying DISCONNECTED", len(self._reconnect_delays))
            self._connection_state = ConnectionState.DISCONNECTED
            self._current_backoff_delay = None
        except asyncio.CancelledError:
            _LOG.info("Reconnect loop cancelled")
            self._current_backoff_delay = None
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
        # Null tick (no data permission, off-hours snapshot, etc.) means "I
        # don't know the price right now," not "the price is zero." Don't
        # clobber the seeded Yahoo prev-close or any real previous tick.
        if new_last <= 0:
            return
        if new_last == old_position.last_price:
            return
        # Recompute USD on every real tick — fixes seed-time fx_unavailable
        # rows where the FX rate hadn't loaded yet. See Position.price_magnifier
        # for the pence-divisor convention.
        pm = old_position.price_magnifier
        new_mv_native = old_position.quantity * new_last / pm
        new_pnl_native = (new_last - old_position.avg_cost) * old_position.quantity / pm
        conv = self._convert_to_usd(
            old_position.currency, new_mv_native, new_pnl_native,
        )
        new_position = replace(
            old_position,
            last_price=new_last,
            market_value_native=new_mv_native,
            unrealized_pnl_native=new_pnl_native,
            market_value_usd=conv.mv_usd,
            unrealized_pnl_usd=conv.pnl_usd,
            fx_is_stale=conv.fx_is_stale,
            fx_is_fallback=conv.fx_is_fallback,
            fx_unavailable=conv.fx_unavailable,
            last_price_is_previous_close=False,
            last_price_is_stale=False,
        )
        self._streaming[conid] = (new_position, contract, ticker)
        self._live_positions.set_position(new_position)

    # ---- account summary (slice 7) ------------------------------------------

    async def get_account_summary(self) -> list[AccountSummary]:
        """Return one AccountSummary per linked IBKR account.

        Pulls the tags we care about (NetLiquidation, TotalCashValue,
        BuyingPower) for every account from IB's `accountSummaryAsync`,
        then converts each value to USD through the FxService when the
        reported currency isn't already USD.

        Account IDs come from `managedAccounts()` so accounts with zero
        positions still appear as filter-chip options. Returns `[]` when
        the gateway isn't connected — matches `get_positions`.
        """
        if self._ib is None:
            return []
        managed = getattr(self._ib, "managedAccounts", None)
        accounts: list[str] = list(managed()) if callable(managed) else []
        try:
            rows = await self._ib.accountSummaryAsync()
        except Exception as exc:
            _LOG.warning("accountSummaryAsync failed: %s", exc)
            rows = []

        # Group {(account, tag) -> (value:str, currency:str)} for easy access.
        by_acc_tag: dict[tuple[str, str], tuple[str, str]] = {}
        for r in rows:
            by_acc_tag[(r.account, r.tag)] = (r.value, r.currency or "USD")
            if r.account and r.account not in accounts:
                accounts.append(r.account)

        out: list[AccountSummary] = []
        for acc in sorted(set(accounts)):
            nlv_value, nlv_ccy = by_acc_tag.get((acc, "NetLiquidation"), ("0", "USD"))
            cash_value, cash_ccy = by_acc_tag.get((acc, "TotalCashValue"), ("0", "USD"))
            bp_value, bp_ccy = by_acc_tag.get((acc, "BuyingPower"), ("0", "USD"))
            base_ccy = nlv_ccy  # NLV's reported currency is the account's base
            nlv_usd = self._to_usd(_safe_float(nlv_value), nlv_ccy)
            cash_usd = self._to_usd(_safe_float(cash_value), cash_ccy)
            bp_usd = self._to_usd(_safe_float(bp_value), bp_ccy)
            out.append(
                AccountSummary(
                    broker=self.name,
                    account_id=acc,
                    base_currency=base_ccy,
                    net_liquidation_usd=nlv_usd,
                    cash_usd=cash_usd,
                    buying_power_usd=bp_usd,
                )
            )
        return out

    def _to_usd(self, amount: float, currency: str) -> float:
        """Convert ``amount`` from ``currency`` to USD using the cached
        FX rate. USD passes through; unknown currency or missing rate
        returns 0.0 so the UI shows zero rather than crashing."""
        if currency == "USD":
            return amount
        if self._fx_service is None:
            return 0.0
        try:
            rate = self._fx_service.get_rate_sync(currency)
        except ValueError:
            _LOG.warning("Invalid base currency on account: %s", currency)
            return 0.0
        if rate is None:
            return 0.0
        return amount * rate.rate


# Helpers ---------------------------------------------------------------------


def _safe_float(value: str) -> float:
    """IB account-summary values are strings — convert defensively.

    Empty / unparseable values become 0.0 rather than raising; an account
    with a missing tag should render zero, not crash the page.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
