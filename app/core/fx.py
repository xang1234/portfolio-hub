"""FX subsystem: convert native currency amounts to USD.

Slice 3 surface (this module grows across cycles 1-7):
  - FxRate: an immutable quote with source + staleness metadata
  - SUPPORTED_FX: the eleven currencies we subscribe to via IB
  - validate_currency: boundary validator that rejects CNY explicitly

Subsequent cycles add FxService (in-memory cache + IB subscriptions),
public-API fallback, staleness logic, and the auto-switch between IB
and API sources.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.store import Store


def _default_forex_factory(currency: str) -> Any:
    """Build an ib_async Forex contract for `currency` against USD.

    Imported lazily so tests don't need ib_async on PYTHONPATH.
    """
    from ib_async import Forex
    return Forex(f"{currency}USD")


_API_URL = "https://open.er-api.com/v6/latest/USD"
_API_DEFAULT_POLL_INTERVAL_S = 3600.0


async def _default_api_fetcher() -> dict | None:
    """Fetch USD-base rates from open.er-api.com.

    Returns the parsed JSON dict, or None on any failure (network error,
    non-200 status, bad JSON). Callers must handle None.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(_API_URL)
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        _LOG.warning("Public-API FX fetch failed: %s", exc)
        return None


_LOG = logging.getLogger(__name__)


# Currencies we actively quote against USD. HKD is included despite the peg
# because the per-row code path is uniform regardless of whether the rate is
# pegged or floating — special-casing peg semantics here would bleed into
# every consumer.
#
# CNY is INTENTIONALLY excluded: IB returns CNH (offshore RMB) for the
# Stock-Connect HK-routed A-shares we hold, and silently substituting CNY
# (onshore mainland RMB) would mis-value those positions because the two
# rates can diverge by 1-3% under normal conditions.
SUPPORTED_FX: frozenset[str] = frozenset({
    "HKD", "JPY", "KRW", "TWD", "CNH",
    "AUD", "GBP", "EUR", "SGD", "CHF", "CAD",
})


@dataclass(frozen=True)
class FxRate:
    """A single FX quote against USD with the metadata a row needs to render.

    `pair` is the canonical IB-style pair name (e.g. "HKDUSD"). `rate` is
    the multiplier — `amount_native * rate = amount_usd`.

    `source` distinguishes IB-streamed quotes from API-fallback values so
    the 📡 badge can appear on rows backed by the fallback (which doesn't
    update tick-by-tick).
    """

    pair: str
    rate: float
    quoted_at: datetime
    is_stale: bool
    source: Literal["IB", "API_FALLBACK"]


def _pair_for(currency: str) -> str:
    """Convention: 'HKDUSD' means 'how many USD per HKD'."""
    return f"{currency}USD"


# Staleness threshold: an IB rate is considered stale when this much time has
# elapsed AND the FX market is open. Below this we trust the IB tick is just
# briefly delayed; above this it suggests the subscription has stopped.
_IB_STALENESS_SECONDS = 60.0


def is_fx_market_open(at: datetime) -> bool:
    """Return True if FX trading is in session at the given UTC moment.

    Spot FX runs Sunday 22:00 UTC through Friday 22:00 UTC. Saturday is
    fully closed; Sunday before 22:00 UTC is closed (the Sydney open
    starts ~22:00 UTC = ~8am AEST/AEDT in northern hemisphere terms).
    """
    weekday = at.weekday()  # Mon=0 .. Sun=6
    if weekday == 5:  # Saturday
        return False
    if weekday == 6:  # Sunday — open after 22:00 UTC
        return at.hour >= 22
    if weekday == 4:  # Friday — closed after 22:00 UTC
        return at.hour < 22
    return True  # Monday-Thursday: open all day


def validate_currency(code: str) -> None:
    """Raise ValueError if `code` is not USD or a supported FX currency.

    CNY gets a dedicated message because the most common reason for hitting
    this path is mis-configuration where someone thinks CNY and CNH are
    interchangeable. They aren't.
    """
    if code == "USD":
        return
    if code == "CNY":
        raise ValueError(
            "CNY not supported — use CNH for offshore renminbi (IB returns CNH "
            "for Stock-Connect A-shares; CNY and CNH can diverge 1-3%)"
        )
    if code not in SUPPORTED_FX:
        raise ValueError(
            f"Currency {code!r} not in SUPPORTED_FX={sorted(SUPPORTED_FX)}"
        )


