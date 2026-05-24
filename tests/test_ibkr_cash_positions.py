"""Slice 6 cycle 2: IbkrAdapter.get_positions emits CASH rows.

IB returns FX cash balances (`secType=="CASH"`) as Position rows in
`reqPositions`. We map them to a `Position` with asset_class="CASH" and
synthesize the display fields from the currency code — no
reqContractDetails, no market-data subscription needed.

CASH rules:
- native_key == canonical_symbol == native_symbol == currency code
- exchange = "" (CASH has no exchange)
- name_en = CURRENCY_NAMES[currency]  (e.g. "Hong Kong Dollar")
- quantity = balance, last_price = avg_cost = 1.0
- market_value_native = balance
- market_value_usd via FxService (slice 3)
- P&L = 0 always (computing true USD P&L needs FX cost basis IB doesn't track)
- We do NOT call reqContractDetails for CASH — saves a round-trip and
  CASH has no company name to resolve.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from app.core.fx import FxRate, FxService


# Test doubles — copied minimally from test_fx_in_adapter.py shape ----------


@dataclass
class FakeContract:
    conId: int
    symbol: str
    secType: str
    currency: str
    exchange: str = "IDEALPRO"  # IB returns IDEALPRO for FX cash
    primaryExchange: str = ""


@dataclass
class FakeContractDetails:
    contract: FakeContract
    longName: str


@dataclass
class FakeIBPosition:
    account: str
    contract: FakeContract
    position: float
    avgCost: float


@dataclass
class FakeAccountValue:
    account: str
    tag: str
    value: str
    currency: str = "USD"


class FakeTicker:
    def __init__(self, contract, last=None):
        self.contract = contract
        self.last = last
        self.bid = None
        self.ask = None

    def marketPrice(self):  # noqa: N802
        return self.last


class FakeIB:
    def __init__(self, positions, details, last_prices=None, accounts=None, summary_rows=None):
        self._positions = positions
        self._details = details
        self._last_prices = last_prices or {}
        self._accounts = list(accounts or [])
        self._summary_rows = list(summary_rows or [])
        self._connected = False
        self.contract_details_calls: list[int] = []
        self.market_data_type_calls: list[int] = []
        self.req_mkt_data_calls: list = []

    async def connectAsync(self, host, port, clientId):  # noqa: N802, N803
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isConnected(self):  # noqa: N802
        return self._connected

    def reqMarketDataType(self, t):  # noqa: N802
        self.market_data_type_calls.append(t)

    async def reqPositionsAsync(self):  # noqa: N802
        return self._positions

    def managedAccounts(self):  # noqa: N802
        return self._accounts

    async def accountSummaryAsync(self):  # noqa: N802
        return list(self._summary_rows)

    async def reqContractDetailsAsync(self, contract):  # noqa: N802
        self.contract_details_calls.append(contract.conId)
        d = self._details.get(contract.conId)
        return [d] if d is not None else []

    async def reqTickersAsync(self, *contracts):  # noqa: N802
        return [FakeTicker(c, last=self._last_prices.get(c.conId)) for c in contracts]

    def reqMktData(self, contract, *args, **kwargs):  # noqa: N802
        self.req_mkt_data_calls.append(contract)
        ticker = FakeTicker(contract)
        from tests.test_fx_ib_subscription import _Event
        ticker.updateEvent = _Event()
        return ticker


def _hkd_cash(qty=50000.0):
    contract = FakeContract(conId=900001, symbol="HKD", secType="CASH", currency="HKD")
    return FakeIBPosition(account="U1", contract=contract, position=qty, avgCost=1.0)


def _jpy_cash(qty=1_200_000.0):
    contract = FakeContract(conId=900002, symbol="JPY", secType="CASH", currency="JPY")
    return FakeIBPosition(account="U1", contract=contract, position=qty, avgCost=1.0)


def _aapl_stk():
    contract = FakeContract(conId=265598, symbol="AAPL", secType="STK", currency="USD")
    details = FakeContractDetails(
        contract=FakeContract(
            conId=265598, symbol="AAPL", secType="STK", currency="USD",
            primaryExchange="NASDAQ",
        ),
        longName="APPLE INC",
    )
    pos = FakeIBPosition(account="U1", contract=contract, position=10.0, avgCost=150.0)
    return pos, details


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
    """Inject a fake IB FX quote into the cache so .get_rate_sync returns it."""
    fx_svc._ib_rates[currency] = FxRate(
        pair=f"{currency}USD", rate=rate, is_stale=False, source="IB",
        quoted_at=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
    )


# Core behavior ------------------------------------------------------------


async def test_hkd_cash_balance_appears_as_position(store):
    """50,000 HKD sitting idle must show up as a Position row."""
    fake_ib = FakeIB(positions=[_hkd_cash(50000.0)], details={})
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    _seed_rate(fx_svc, "HKD", 1 / 7.80)

    adapter = await _make_adapter(fake_ib, store, fx_svc)
    positions = await adapter.get_positions()

    assert len(positions) == 1
    p = positions[0]
    assert p.asset_class == "CASH"
    assert p.currency == "HKD"
    assert p.quantity == pytest.approx(50000.0)


async def test_cash_position_has_currency_based_identity_fields(store):
    fake_ib = FakeIB(positions=[_hkd_cash()], details={})
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    _seed_rate(fx_svc, "HKD", 1 / 7.80)
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    p = (await adapter.get_positions())[0]

    assert p.native_key == "HKD"
    assert p.canonical_symbol == "HKD"
    assert p.native_symbol == "HKD"
    assert p.exchange == ""
    assert p.name_en == "Hong Kong Dollar"


async def test_cash_last_and_avg_cost_are_one(store):
    """The instrument IS the currency — last price and cost basis are
    both 1.0 by definition. Market value equals quantity (in native)."""
    fake_ib = FakeIB(positions=[_hkd_cash(50000.0)], details={})
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    _seed_rate(fx_svc, "HKD", 1 / 7.80)
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    p = (await adapter.get_positions())[0]

    assert p.avg_cost == pytest.approx(1.0)
    assert p.last_price == pytest.approx(1.0)
    assert p.market_value_native == pytest.approx(50000.0)


async def test_cash_market_value_usd_uses_fx_service(store):
    """50,000 HKD × (1 / 7.80) ≈ 6,410 USD."""
    fake_ib = FakeIB(positions=[_hkd_cash(50000.0)], details={})
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    _seed_rate(fx_svc, "HKD", 1 / 7.80)
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    p = (await adapter.get_positions())[0]

    assert p.market_value_usd == pytest.approx(50000.0 / 7.80, rel=1e-4)


async def test_cash_pnl_is_always_zero(store):
    """V1 explicitly does not compute USD P&L for CASH — needs FX cost
    basis IB doesn't track reliably. Both native and USD P&L stay at 0;
    the template renders — instead of $0.00."""
    fake_ib = FakeIB(positions=[_hkd_cash()], details={})
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    _seed_rate(fx_svc, "HKD", 1 / 7.80)
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    p = (await adapter.get_positions())[0]

    assert p.unrealized_pnl_native == 0.0
    assert p.unrealized_pnl_usd == 0.0


# Performance — no wasted IB round-trips for CASH ---------------------------


async def test_cash_skips_req_contract_details(store):
    """CASH has no company name to resolve. Hitting reqContractDetails
    for every cash balance on every reconnect would waste round-trips."""
    fake_ib = FakeIB(positions=[_hkd_cash()], details={})
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    _seed_rate(fx_svc, "HKD", 1 / 7.80)
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    await adapter.get_positions()

    assert fake_ib.contract_details_calls == [], (
        f"reqContractDetails should NOT be called for CASH; saw {fake_ib.contract_details_calls}"
    )


# Mixed portfolio ----------------------------------------------------------


async def test_stk_and_cash_both_returned(store):
    """A real portfolio holds both. The order doesn't matter, but both
    must appear."""
    apple_pos, apple_details = _aapl_stk()
    fake_ib = FakeIB(
        positions=[apple_pos, _hkd_cash(50000.0), _jpy_cash(1_000_000.0)],
        details={265598: apple_details},
        last_prices={265598: 180.0},
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    _seed_rate(fx_svc, "HKD", 1 / 7.80)
    _seed_rate(fx_svc, "JPY", 1 / 150.0)
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    positions = await adapter.get_positions()

    classes = sorted(p.asset_class for p in positions)
    assert classes == ["CASH", "CASH", "STK"]
    currencies = sorted(p.currency for p in positions if p.asset_class == "CASH")
    assert currencies == ["HKD", "JPY"]


async def test_cash_in_unsupported_currency_is_still_returned_with_unavailable_fx(store):
    """If IB returns a cash balance in a currency we have no FX rate for,
    we still surface the row — but USD column shows — (fx_unavailable=True).
    Better to show "50,000 CHF — USD unknown" than to drop the row."""
    contract = FakeContract(conId=900099, symbol="CHF", secType="CASH", currency="CHF")
    chf_cash = FakeIBPosition(account="U1", contract=contract, position=5000.0, avgCost=1.0)
    fake_ib = FakeIB(positions=[chf_cash], details={})
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    # Deliberately no CHF rate seeded
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    positions = await adapter.get_positions()

    assert len(positions) == 1
    p = positions[0]
    assert p.asset_class == "CASH"
    assert p.currency == "CHF"
    assert p.fx_unavailable is True


# USD cash is its own currency ---------------------------------------------


async def test_usd_cash_no_fx_conversion_needed(store):
    """USD cash passes through — no rate lookup, never marked fx_unavailable."""
    contract = FakeContract(conId=900098, symbol="USD", secType="CASH", currency="USD")
    usd_cash = FakeIBPosition(account="U1", contract=contract, position=10000.0, avgCost=1.0)
    fake_ib = FakeIB(positions=[usd_cash], details={})
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    p = (await adapter.get_positions())[0]

    assert p.currency == "USD"
    assert p.market_value_usd == pytest.approx(10000.0)
    assert p.fx_unavailable is False


async def test_account_summary_cash_is_synthesized_when_reqpositions_has_no_cash(store):
    """Some IBKR accounts expose idle cash only through TotalCashValue.
    Holdings still need a CASH row so the all-accounts view includes it."""
    apple_pos, apple_details = _aapl_stk()
    fake_ib = FakeIB(
        positions=[apple_pos],
        details={265598: apple_details},
        last_prices={265598: 180.0},
        accounts=["U1"],
        summary_rows=[
            FakeAccountValue("U1", "NetLiquidation", "11800", "USD"),
            FakeAccountValue("U1", "TotalCashValue", "10000", "USD"),
            FakeAccountValue("U1", "BuyingPower", "50000", "USD"),
        ],
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    positions = await adapter.get_positions()

    cash_rows = [p for p in positions if p.asset_class == "CASH"]
    assert len(cash_rows) == 1
    cash = cash_rows[0]
    assert cash.account_id == "U1"
    assert cash.currency == "USD"
    assert cash.quantity == pytest.approx(10000.0)
    assert cash.market_value_usd == pytest.approx(10000.0)


async def test_account_summary_cash_is_synthesized_for_cash_only_account(store):
    fake_ib = FakeIB(
        positions=[],
        details={},
        accounts=["U_CASH"],
        summary_rows=[
            FakeAccountValue("U_CASH", "NetLiquidation", "25000", "USD"),
            FakeAccountValue("U_CASH", "TotalCashValue", "25000", "USD"),
        ],
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    positions = await adapter.get_positions()

    assert len(positions) == 1
    assert positions[0].asset_class == "CASH"
    assert positions[0].account_id == "U_CASH"
    assert positions[0].market_value_usd == pytest.approx(25000.0)


async def test_account_summary_cash_does_not_duplicate_reqpositions_cash(store):
    fake_ib = FakeIB(
        positions=[_hkd_cash(50000.0)],
        details={},
        accounts=["U1"],
        summary_rows=[
            FakeAccountValue("U1", "NetLiquidation", "50000", "HKD"),
            FakeAccountValue("U1", "TotalCashValue", "50000", "HKD"),
        ],
    )
    fx_svc = FxService(store=store, api_fetcher=None)
    await fx_svc.start()
    _seed_rate(fx_svc, "HKD", 1 / 7.80)
    adapter = await _make_adapter(fake_ib, store, fx_svc)

    positions = await adapter.get_positions()

    assert [p.asset_class for p in positions].count("CASH") == 1
