"""Slice 11 cycle 2: convert an ib_async Fill into Store.insert_fill kwargs.

ib_async fires `execDetailsEvent(trade, fill)` where `fill` is a NamedTuple
of (contract, execution, commissionReport, time). We need a pure-function
adapter that turns those four objects + an FX service into the dict of
kwargs the Store accepts.

Behaviour:
- `Execution.side` ("BOT"/"SLD") becomes "BUY"/"SELL".
- `canonical_symbol` derived via the symbols module from (symbol,
  primaryExchange). Unknown exchanges return None to skip the fill.
- USD fills get `fx_rate_at_fill=None` (no conversion needed) and
  `fees_usd = fees_native`.
- Non-USD fills snapshot the current FX rate at fill time; `fees_usd`
  is computed via that rate. Missing FX rate falls back to None for
  both `fx_rate_at_fill` and `fees_usd` (we record the fill, but the
  USD-derived values are unavailable).
- `filled_at` is the execution.time; we pass it through as-is (caller
  is responsible for ensuring it's UTC-aware, ib_async returns aware).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest


# --- IB-shape fakes (just enough surface for the converter) -----------------


@dataclass
class FakeContract:
    conId: int = 0
    symbol: str = ""
    secType: str = "STK"
    currency: str = "USD"
    primaryExchange: str = ""


@dataclass
class FakeExecution:
    execId: str = ""
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc))
    acctNumber: str = "U1"
    side: str = "BOT"
    shares: float = 0.0
    price: float = 0.0


@dataclass
class FakeCommissionReport:
    execId: str = ""
    commission: float = 0.0
    currency: str = "USD"


@dataclass
class FakeFill:
    contract: FakeContract
    execution: FakeExecution
    commissionReport: FakeCommissionReport
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc))


class FxStub:
    """Mimics the FxService.get_rate_sync() surface — returns the rate or None."""

    def __init__(self, rates: dict[str, float | None]):
        self._rates = rates

    def get_rate_sync(self, currency: str):
        rate = self._rates.get(currency)
        if rate is None:
            return None
        # Mirror the real FxRate shape just enough for the converter.
        return type("FxRate", (), {"rate": rate, "is_stale": False, "source": "IB",
                                   "pair": currency + "USD"})()


def _fill(*, currency="USD", side="BOT", shares=100.0, price=420.0,
          commission=1.5, comm_currency=None, symbol="700", primary="SEHK",
          conId=76792991, exec_id="exec-1", acct="U7575980"):
    contract = FakeContract(
        conId=conId, symbol=symbol, secType="STK",
        currency=currency, primaryExchange=primary,
    )
    execution = FakeExecution(
        execId=exec_id, acctNumber=acct, side=side, shares=shares, price=price,
        time=datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc),
    )
    commissionReport = FakeCommissionReport(
        execId=exec_id, commission=commission,
        currency=comm_currency or currency,
    )
    return FakeFill(contract=contract, execution=execution,
                    commissionReport=commissionReport)


# --- Tests ------------------------------------------------------------------


def test_buy_side_maps_to_BUY():
    from app.adapters.ibkr import build_fill_row

    f = _fill(side="BOT", currency="USD")
    row = build_fill_row(broker="IBKR", fill=f, fx_service=None)
    assert row["side"] == "BUY"


def test_sell_side_maps_to_SELL():
    from app.adapters.ibkr import build_fill_row

    f = _fill(side="SLD", currency="USD")
    row = build_fill_row(broker="IBKR", fill=f, fx_service=None)
    assert row["side"] == "SELL"


def test_unknown_side_returns_none():
    """A weird side string (defensive) — don't write a bogus row."""
    from app.adapters.ibkr import build_fill_row

    f = _fill(side="???", currency="USD")
    assert build_fill_row(broker="IBKR", fill=f, fx_service=None) is None


def test_usd_fill_has_null_fx_rate_and_fees_usd_equals_native():
    from app.adapters.ibkr import build_fill_row

    f = _fill(currency="USD", commission=1.5, symbol="AAPL", primary="NASDAQ")
    row = build_fill_row(broker="IBKR", fill=f, fx_service=None)
    assert row["fx_rate_at_fill"] is None
    assert row["fees_native"] == 1.5
    assert row["fees_usd"] == 1.5


def test_non_usd_fill_snapshots_current_fx_rate():
    """A HKD fill should record the current HKDUSD rate and convert fees."""
    from app.adapters.ibkr import build_fill_row

    fx = FxStub({"HKD": 0.1283})
    f = _fill(currency="HKD", commission=15.0, symbol="700", primary="SEHK")
    row = build_fill_row(broker="IBKR", fill=f, fx_service=fx)
    assert row["fx_rate_at_fill"] == pytest.approx(0.1283)
    assert row["fees_native"] == 15.0
    assert row["fees_usd"] == pytest.approx(15.0 * 0.1283)


def test_non_usd_fill_with_missing_fx_records_nulls():
    """If FX is unavailable for the trade currency, we still record the fill
    (don't drop trade history!) but leave the USD-derived fields NULL."""
    from app.adapters.ibkr import build_fill_row

    fx = FxStub({"HKD": None})
    f = _fill(currency="HKD", commission=15.0, symbol="700", primary="SEHK")
    row = build_fill_row(broker="IBKR", fill=f, fx_service=fx)
    assert row["fx_rate_at_fill"] is None
    assert row["fees_usd"] is None
    assert row["fees_native"] == 15.0  # always preserved


def test_canonical_symbol_derived_from_primary_exchange():
    from app.adapters.ibkr import build_fill_row

    f = _fill(symbol="700", primary="SEHK", currency="HKD")
    row = build_fill_row(broker="IBKR", fill=f, fx_service=FxStub({"HKD": 0.1283}))
    assert row["canonical_symbol"] == "700.HK"
    assert row["native_key"] == "76792991"


def test_unknown_primary_exchange_returns_none():
    """If primaryExchange isn't in IB_EXCHANGE_TO_SUFFIX, skip the fill rather
    than write an ambiguous canonical_symbol."""
    from app.adapters.ibkr import build_fill_row

    f = _fill(symbol="AAPL", primary="MADE_UP_EXCH", currency="USD")
    assert build_fill_row(broker="IBKR", fill=f, fx_service=None) is None


def test_round_trip_passes_through_quantity_price_account_execid():
    from app.adapters.ibkr import build_fill_row

    f = _fill(shares=42.0, price=183.55, exec_id="abc.0001",
              acct="U1234567", symbol="AAPL", primary="NASDAQ",
              currency="USD")
    row = build_fill_row(broker="IBKR", fill=f, fx_service=None)
    assert row["quantity"] == 42.0
    assert row["price"] == 183.55
    assert row["execution_id"] == "abc.0001"
    assert row["account_id"] == "U1234567"
    assert row["currency"] == "USD"
    assert row["asset_class"] == "STK"


def test_filled_at_pulled_from_execution_time():
    from app.adapters.ibkr import build_fill_row

    f = _fill(currency="USD")
    row = build_fill_row(broker="IBKR", fill=f, fx_service=None)
    assert row["filled_at"] == datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc)