class FxService:
    """In-memory cache of FX rates with optional persistence.

    Cycle 3 surface (this class grows across cycles 3-7):
      - start() / stop(): lifecycle. start() loads fx_cache from Store.
      - get_rate(currency) → FxRate | None
      - convert(amount, currency) → float | None
      - set_rate(rate): test hook; production callers (IB tick handler,
        API fallback) go through the same method.

    USD is the base currency: get_rate('USD') returns a synthetic 1.0
    rate, convert(x, 'USD') returns x. This keeps callers from needing
    to special-case USD-denominated rows.
    """

    def __init__(
        self,
        *,
        store: "Store | None" = None,
        ib: Any | None = None,
        forex_factory: Callable[[str], Any] = _default_forex_factory,
        api_fetcher: Callable[[], Any] | None = _default_api_fetcher,
        api_poll_interval_s: float = _API_DEFAULT_POLL_INTERVAL_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._ib = ib
        self._forex_factory = forex_factory
        self._api_fetcher = api_fetcher
        self._api_poll_interval_s = api_poll_interval_s
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rates: dict[str, FxRate] = {}
        self._subscribed: set[str] = set()
        self._tickers: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._api_task: asyncio.Task | None = None

    async def start(self) -> None:
        # Load last-known rates from disk so USD columns are populated before
        # the first fresh IB tick or API poll arrives.
        if self._store is not None:
            for currency in SUPPORTED_FX:
                pair = _pair_for(currency)
                row = await self._store.get_fx_rate(pair)
                if row is None:
                    continue
                self._rates[currency] = FxRate(
                    pair=row["pair"],
                    rate=row["rate"],
                    quoted_at=row["quoted_at"],
                    is_stale=False,
                    source=row["source"],
                )
        # Start the API fallback poll loop (cancellable via stop()).
        if self._api_fetcher is not None:
            self._api_task = asyncio.create_task(self._api_poll_loop())

    async def stop(self) -> None:
        task = self._api_task
        self._api_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def get_rate(self, currency: str) -> FxRate | None:
        if currency == "USD":
            return FxRate(
                pair="USDUSD",
                rate=1.0,
                quoted_at=self._clock(),
                is_stale=False,
                source="IB",
            )
        validate_currency(currency)
        cached = self._rates.get(currency)
        if cached is None:
            return None
        # Recompute staleness at read time. Stored rates have is_stale=False
        # because staleness depends on current time, not when the tick arrived.
        is_stale = self._compute_staleness(cached)
        if is_stale == cached.is_stale:
            return cached
        return FxRate(
            pair=cached.pair, rate=cached.rate, quoted_at=cached.quoted_at,
            is_stale=is_stale, source=cached.source,
        )

    def _compute_staleness(self, rate: FxRate) -> bool:
        """An IB rate is stale when it's older than 60s AND the FX market
        is currently open. API_FALLBACK is never marked stale (it's
        deliberately a slow source)."""
        if rate.source != "IB":
            return False
        now = self._clock()
        if not is_fx_market_open(now):
            return False
        return (now - rate.quoted_at).total_seconds() > _IB_STALENESS_SECONDS

    async def convert(self, amount: float, currency: str) -> float | None:
        if currency == "USD":
            return amount
        rate = await self.get_rate(currency)
        if rate is None:
            return None
        return amount * rate.rate

    async def set_rate(self, rate: FxRate) -> None:
        currency = rate.pair[:3]
        validate_currency(currency)
        async with self._lock:
            self._rates[currency] = rate
        if self._store is not None:
            try:
                await self._store.put_fx_rate(
                    pair=rate.pair,
                    rate=rate.rate,
                    source=rate.source,
                    quoted_at=rate.quoted_at,
                )
            except Exception as exc:
                _LOG.warning("Failed to persist FX rate %s: %s", rate.pair, exc)

    # ---- IB subscriptions (cycle 4) -----------------------------------------

    async def ensure_subscribed(self, currencies: set[str]) -> None:
        """Subscribe to Forex(XXXUSD) for each currency we haven't already
        subscribed to. Idempotent. No-op when no IB instance was injected
        (e.g. unit tests that seed rates directly)."""
        if self._ib is None:
            return
        for currency in currencies:
            if currency == "USD":
                continue
            if currency in self._subscribed:
                continue
            validate_currency(currency)
            contract = self._forex_factory(currency)
            ticker = self._ib.reqMktData(contract, "", False, False)
            ticker.updateEvent += self._make_ticker_handler(currency)
            self._tickers[currency] = ticker
            self._subscribed.add(currency)
            _LOG.info("Subscribed to FX pair %sUSD via IB", currency)

    def _make_ticker_handler(self, currency: str):
        """Return a closure that handles updateEvent for the given currency.

        The in-memory cache is updated synchronously so the next get_rate()
        sees the new value immediately. Persistence to fx_cache is scheduled
        on the event loop because put_fx_rate is async.
        """
        def _handler(ticker):
            price = _extract_forex_price(ticker)
            if price is None:
                return
            rate = FxRate(
                pair=f"{currency}USD",
                rate=price,
                quoted_at=datetime.now(timezone.utc),
                is_stale=False,
                source="IB",
            )
            self._rates[currency] = rate  # synchronous in-memory update
            if self._store is None:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # no loop (test teardown) — in-memory update suffices
            loop.create_task(self._persist_rate(rate))
        return _handler

    async def _persist_rate(self, rate: FxRate) -> None:
        try:
            await self._store.put_fx_rate(
                pair=rate.pair,
                rate=rate.rate,
                source=rate.source,
                quoted_at=rate.quoted_at,
            )
        except Exception as exc:
            _LOG.warning("Failed to persist FX rate %s: %s", rate.pair, exc)

    # ---- API fallback (cycle 5) ---------------------------------------------

    async def refresh_from_api(self) -> None:
        """One-shot fetch from the public API. Applies returned rates as
        FxRates with source=API_FALLBACK. Silently skips on any error so
        callers don't need to wrap in try/except.

        CNH is NOT applied even if CNY appears in the response — the two
        rates can diverge 1-3% and IB returns CNH for our positions.
        """
        if self._api_fetcher is None:
            return
        try:
            payload = await self._api_fetcher()
        except Exception as exc:
            _LOG.warning("API fallback fetcher raised: %s", exc)
            return
        if not payload or not isinstance(payload, dict):
            return
        rates = payload.get("rates")
        if not isinstance(rates, dict):
            return
        quoted_at = datetime.now(timezone.utc)
        for currency in SUPPORTED_FX:
            if currency == "CNH":
                # API exposes CNY only; never substitute.
                continue
            inverse = rates.get(currency)
            if not isinstance(inverse, (int, float)) or inverse <= 0:
                continue
            usd_per_native = 1.0 / float(inverse)
            await self.set_rate(FxRate(
                pair=_pair_for(currency),
                rate=usd_per_native,
                quoted_at=quoted_at,
                is_stale=False,
                source="API_FALLBACK",
            ))

    async def _api_poll_loop(self) -> None:
        """Periodically refresh from the public API. Survives transient
        failures; exits on cancellation."""
        try:
            while True:
                try:
                    await self.refresh_from_api()
                except Exception as exc:
                    _LOG.warning("API poll iteration failed: %s", exc)
                await asyncio.sleep(self._api_poll_interval_s)
        except asyncio.CancelledError:
            raise


def _extract_forex_price(ticker) -> float | None:
    """Pull the best-available FX rate out of a ticker.

    Preference order: midpoint(bid,ask) → last → marketPrice(). The midpoint
    is the most consistent for Forex (last trade can lag during quiet hours).
    """
    bid = getattr(ticker, "bid", None)
    ask = getattr(ticker, "ask", None)
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    last = getattr(ticker, "last", None)
    if last is not None and last > 0:
        return float(last)
    market_price = getattr(ticker, "marketPrice", None)
    if callable(market_price):
        mp = market_price()
        if mp is not None and mp > 0:
            return float(mp)
    return None
