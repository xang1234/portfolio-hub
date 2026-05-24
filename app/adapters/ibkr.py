"""IBKR concrete Broker adapter.

Public surface (Broker Protocol):
  - connect / disconnect / is_connected / get_connection_state
  - get_positions(): STK + CASH rows, with English name
    resolution via reqContractDetails, FX conversion to USD via FxService,
    previous-close fallback (Yahoo → fast IB historical) for unsubscribed markets,
    and pence-quoted UK equity normalization via priceMagnifier.
  - get_account_summary(): per-account NLV/cash/buying-power summary.

Internally, the adapter also runs:
  - A streaming layer that subscribes to reqMktData per position and
    pushes ticks into LivePositions for the SSE consumer.
  - An auto-reconnect loop on disconnectedEvent with exponential backoff.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Callable, Protocol, Sequence

from app.core.broker import AccountSummary, ConnectionState, Position
from app.core.fx import FxConversion, FxService
from app.core.live_positions import LivePositions
from app.core.names import NameResolver
from app.core.symbols import CURRENCY_NAMES, IB_EXCHANGE_TO_SUFFIX, canonical_symbol
from app.core.yahoo_quotes import default_yahoo_fetcher, yahoo_symbol_for
from app.db.store import Store


_LOG = logging.getLogger(__name__)
_IB_HISTORICAL_TIMEOUT_SECONDS = 2.0
_PREVIOUS_CLOSE_FALLBACK_CONCURRENCY = 8
_IB_HISTORICAL_PERMISSION_GATED_EXCHANGES = frozenset({
    "AEB",
    "IBIS",
    "KOSDAQ",
    "KRX",
    "KSE",
    "LSE",
    "SBF",
    "SFB",
    "SGX",
    "TPEX",
    "TSEJ",
    "TWSE",
})
_IB_LIVE_QUOTE_PERMISSION_GATED_EXCHANGES = _IB_HISTORICAL_PERMISSION_GATED_EXCHANGES
_IB_STREAMING_PERMISSION_GATED_EXCHANGES = _IB_HISTORICAL_PERMISSION_GATED_EXCHANGES

# Re-export so existing test imports (and the ibkr adapter's own callers)
# keep working without having to grep-and-update. The canonical home is
# app.core.fills — adapters should import from there in new code.
from app.core.fills import build_fill_row  # noqa: E402, F401  (re-export)

# Production backoff schedule: ~5s, 15s, 60s, then stay at 60s. IBKR's daily
# restart usually completes within 1-2 minutes; capping at 60s avoids hammering
# the gateway while keeping recovery within a few minutes worst-case.
_DEFAULT_RECONNECT_DELAYS: Sequence[float] = (5.0, 15.0, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0)


@dataclass(frozen=True)
class _AccountCashValue:
    account_id: str
    currency: str
    amount: float


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
        # Per-conId previous-close cache, keyed by UTC date. A poll loop hitting
        # get_positions every few seconds otherwise spawns N reqHistoricalData
        # calls per cycle — once a day per contract is enough for prev-close.
        self._previous_close_cache: dict[int, tuple[object, float]] = {}
        self._previous_close_miss_cache: dict[int, object] = {}
        self._listing_exchange_cache: dict[int, str] = {}
        self._contract_details_cache: dict[int, object] = {}
        # Daily re-seed of previous_close on streaming Positions. _start_streaming
        # spawns it; _stop_streaming cancels. Without it, a session that survives
        # past UTC midnight would show intraday % vs the close from the day the
        # session connected (the in-memory streaming Positions never re-consult
        # the cache after their initial seed). IBKR's once-a-day restart usually
        # masks this — defense-in-depth for sessions that don't restart.
        self._prev_close_refresh_task: asyncio.Task | None = None
        self._connection_state: ConnectionState = ConnectionState.DISCONNECTED
        self._reconnect_task: asyncio.Task | None = None
        # Hooks invoked once per successful reconnect (see on_reconnected).
        # Each callback gets the fresh IB instance as its only argument.
        self._reconnect_hooks: list[Callable[[object], None]] = []
        # Current step in the backoff schedule while RECONNECTING; None when
        # the loop isn't sleeping a delay. Surfaced via current_backoff_delay()
        # so the badge can render "reconnecting (5s)" / "(15s)" / "(60s)".
        self._current_backoff_delay: float | None = None
        # Fill-write tasks in flight. Held strongly so the event loop's weak-
        # reference garbage collector can't reap them mid-INSERT (a real
        # Python asyncio footgun — see Python docs on create_task). Cleared
        # via add_done_callback so the set doesn't grow unbounded.
        self._pending_writes: set[asyncio.Task] = set()

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
        # Slice 11: wire execDetailsEvent on every connect (initial + each
        # reconnect retry). Each fresh IB instance carries its own event
        # object, so this is naturally idempotent — we wire once per
        # instance, never accumulate handlers across reconnects.
        #
        # Note: issue #11 proposed using slice 9's on_reconnected hook for
        # re-attach. We chose `connect()` instead because (a) on_reconnected
        # fires only after RECONNECTS, not the initial connect, so we'd
        # still need a startup site and a hook site — two places to forget;
        # (b) re-binding IS what connect() does on the new IB session, so
        # the hook layer would just delegate back to this exact site.
        # `test_fills_handler_does_not_double_register_across_reconnects`
        # is the regression guard.
        exec_event = getattr(ib, "execDetailsEvent", None)
        if exec_event is not None and self._store is not None:
            exec_event += self._on_exec_details
        portfolio_event = getattr(ib, "updatePortfolioEvent", None)
        if portfolio_event is not None and self._live_positions is not None:
            portfolio_event += self._on_portfolio_update
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
                if portfolio_event is not None:
                    try:
                        portfolio_event -= self._on_portfolio_update
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
        # Drain any in-flight fill INSERTs before tearing down so we don't
        # orphan the last few writes during a graceful shutdown.
        if self._pending_writes:
            await asyncio.gather(*self._pending_writes, return_exceptions=True)
        if self._ib is None:
            self._connection_state = ConnectionState.DISCONNECTED
            return
        portfolio_event = getattr(self._ib, "updatePortfolioEvent", None)
        if portfolio_event is not None:
            try:
                portfolio_event -= self._on_portfolio_update
            except Exception:
                pass
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
            return await self._missing_account_cash_positions([])

        last_prices: dict[int, float] = {}
        last_price_delayed: dict[int, bool] = {}
        previous_closes: dict[int, float] = {}
        portfolio_items = self._portfolio_items_by_conid()
        if stk_positions:
            # Snapshot last prices in one round-trip rather than one per row
            contracts = [p.contract for p in stk_positions]
            listing_exchanges = await self._listing_exchanges_for_contracts(contracts)
            quote_contracts = [
                c for c in contracts
                if not _is_live_quote_permission_gated(
                    listing_exchanges.get(int(getattr(c, "conId", 0)), "")
                )
            ]
            if quote_contracts:
                tickers = await self._ib.reqTickersAsync(*quote_contracts)
                last_prices = {
                    c.conId: _coerce_last(t) for c, t in zip(quote_contracts, tickers)
                }
                last_price_delayed = {
                    c.conId: _ticker_is_delayed(t)
                    for c, t in zip(quote_contracts, tickers)
                }

            # Always fetch previous-session close for ALL contracts (cached by
            # UTC date to avoid per-poll thrash). Two consumers:
            #  1. The intraday-change % and hero "Today" P&L need it for every
            #     row where we also have a live last_price.
            #  2. Unsubscribed markets (TSEJ, SBF, IBIS, etc.) reuse it as the
            #     fallback last_price so the row still renders — same behavior
            #     as before, just sourced from the same cache.
            previous_closes = await self._fetch_previous_closes_cached(
                contracts,
                listing_exchanges=listing_exchanges,
            )

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
                ib_pos,
                last_prices,
                previous_closes,
                portfolio_items,
                last_price_delayed,
            )
            if position is not None:
                out.append(position)
        for ib_pos in cash_positions:
            out.append(self._build_cash_position(ib_pos))
        out.extend(await self._missing_account_cash_positions(out))
        return out

    async def _missing_account_cash_positions(
        self, positions: list[Position],
    ) -> list[Position]:
        """Backfill account-summary cash when reqPositions omits CASH rows."""
        accounts_with_cash = {
            p.account_id for p in positions if p.asset_class == "CASH"
        }
        missing: list[Position] = []
        for cash in await self._account_cash_values():
            if cash.account_id in accounts_with_cash:
                continue
            if cash.amount == 0:
                continue
            missing.append(self._build_account_cash_position(cash))
        return missing

    async def _account_cash_values(self) -> list[_AccountCashValue]:
        if self._ib is None:
            return []
        account_summary = getattr(self._ib, "accountSummaryAsync", None)
        if not callable(account_summary):
            return []

        managed = getattr(self._ib, "managedAccounts", None)
        accounts: list[str] = list(managed()) if callable(managed) else []
        try:
            rows = await account_summary()
        except Exception as exc:
            _LOG.warning("accountSummaryAsync failed while backfilling cash: %s", exc)
            return []

        by_account: dict[str, _AccountCashValue] = {}
        for row in rows:
            account_id = getattr(row, "account", "")
            if account_id and account_id not in accounts:
                accounts.append(account_id)
            if getattr(row, "tag", "") != "TotalCashValue" or not account_id:
                continue
            by_account[account_id] = _AccountCashValue(
                account_id=account_id,
                currency=getattr(row, "currency", "") or "USD",
                amount=_safe_float(getattr(row, "value", "0")),
            )
        return [
            by_account[account_id]
            for account_id in accounts
            if account_id in by_account
        ]

    async def _listing_exchanges_for_contracts(self, contracts) -> dict[int, str]:
        out: dict[int, str] = {}
        for contract in contracts:
            conid = int(getattr(contract, "conId", 0))
            cached = self._listing_exchange_cache.get(conid)
            if cached:
                out[conid] = cached
                continue
            exchange = await self._listing_exchange_for_contract(contract)
            if exchange:
                self._listing_exchange_cache[conid] = exchange
                out[conid] = exchange
        return out

    async def _listing_exchange_for_contract(self, contract) -> str | None:
        exchange = _first_known_listing_exchange(
            "",
            "",
            getattr(contract, "primaryExchange", ""),
            getattr(contract, "exchange", ""),
        )
        if exchange:
            return exchange
        if self._ib is None:
            return None
        try:
            details = await self._contract_details_for_contract(contract)
        except Exception:
            return None
        if details is None:
            return None
        details_contract = getattr(details, "contract", None)
        return _first_known_listing_exchange(
            getattr(details_contract, "primaryExchange", ""),
            getattr(details_contract, "exchange", ""),
            getattr(contract, "primaryExchange", ""),
            getattr(contract, "exchange", ""),
        )

    async def _contract_details_for_contract(self, contract):
        if self._ib is None:
            return None
        conid = int(getattr(contract, "conId", 0))
        cached = self._contract_details_cache.get(conid)
        if cached is not None:
            return cached
        details_list = await self._ib.reqContractDetailsAsync(contract)
        if not details_list:
            return None
        details = details_list[0]
        self._contract_details_cache[conid] = details
        return details

    def _portfolio_items_by_conid(self) -> dict[int, object]:
        if self._ib is None:
            return {}
        portfolio = getattr(self._ib, "portfolio", None)
        if not callable(portfolio):
            return {}
        try:
            items = portfolio()
        except Exception as exc:
            _LOG.debug("Failed to read IB portfolio cache: %s", exc)
            return {}
        out: dict[int, object] = {}
        for item in items:
            contract = getattr(item, "contract", None)
            try:
                conid = int(getattr(contract, "conId", 0))
            except (TypeError, ValueError):
                continue
            if conid:
                out[conid] = item
        return out

    async def _fetch_previous_closes_cached(
        self,
        contracts,
        *,
        listing_exchanges: dict[int, str] | None = None,
    ) -> dict[int, float]:
        """Return {conId: prev-close} for every contract, hitting the per-UTC-date
        cache before falling back to the concurrent network fetcher.

        Invalidates naturally at UTC midnight — coarse-grained, but the worst
        case is one venue's session has rolled past midnight UTC and we serve
        a one-day-stale close until first refresh of the new UTC day. The
        intraday-% display tolerates this; per-second tick correctness is not
        the contract."""
        import datetime as _dt

        today = _dt.datetime.now(_dt.timezone.utc).date()
        out: dict[int, float] = {}
        misses: list = []
        for contract in contracts:
            conid = int(getattr(contract, "conId", 0))
            cached = self._previous_close_cache.get(conid)
            if cached is not None and cached[0] == today:
                out[conid] = cached[1]
            elif self._previous_close_miss_cache.get(conid) == today:
                continue
            else:
                misses.append(contract)
        if misses:
            fresh = await self._fetch_previous_closes(
                misses,
                listing_exchanges=listing_exchanges,
            )
            for conid, value in fresh.items():
                self._previous_close_cache[conid] = (today, value)
                self._previous_close_miss_cache.pop(conid, None)
                out[conid] = value
            for contract in misses:
                conid = int(getattr(contract, "conId", 0))
                if conid not in fresh:
                    self._previous_close_miss_cache[conid] = today
        return out

    async def _fetch_previous_closes(
        self,
        contracts,
        *,
        listing_exchanges: dict[int, str] | None = None,
    ) -> dict[int, float]:
        if not contracts:
            return {}
        semaphore = asyncio.Semaphore(_PREVIOUS_CLOSE_FALLBACK_CONCURRENCY)

        async def _fetch_one(contract) -> tuple[int, float | None]:
            async with semaphore:
                conid = int(getattr(contract, "conId"))
                return conid, await self._fetch_previous_close(
                    contract,
                    listing_exchange=(listing_exchanges or {}).get(conid),
                )

        results = await asyncio.gather(*(_fetch_one(contract) for contract in contracts))
        return {
            conid: value for conid, value in results
            if value is not None and value > 0
        }

    async def _fetch_previous_close(
        self,
        contract,
        *,
        listing_exchange: str | None = None,
    ) -> float | None:
        """Fall back to last-known close for instruments without a live tick.

        Tries Yahoo Finance first when we know the venue's symbol convention.
        This avoids waiting through IB historical-data timeouts on exchanges
        where the account lacks market-data subscriptions (TSEJ, SBF, IBIS,
        SFB, etc.). If Yahoo has no mapping/data for those known gated
        venues, degrade the row without asking IB for historical bars. Other
        venues still get a short IB historical request before we give up.
        """
        yahoo_close = await self._try_yahoo(contract, listing_exchange=listing_exchange)
        if yahoo_close is not None:
            return yahoo_close
        exchange = listing_exchange or getattr(contract, "primaryExchange", "") or getattr(contract, "exchange", "")
        if _is_historical_permission_gated(exchange):
            return None
        return await self._try_ib_historical(contract)

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
                timeout=_IB_HISTORICAL_TIMEOUT_SECONDS,
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

    async def _try_yahoo(
        self,
        contract,
        *,
        listing_exchange: str | None = None,
    ) -> float | None:
        if self._yahoo_quote_fetcher is None:
            return None
        exchange = listing_exchange or getattr(contract, "primaryExchange", "") or getattr(contract, "exchange", "")
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
        previous_closes: dict[int, float] | None = None,
        portfolio_items: dict[int, object] | None = None,
        last_price_delayed: dict[int, bool] | None = None,
    ) -> Position | None:
        contract = ib_pos.contract
        native_key = str(contract.conId)

        resolved = await self._resolve_name(native_key, contract)
        if resolved is None:
            return None
        canonical, name_en, primary_exchange, price_magnifier = resolved

        conid = int(getattr(contract, "conId", 0))
        portfolio_item = (portfolio_items or {}).get(conid)
        last_price = last_prices.get(contract.conId, 0.0)
        is_delayed = bool((last_price_delayed or {}).get(contract.conId, False))
        is_broker_mark = False
        previous_close = (previous_closes or {}).get(contract.conId, 0.0)
        is_prev_close = False
        quantity = float(ib_pos.position)
        portfolio_avg_cost = _portfolio_average_cost(portfolio_item, price_magnifier)
        avg_cost = (
            portfolio_avg_cost
            if portfolio_avg_cost is not None
            else float(ib_pos.avgCost)
        )

        if last_price <= 0:
            portfolio_last = _portfolio_display_price(
                portfolio_item,
                price_magnifier,
                quantity=quantity,
            )
            if portfolio_last is not None:
                last_price = portfolio_last
                is_broker_mark = True
                is_delayed = False
            elif previous_close > 0:
                last_price = previous_close
                is_prev_close = True
                is_delayed = False

        # mv/pnl in major currency — see Position.price_magnifier for the
        # divisor convention.
        portfolio_mv = _portfolio_market_value(portfolio_item)
        portfolio_pnl = _portfolio_unrealized_pnl(portfolio_item)
        if last_prices.get(contract.conId, 0.0) <= 0 and portfolio_mv is not None:
            mv_native = portfolio_mv
        else:
            mv_native = quantity * last_price / price_magnifier
        if last_prices.get(contract.conId, 0.0) <= 0 and portfolio_pnl is not None:
            pnl_native = portfolio_pnl
        else:
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
            last_price_is_broker_mark=is_broker_mark,
            last_price_is_delayed=is_delayed,
            price_magnifier=price_magnifier,
            previous_close=previous_close,
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

    def _build_account_cash_position(self, cash: _AccountCashValue) -> Position:
        currency = cash.currency
        quantity = cash.amount
        conv = self._convert_to_usd(currency, quantity, 0.0)
        name = CURRENCY_NAMES.get(currency, currency)
        return Position(
            broker=self.name,
            account_id=cash.account_id,
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

        details = await self._contract_details_for_contract(contract)
        if details is None:
            _LOG.warning("reqContractDetails returned no details for conId=%s", native_key)
            return None

        details_contract = getattr(details, "contract", None)
        primary_exchange = _first_known_listing_exchange(
            getattr(details_contract, "primaryExchange", ""),
            getattr(details_contract, "exchange", ""),
            getattr(contract, "primaryExchange", ""),
            getattr(contract, "exchange", ""),
        )
        if not primary_exchange:
            _LOG.warning(
                "Contract conId=%s has no recognized listing exchange; dropping (would produce ambiguous canonical_symbol)",
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
            if _is_streaming_permission_gated(position.exchange):
                continue
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._streaming[conid] = (position, contract, ticker)
            # Subscribe to update events; ib_async fires this on every tick
            update_event = getattr(ticker, "updateEvent", None)
            if update_event is not None:
                update_event += self._on_ticker_update
        # Kick off the daily prev_close refresh loop so long-lived sessions
        # don't show stale intraday %. Idempotent — cancels any prior task.
        if self._prev_close_refresh_task is not None and not self._prev_close_refresh_task.done():
            self._prev_close_refresh_task.cancel()
        self._prev_close_refresh_task = asyncio.create_task(
            self._refresh_previous_closes_daily()
        )

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
        # Cancel the daily prev_close refresh loop; a fresh _start_streaming
        # (on reconnect) will spawn a new one.
        task = self._prev_close_refresh_task
        self._prev_close_refresh_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _refresh_previous_closes_daily(self) -> None:
        """Once per UTC day at ~00:05 UTC, re-fetch previous_close for every
        streaming contract and replace it on the in-memory Position. The
        cache invalidates on UTC date rollover so this also rebuilds the
        cache. ~00:05 (not 00:00) so we're firmly past the rollover even
        with mild clock skew.
        """
        import datetime as _dt
        while True:
            try:
                now = _dt.datetime.now(_dt.timezone.utc)
                next_run = (now + _dt.timedelta(days=1)).replace(
                    hour=0, minute=5, second=0, microsecond=0,
                )
                sleep_s = max(60.0, (next_run - now).total_seconds())
                await asyncio.sleep(sleep_s)
                await self._reseed_streaming_previous_closes()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOG.warning("prev_close daily refresh raised: %s", exc)
                # Don't busy-loop on a persistent failure mode (e.g., gateway
                # mid-reconnect refusing reqHistoricalData) — back off and try
                # again on the next UTC midnight.
                await asyncio.sleep(3600)

    async def _reseed_streaming_previous_closes(self) -> None:
        """Refresh previous_close on every streaming Position. Called by the
        daily loop and exposed as a method so tests can drive it directly."""
        if self._ib is None or self._live_positions is None:
            return
        contracts = [c for (_p, c, _t) in self._streaming.values()]
        if not contracts:
            return
        fresh = await self._fetch_previous_closes_cached(contracts)
        for conid, (position, contract, ticker) in list(self._streaming.items()):
            new_prev = fresh.get(conid)
            if new_prev is None or new_prev <= 0:
                continue
            if new_prev == position.previous_close:
                continue
            new_position = replace(position, previous_close=new_prev)
            self._streaming[conid] = (new_position, contract, ticker)
            self._live_positions.set_position(new_position)

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
            last_price_is_broker_mark=False,
            last_price_is_delayed=_ticker_is_delayed(ticker),
            last_price_is_stale=False,
        )
        self._streaming[conid] = (new_position, contract, ticker)
        self._live_positions.set_position(new_position)

    def _on_portfolio_update(self, item) -> None:
        """Refresh live rows from IB's account-level portfolio valuation feed.

        This feed is important for exchanges where `reqMktData`/historical
        calls require permissions the account does not have. IB still values
        the held position in the account portfolio; use that as a broker
        mark instead of rendering the row as unknown.
        """
        if self._live_positions is None:
            return
        contract = getattr(item, "contract", None)
        try:
            conid = int(getattr(contract, "conId", 0))
        except (TypeError, ValueError):
            return
        if not conid:
            return
        old_position = next(
            (
                p for p in self._live_positions.get_all()
                if p.broker == self.name and p.native_key == str(conid)
            ),
            None,
        )
        if old_position is None or old_position.asset_class != "STK":
            return
        if (
            conid in self._streaming
            and not old_position.last_price_is_broker_mark
            and old_position.last_price > 0
        ):
            return

        pm = old_position.price_magnifier
        last_price = _portfolio_display_price(
            item,
            pm,
            quantity=old_position.quantity,
        )
        mv_native = _portfolio_market_value(item)
        pnl_native = _portfolio_unrealized_pnl(item)
        avg_cost = _portfolio_average_cost(item, pm)
        if last_price is None:
            last_price = old_position.last_price
        if mv_native is None:
            mv_native = old_position.market_value_native
        if pnl_native is None:
            pnl_native = old_position.unrealized_pnl_native
        if avg_cost is None:
            avg_cost = old_position.avg_cost
        if (
            last_price == old_position.last_price
            and mv_native == old_position.market_value_native
            and pnl_native == old_position.unrealized_pnl_native
            and avg_cost == old_position.avg_cost
        ):
            return
        conv = self._convert_to_usd(old_position.currency, mv_native, pnl_native)
        new_position = replace(
            old_position,
            avg_cost=avg_cost,
            last_price=last_price,
            market_value_native=mv_native,
            unrealized_pnl_native=pnl_native,
            market_value_usd=conv.mv_usd,
            unrealized_pnl_usd=conv.pnl_usd,
            fx_is_stale=conv.fx_is_stale,
            fx_is_fallback=conv.fx_is_fallback,
            fx_unavailable=conv.fx_unavailable,
            last_price_is_previous_close=False,
            last_price_is_broker_mark=True,
            last_price_is_delayed=False,
            last_price_is_stale=False,
        )
        if conid in self._streaming:
            _old_stream_position, stream_contract, ticker = self._streaming[conid]
            self._streaming[conid] = (new_position, stream_contract, ticker)
        self._live_positions.set_position(new_position)

    # ---- fills (slice 11) ---------------------------------------------------

    async def _req_executions(self):
        """Thin wrapper around IB.reqExecutionsAsync used by the EOD reconcile
        job. Returns the broker's recent executions (last ~24-48h). Empty list
        if not connected. Test fakes can override this method directly without
        touching the underlying IB.

        Passes an explicit ExecutionFilter() so we don't depend on whatever
        default ib_async chooses for missing filter args — a future release
        could expand the window unexpectedly.
        """
        if self._ib is None:
            return []
        req = getattr(self._ib, "reqExecutionsAsync", None)
        if not callable(req):
            return []
        # Lazy import: tests use stub adapters that override _req_executions
        # without ib_async installed in the path.
        from ib_async import ExecutionFilter
        return await req(ExecutionFilter())

    def _on_exec_details(self, trade, fill) -> None:
        """Synchronous ib_async callback for execDetailsEvent.

        Builds the Store-shaped row and schedules an async INSERT. Store
        access is async so we can't await inline; create_task runs the
        write on the same event loop. Failures log and are dropped — the
        EOD reconcile job is the backstop for missed live fills.
        """
        if self._store is None:
            return
        try:
            row = build_fill_row(broker=self.name, fill=fill, fx_service=self._fx_service)
        except Exception as exc:
            _LOG.warning("Failed to build fill row for exec %s: %s",
                         getattr(getattr(fill, "execution", None), "execId", "?"), exc)
            return
        if row is None:
            return
        # Hold a strong ref in _pending_writes; without this the loop's
        # weak-ref bookkeeping can GC the task mid-INSERT under load.
        task = asyncio.create_task(self._persist_fill(row))
        self._pending_writes.add(task)
        task.add_done_callback(self._pending_writes.discard)

    async def _persist_fill(self, row: dict) -> None:
        try:
            inserted = await self._store.insert_fill(**row)
        except Exception as exc:
            _LOG.warning("insert_fill failed for exec %s: %s",
                         row.get("execution_id", "?"), exc)
            return
        if inserted:
            _LOG.info(
                "Captured fill exec=%s symbol=%s side=%s",
                row["execution_id"], row["canonical_symbol"], row["side"],
            )

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
            # Slice 10: GrossPositionValue (sum of |market value| across STK)
            # is needed for equity_snapshots. Missing tag → 0.0; many older
            # accounts don't report it and we don't want to crash the loop.
            gpv_value, gpv_ccy = by_acc_tag.get((acc, "GrossPositionValue"), ("0", "USD"))
            base_ccy = nlv_ccy  # NLV's reported currency is the account's base
            nlv_native = _safe_float(nlv_value)
            nlv_usd = self._to_usd(nlv_native, nlv_ccy)
            cash_usd = self._to_usd(_safe_float(cash_value), cash_ccy)
            bp_usd = self._to_usd(_safe_float(bp_value), bp_ccy)
            gpv_usd = self._to_usd(_safe_float(gpv_value), gpv_ccy)
            out.append(
                AccountSummary(
                    broker=self.name,
                    account_id=acc,
                    base_currency=base_ccy,
                    net_liquidation_usd=nlv_usd,
                    cash_usd=cash_usd,
                    buying_power_usd=bp_usd,
                    net_liquidation_native=nlv_native,
                    gross_position_value_usd=gpv_usd,
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


def _ticker_is_delayed(ticker) -> bool:
    try:
        market_data_type = int(getattr(ticker, "marketDataType", 1))
    except (TypeError, ValueError):
        return False
    return market_data_type in {2, 3, 4}


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


def _to_finite_float(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f


def _portfolio_display_price(
    item,
    price_magnifier: int,
    *,
    quantity: float | None = None,
) -> float | None:
    if item is None:
        return None
    price = _to_positive_float(getattr(item, "marketPrice", None))
    if price is None and quantity not in (None, 0):
        market_value = _portfolio_market_value(item)
        if market_value is not None:
            price = abs(market_value / float(quantity))
    if price is None:
        return None
    return price * price_magnifier


def _portfolio_average_cost(item, price_magnifier: int) -> float | None:
    if item is None:
        return None
    value = _to_positive_float(getattr(item, "averageCost", None))
    if value is None:
        return None
    return value * price_magnifier


def _portfolio_market_value(item) -> float | None:
    if item is None:
        return None
    return _to_finite_float(getattr(item, "marketValue", None))


def _portfolio_unrealized_pnl(item) -> float | None:
    if item is None:
        return None
    return _to_finite_float(getattr(item, "unrealizedPNL", None))


def _is_historical_permission_gated(exchange: str | None) -> bool:
    return str(exchange or "").strip() in _IB_HISTORICAL_PERMISSION_GATED_EXCHANGES


def _is_live_quote_permission_gated(exchange: str | None) -> bool:
    return str(exchange or "").strip() in _IB_LIVE_QUOTE_PERMISSION_GATED_EXCHANGES


def _is_streaming_permission_gated(exchange: str | None) -> bool:
    return str(exchange or "").strip() in _IB_STREAMING_PERMISSION_GATED_EXCHANGES


def _first_known_listing_exchange(
    details_primary_exchange: object,
    details_exchange: object,
    contract_primary_exchange: object,
    contract_exchange: object,
) -> str | None:
    """Return the first known IB listing exchange from contract fields.

    IB usually supplies ContractDetails.contract.primaryExchange, but some
    holdings surface their venue only in exchange. SMART is a routing
    destination, not a listing venue, so it is never accepted as fallback.
    Unknown non-routing values are treated as authoritative failures rather
    than skipped, because a later fallback could misclassify the instrument.
    """
    primary_candidates = (details_primary_exchange, contract_primary_exchange)
    fallback_candidates = (details_exchange, contract_exchange)
    for candidate in primary_candidates + fallback_candidates:
        exchange = str(candidate or "").strip()
        if not exchange or exchange == "SMART":
            continue
        if exchange in IB_EXCHANGE_TO_SUFFIX:
            return exchange
        return None
    return None


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
