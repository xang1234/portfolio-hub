"""Slice 7 cycle 1: IbkrAdapter.get_account_summary returns one
AccountSummary per linked IBKR account, populated from IB
accountSummary calls and converted to USD via FxService.

The AccountSummary contract:
  - broker  = "IBKR"
  - account_id = the linked account number (e.g. "U1234567")
  - base_currency = whatever IB reports for that account (often USD;
    can be HKD/EUR/etc. on Asian/European desks)
  - net_liquidation_usd / cash_usd / buying_power_usd — converted from
    the base-currency values via the same FxService used for positions

Multiple linked accounts (taxable + IRA, or two Asian desks) return
multiple AccountSummary rows. Account IDs come from IB's
managedAccounts(), not derived from position rows (some accounts may
hold no positions).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from app.core.broker import AccountSummary
from app.core.fx import FxRate, FxService


# Test doubles ------------------------------------------------------------


@dataclass
class FakeAccountValue:
    """Mirrors ib_async's AccountValue dataclass shape."""
    account: str
    tag: str
    value: str
    currency: str = "USD"


@dataclass
class FakeContract:
    conId: int = 0
    symbol: str = ""
    secType: str = ""
    currency: str = ""
    exchange: str = ""
    primaryExchange: str = ""


@dataclass
class FakeIBPosition:
    account: str = "UNKNOWN"
    contract: FakeContract = field(default_factory=FakeContract)
    position: float = 0.0
    avgCost: float = 0.0


class FakeIB:
    """Minimal IB stub for the account-summary path.

    `accountSummaryAsync` returns a flat list of AccountValue rows —
    real IB uses one row per (account, tag) combination, e.g.
    ('U1', 'NetLiquidation', '100000', 'USD') and
    ('U1', 'TotalCashValue', '20000', 'USD').
    """

    def __init__(self, *, accounts, summary_rows, positions=None):
        self._accounts = list(accounts)
        self._summary = list(summary_rows)
        self._positions = list(positions or [])
        self._connected = False
        self.summary_calls = 0

    async def connectAsync(self, host, port, clientId):  # noqa: N802, N803
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isConnected(self):  # noqa: N802
        return self._connected

    def managedAccounts(self):  # noqa: N802
        return self._accounts

    async def accountSummaryAsync(self, account=""):  # noqa: N802
        self.summary_calls += 1
        if account:
            return [r for r in self._summary if r.account == account]
        return list(self._summary)

    # Position-shape stubs so get_positions() can run when needed
    async def reqPositionsAsync(self):  # noqa: N802
        return self._positions

    async def reqTickersAsync(self, *contracts):  # noqa: N802
        return []


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store
    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


async def _make_adapter(fake_ib, store, fx_svc=None):
    from app.adapters.ibkr import IbkrAdapter
    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: fake_ib, store=store, fx_service=fx_svc,
    )
    await adapter.connect()
    return adapter


def _seed_rate(fx_svc, currency, rate):
    fx_svc._ib_rates[currency] = FxRate(
        pair=f"{currency}USD", rate=rate, is_stale=False, source="IB",
        quoted_at=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
    )


# One account, USD base ----------------------------------------------------


