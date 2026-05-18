"""Slice 10 cycle 2: AccountSummary carries net_liquidation_native +
gross_position_value_usd so the snapshot capture has everything the
equity_snapshots row needs without re-querying the broker.

- New fields default-initialize to 0.0 so existing test stubs continue to
  construct AccountSummary without specifying them.
- IBKR adapter populates them from `accountSummaryAsync`: raw NLV value
  becomes net_liquidation_native; GrossPositionValue tag is fetched and
  converted to USD.
"""

from dataclasses import dataclass

import pytest


# Dataclass shape ------------------------------------------------------------


def test_account_summary_has_new_fields_with_defaults():
    """Existing constructors that omit the new fields must keep working."""
    from app.core.broker import AccountSummary

    s = AccountSummary(
        broker="IBKR", account_id="U1", base_currency="USD",
        net_liquidation_usd=100.0, cash_usd=20.0, buying_power_usd=400.0,
    )
    assert s.net_liquidation_native == 0.0
    assert s.gross_position_value_usd == 0.0


def test_account_summary_new_fields_set_explicitly():
    from app.core.broker import AccountSummary

    s = AccountSummary(
        broker="IBKR", account_id="U1", base_currency="HKD",
        net_liquidation_usd=10_000.0, cash_usd=2_000.0, buying_power_usd=40_000.0,
        net_liquidation_native=78_000.0,
        gross_position_value_usd=8_000.0,
    )
    assert s.net_liquidation_native == 78_000.0
    assert s.gross_position_value_usd == 8_000.0


# IBKR adapter populates them ------------------------------------------------


# Lightweight fakes mirroring the existing adapter test pattern --------------


@dataclass
class _Row:
    account: str
    tag: str
    value: str
    currency: str


class _FakeIB:
    def __init__(self, rows):
        self._rows = rows
        self._connected = True
        self._managed = ["U1"]

    def managedAccounts(self): return self._managed
    def isConnected(self): return self._connected
    async def accountSummaryAsync(self): return list(self._rows)
    async def reqPositionsAsync(self): return []


class _FxStub:
    """get_rate_sync returns a fixed FxRate-shaped object."""
    def __init__(self, rates):
        self._rates = rates
    def attach_ib(self, ib): pass
    def get_rate_sync(self, currency):
        if currency == "USD":
            return type("R", (), {"rate": 1.0, "is_stale": False, "source": "IB"})()
        r = self._rates.get(currency)
        if r is None:
            return None
        return type("R", (), {"rate": r, "is_stale": False, "source": "IB"})()


async def test_adapter_populates_native_nlv_and_gross_position_value():
    """For a HKD-base account, native NLV should be the raw HKD figure
    IB reported, and gross_position_value_usd should be the GrossPositionValue
    tag converted via the FX rate (HKD→USD ≈ 0.1283)."""
    from app.adapters.ibkr import IbkrAdapter

    rows = [
        _Row("U1", "NetLiquidation",     "780000",  "HKD"),
        _Row("U1", "TotalCashValue",     "100000",  "HKD"),
        _Row("U1", "BuyingPower",        "1560000", "HKD"),
        _Row("U1", "GrossPositionValue", "680000",  "HKD"),
    ]
    ib = _FakeIB(rows)
    adapter = IbkrAdapter(
        host="ib", port=4003, client_id=1,
        ib_factory=lambda: ib,
        fx_service=_FxStub({"HKD": 0.1283}),
    )
    # Skip the network path by injecting _ib directly.
    adapter._ib = ib

    summaries = await adapter.get_account_summary()
    assert len(summaries) == 1
    s = summaries[0]
    assert s.base_currency == "HKD"
    assert s.net_liquidation_native == pytest.approx(780_000.0)
    assert s.net_liquidation_usd == pytest.approx(780_000.0 * 0.1283)
    assert s.gross_position_value_usd == pytest.approx(680_000.0 * 0.1283)


async def test_adapter_handles_missing_gross_position_value_tag():
    """If IB doesn't return GrossPositionValue (older accounts, perm denied),
    gross_position_value_usd is 0.0 — never None, never raises."""
    from app.adapters.ibkr import IbkrAdapter

    rows = [
        _Row("U1", "NetLiquidation", "100000", "USD"),
        _Row("U1", "TotalCashValue", "30000",  "USD"),
        _Row("U1", "BuyingPower",    "200000", "USD"),
        # Note: no GrossPositionValue row.
    ]
    ib = _FakeIB(rows)
    adapter = IbkrAdapter(
        host="ib", port=4003, client_id=1, ib_factory=lambda: ib,
        fx_service=_FxStub({}),
    )
    adapter._ib = ib

    summaries = await adapter.get_account_summary()
    assert summaries[0].gross_position_value_usd == 0.0
    assert summaries[0].net_liquidation_native == 100_000.0
    assert summaries[0].net_liquidation_usd == 100_000.0
