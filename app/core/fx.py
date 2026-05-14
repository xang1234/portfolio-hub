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
from typing import Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.store import Store


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

    def __init__(self, *, store: "Store | None" = None) -> None:
        self._store = store
        self._rates: dict[str, FxRate] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._store is None:
            return
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

    async def stop(self) -> None:
        pass

    async def get_rate(self, currency: str) -> FxRate | None:
        if currency == "USD":
            return FxRate(
                pair="USDUSD",
                rate=1.0,
                quoted_at=datetime.now(timezone.utc),
                is_stale=False,
                source="IB",
            )
        validate_currency(currency)
        return self._rates.get(currency)

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
