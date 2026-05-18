"""Broker-agnostic fill normalization.

Adapters (IBKR today; Futu / Tiger / Longbridge later) hand us a fill-like
object — `contract`, `execution`, `commissionReport` attributes — and this
module turns it into the kwargs dict for `Store.insert_fill`. Keeping the
conversion in `core/` means new adapters don't import from each other or
duplicate the mapping, and the EOD reconcile job (in `jobs/`) depends on
this module rather than on a specific adapter.

The fill object only needs to walk like an ib_async Fill: dataclass-style
duck typing on these attributes:

  fill.execution.execId          : str
  fill.execution.acctNumber      : str
  fill.execution.side            : "BOT" | "SLD"
  fill.execution.shares          : float
  fill.execution.price           : float
  fill.execution.time            : datetime (UTC-aware)
  fill.contract.symbol           : str
  fill.contract.primaryExchange  : str
  fill.contract.currency         : str
  fill.contract.secType          : "STK" | "CASH" | ...
  fill.contract.conId            : int
  fill.commissionReport.commission : float

Adapters whose vendor SDK uses different shapes wrap their objects in a
shim that exposes these attributes before calling build_fill_row.
"""

import logging

from app.core.symbols import canonical_symbol


_LOG = logging.getLogger(__name__)

# IB-style side strings → portable BUY/SELL. Other brokers either emit the
# same strings (most do — it's a TWS convention) or their adapter translates
# before constructing the fill shim.
_SIDE_MAP = {"BOT": "BUY", "SLD": "SELL"}


def build_fill_row(*, broker: str, fill, fx_service) -> dict | None:
    """Convert a vendor Fill into the kwargs dict for Store.insert_fill,
    snapshotting the current FX rate at fill time.

    Returns None when the fill should be dropped (unknown exchange or
    unrecognised side string). USD fills get fx_rate_at_fill=None and
    fees_usd == fees_native; non-USD fills convert via fx_service. If the
    FX rate is missing for a non-USD trade, fx_rate_at_fill and fees_usd
    are recorded as NULL — we still preserve the trade history.
    """
    execution = fill.execution
    contract = fill.contract
    commission_report = fill.commissionReport

    side = _SIDE_MAP.get(execution.side)
    if side is None:
        _LOG.warning("Unknown execution side %r — dropping fill %s",
                     execution.side, execution.execId)
        return None

    primary = getattr(contract, "primaryExchange", "") or ""
    try:
        canonical = canonical_symbol(contract.symbol, primary)
    except ValueError:
        _LOG.warning(
            "Unknown primary exchange %r on fill %s — dropping",
            primary, execution.execId,
        )
        return None

    currency = contract.currency or "USD"
    fees_native = float(commission_report.commission or 0.0)
    fx_rate: float | None = None
    fees_usd: float | None = None
    if currency == "USD":
        fees_usd = fees_native
    elif fx_service is not None:
        rate = fx_service.get_rate_sync(currency)
        if rate is not None:
            fx_rate = float(rate.rate)
            fees_usd = fees_native * fx_rate

    return {
        "broker": broker,
        "account_id": execution.acctNumber,
        "execution_id": execution.execId,
        "canonical_symbol": canonical,
        "native_key": str(contract.conId),
        "asset_class": contract.secType,
        "side": side,
        "quantity": float(execution.shares),
        "price": float(execution.price),
        "currency": currency,
        "fx_rate_at_fill": fx_rate,
        "fees_native": fees_native,
        "fees_usd": fees_usd,
        "filled_at": execution.time,
    }