async def test_single_account_returns_one_summary(store):
    fake_ib = FakeIB(
        accounts=["U1234567"],
        summary_rows=[
            FakeAccountValue("U1234567", "NetLiquidation", "100000", "USD"),
            FakeAccountValue("U1234567", "TotalCashValue", "20000",  "USD"),
            FakeAccountValue("U1234567", "BuyingPower",    "400000", "USD"),
            FakeAccountValue("U1234567", "AccountCode",    "U1234567", ""),
        ],
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    summaries = await adapter.get_account_summary()

    assert len(summaries) == 1
    s = summaries[0]
    assert s.broker == "IBKR"
    assert s.account_id == "U1234567"
    assert s.net_liquidation_usd == pytest.approx(100000.0)
    assert s.cash_usd == pytest.approx(20000.0)
    assert s.buying_power_usd == pytest.approx(400000.0)


async def test_usd_account_base_currency_is_usd(store):
    fake_ib = FakeIB(
        accounts=["U1234567"],
        summary_rows=[
            FakeAccountValue("U1234567", "NetLiquidation", "100000", "USD"),
            FakeAccountValue("U1234567", "TotalCashValue", "20000",  "USD"),
            FakeAccountValue("U1234567", "BuyingPower",    "400000", "USD"),
        ],
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    summary = (await adapter.get_account_summary())[0]

    assert summary.base_currency == "USD"


# Multiple accounts --------------------------------------------------------


async def test_two_linked_accounts_returns_two_summaries(store):
    """Taxable + IRA: both should surface."""
    fake_ib = FakeIB(
        accounts=["U1234567", "U7654321"],
        summary_rows=[
            FakeAccountValue("U1234567", "NetLiquidation", "100000", "USD"),
            FakeAccountValue("U1234567", "TotalCashValue", "20000",  "USD"),
            FakeAccountValue("U1234567", "BuyingPower",    "400000", "USD"),
            FakeAccountValue("U7654321", "NetLiquidation", "50000",  "USD"),
            FakeAccountValue("U7654321", "TotalCashValue", "5000",   "USD"),
            FakeAccountValue("U7654321", "BuyingPower",    "50000",  "USD"),
        ],
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    summaries = await adapter.get_account_summary()

    ids = sorted(s.account_id for s in summaries)
    assert ids == ["U1234567", "U7654321"]


async def test_per_account_values_are_distinct(store):
    fake_ib = FakeIB(
        accounts=["U1", "U2"],
        summary_rows=[
            FakeAccountValue("U1", "NetLiquidation", "100000", "USD"),
            FakeAccountValue("U1", "TotalCashValue", "20000",  "USD"),
            FakeAccountValue("U1", "BuyingPower",    "400000", "USD"),
            FakeAccountValue("U2", "NetLiquidation", "50000",  "USD"),
            FakeAccountValue("U2", "TotalCashValue", "5000",   "USD"),
            FakeAccountValue("U2", "BuyingPower",    "50000",  "USD"),
        ],
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    summaries = {s.account_id: s for s in await adapter.get_account_summary()}

    assert summaries["U1"].net_liquidation_usd == pytest.approx(100000.0)
    assert summaries["U2"].net_liquidation_usd == pytest.approx(50000.0)


# Non-USD base currency converts via FxService ----------------------------


async def test_hkd_base_account_converts_nlv_to_usd_via_fx(store):
    """A Hong Kong desk's NLV reported in HKD must come back in USD."""
    fake_ib = FakeIB(
        accounts=["U_HK"],
        summary_rows=[
            FakeAccountValue("U_HK", "NetLiquidation", "780000", "HKD"),
            FakeAccountValue("U_HK", "TotalCashValue", "78000",  "HKD"),
            FakeAccountValue("U_HK", "BuyingPower",    "3120000", "HKD"),
        ],
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    _seed_rate(fx_svc, "HKD", 1 / 7.80)
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    summary = (await adapter.get_account_summary())[0]

    assert summary.base_currency == "HKD"
    # 780,000 HKD ÷ 7.80 = 100,000 USD
    assert summary.net_liquidation_usd == pytest.approx(100000.0, rel=1e-4)
    assert summary.cash_usd == pytest.approx(10000.0, rel=1e-4)


# Account IDs come from managedAccounts even when zero positions ----------


async def test_empty_account_still_returned_with_zero_values(store):
    """An account with no holdings (perhaps just opened) still appears
    in get_account_summary so the filter chip can show it."""
    fake_ib = FakeIB(
        accounts=["U_EMPTY"],
        summary_rows=[
            FakeAccountValue("U_EMPTY", "NetLiquidation", "0", "USD"),
            FakeAccountValue("U_EMPTY", "TotalCashValue", "0", "USD"),
            FakeAccountValue("U_EMPTY", "BuyingPower",    "0", "USD"),
        ],
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    summaries = await adapter.get_account_summary()

    assert len(summaries) == 1
    assert summaries[0].account_id == "U_EMPTY"
    assert summaries[0].net_liquidation_usd == 0.0


# Disconnection: never crash ---------------------------------------------


async def test_disconnected_adapter_returns_empty(store):
    """If the gateway isn't connected, we return [] rather than crash —
    matches the existing get_positions behavior."""
    from app.adapters.ibkr import IbkrAdapter

    adapter = IbkrAdapter(
        host="ib-gateway", port=4003, client_id=1,
        ib_factory=lambda: None, store=store,
    )
    # Note: NOT calling .connect()

    result = await adapter.get_account_summary()

    assert result == []
