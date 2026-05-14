"""Tests for IbkrAdapter.get_positions() and get_account_summary().

Slice 2 surface:
  - get_positions() returns list[Position] for STK secType only (CASH in slice 6)
  - Filters out OPT, FUT, BOND, FUND, CRYPTO with no error
  - Resolves primaryExchange via reqContractDetails (never trusts Contract.exchange)
  - Builds canonical_symbol via symbols.canonical_symbol()
  - native_key = str(conId)
  - account_id captured from the IB Position row
  - name_en populated via NameResolver (which uses the test store)
  - market_value_native = quantity * last_price
  - USD fields default to 0.0 (slice 3 wires FX)

We use FakeIB + FakeIBPosition + FakeContract + FakeContractDetails test doubles
that match the shape ib_async exposes; injected through the same ib_factory hook
the adapter already exposes from slice 1.
"""

from dataclasses import dataclass

import pytest


# Test doubles -----------------------------------------------------------------


@dataclass
class FakeContract:
    conId: int
    symbol: str
    secType: str
    currency: str
    exchange: str = "SMART"   # what reqPositions usually returns — adapter should not trust this
    primaryExchange: str = "" # adapter must derive from reqContractDetails


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


class FakeIB:
    def __init__(
        self,
        positions: list[FakeIBPosition],
        contract_details: dict[int, FakeContractDetails],
        last_prices: dict[int, float] | None = None,
    ) -> None:
        self._positions = positions
        self._contract_details = contract_details
        self._last_prices = last_prices or {}
        self._connected = False
        self.contract_details_calls: list[int] = []
        self.market_data_type_calls: list[int] = []

    async def connectAsync(self, host: str, port: int, clientId: int) -> None:  # noqa: N802
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def isConnected(self) -> bool:  # noqa: N802
        return self._connected

    def reqMarketDataType(self, market_data_type: int) -> None:  # noqa: N802
        self.market_data_type_calls.append(market_data_type)

    async def reqPositionsAsync(self):  # noqa: N802 — matches ib_async
        return self._positions

    async def reqContractDetailsAsync(self, contract: FakeContract):  # noqa: N802
        self.contract_details_calls.append(contract.conId)
        details = self._contract_details.get(contract.conId)
        return [details] if details is not None else []

    async def reqTickersAsync(self, *contracts):  # noqa: N802 — last-price snapshot
        class Tick:
            def __init__(self, last: float | None) -> None:
                self.last = last
                self.marketPrice = lambda: last  # ib_async style helper

        return [Tick(self._last_prices.get(c.conId)) for c in contracts]


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def make_adapter(fake_ib: FakeIB, store):
    from app.adapters.ibkr import IbkrAdapter

    return IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: fake_ib,
        store=store,
    )


# Tests ------------------------------------------------------------------------


async def test_get_positions_returns_empty_when_account_holds_nothing(store):
    fake_ib = FakeIB(positions=[], contract_details={})
    adapter = make_adapter(fake_ib, store)
    await adapter.connect()

    positions = await adapter.get_positions()

    assert positions == []


async def test_get_positions_returns_one_position_per_stk_holding(store):
    contract = FakeContract(conId=76792991, symbol="700", secType="STK", currency="HKD")
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U7575980", contract=contract, position=100.0, avgCost=400.0)],
        contract_details={
            76792991: FakeContractDetails(
                contract=FakeContract(
                    conId=76792991,
                    symbol="700",
                    secType="STK",
                    currency="HKD",
                    exchange="SEHK",
                    primaryExchange="SEHK",
                ),
                longName="TENCENT HOLDINGS LTD",
            ),
        },
        last_prices={76792991: 420.0},
    )
    adapter = make_adapter(fake_ib, store)
    await adapter.connect()

    positions = await adapter.get_positions()

    assert len(positions) == 1
    p = positions[0]
    assert p.broker == "IBKR"
    assert p.account_id == "U7575980"
    assert p.native_key == "76792991"
    assert p.canonical_symbol == "700.HK"
    assert p.native_symbol == "700"
    assert p.exchange == "SEHK"
    assert p.currency == "HKD"
    assert p.name_en == "TENCENT HOLDINGS LTD"
    assert p.asset_class == "STK"
    assert p.quantity == 100.0
    assert p.avg_cost == 400.0
    assert p.last_price == 420.0
    assert p.market_value_native == pytest.approx(42000.0)
    # USD columns deferred to slice 3
    assert p.market_value_usd == 0.0
    assert p.unrealized_pnl_usd == 0.0


async def test_get_positions_filters_out_non_stk_secTypes(store):
    """OPT, FUT, BOND, FUND, CRYPTO are all out of v1 scope.

    CASH is also filtered for slice 2 — it returns in slice 6.
    """
    contracts = {
        1: FakeContract(conId=1, symbol="AAPL", secType="STK", currency="USD"),
        2: FakeContract(conId=2, symbol="AAPL", secType="OPT", currency="USD"),
        3: FakeContract(conId=3, symbol="ES",   secType="FUT", currency="USD"),
        4: FakeContract(conId=4, symbol="HKD",  secType="CASH", currency="HKD"),
        5: FakeContract(conId=5, symbol="T",    secType="BOND", currency="USD"),
    }
    details = {
        1: FakeContractDetails(
            contract=FakeContract(conId=1, symbol="AAPL", secType="STK", currency="USD", primaryExchange="NASDAQ"),
            longName="APPLE INC",
        ),
    }
    fake_ib = FakeIB(
        positions=[
            FakeIBPosition(account="U1", contract=contracts[1], position=10.0, avgCost=150.0),
            FakeIBPosition(account="U1", contract=contracts[2], position=1.0, avgCost=5.0),
            FakeIBPosition(account="U1", contract=contracts[3], position=2.0, avgCost=4500.0),
            FakeIBPosition(account="U1", contract=contracts[4], position=50000.0, avgCost=1.0),
            FakeIBPosition(account="U1", contract=contracts[5], position=10.0, avgCost=100.0),
        ],
        contract_details=details,
        last_prices={1: 180.0},
    )
    adapter = make_adapter(fake_ib, store)
    await adapter.connect()

    positions = await adapter.get_positions()

    assert [p.canonical_symbol for p in positions] == ["AAPL.US"]
    assert positions[0].asset_class == "STK"


async def test_get_positions_uses_primary_exchange_from_reqContractDetails_not_smart(store):
    """Contract.exchange from reqPositions often returns 'SMART' (routing destination).

    The adapter must always derive the canonical exchange from primaryExchange
    via reqContractDetails so 700 ends up as 700.HK, not 700.US-or-similar.
    """
    contract = FakeContract(conId=76792991, symbol="700", secType="STK", currency="HKD", exchange="SMART")
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U7575980", contract=contract, position=100.0, avgCost=400.0)],
        contract_details={
            76792991: FakeContractDetails(
                contract=FakeContract(
                    conId=76792991,
                    symbol="700",
                    secType="STK",
                    currency="HKD",
                    exchange="SMART",
                    primaryExchange="SEHK",
                ),
                longName="TENCENT HOLDINGS LTD",
            ),
        },
        last_prices={76792991: 420.0},
    )
    adapter = make_adapter(fake_ib, store)
    await adapter.connect()

    p = (await adapter.get_positions())[0]

    assert p.canonical_symbol == "700.HK"
    assert p.exchange == "SEHK"


async def test_get_positions_caches_name_resolution_across_calls(store):
    contract = FakeContract(conId=76792991, symbol="700", secType="STK", currency="HKD")
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U7575980", contract=contract, position=100.0, avgCost=400.0)],
        contract_details={
            76792991: FakeContractDetails(
                contract=FakeContract(
                    conId=76792991, symbol="700", secType="STK", currency="HKD", primaryExchange="SEHK",
                ),
                longName="TENCENT HOLDINGS LTD",
            ),
        },
        last_prices={76792991: 420.0},
    )
    adapter = make_adapter(fake_ib, store)
    await adapter.connect()

    await adapter.get_positions()
    await adapter.get_positions()

    # Two calls to get_positions() but only one reqContractDetails per unique conId
    assert fake_ib.contract_details_calls == [76792991]


async def test_get_positions_taiwan_stock_uses_tw_flag_not_cn(store):
    """Hard requirement (saved memory): TWSE → 🇹🇼, never 🇨🇳."""
    contract = FakeContract(conId=2330, symbol="2330", secType="STK", currency="TWD")
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U1", contract=contract, position=100.0, avgCost=500.0)],
        contract_details={
            2330: FakeContractDetails(
                contract=FakeContract(conId=2330, symbol="2330", secType="STK", currency="TWD", primaryExchange="TWSE"),
                longName="TAIWAN SEMICONDUCTOR MANUFACTURING CO",
            ),
        },
        last_prices={2330: 600.0},
    )
    adapter = make_adapter(fake_ib, store)
    await adapter.connect()

    p = (await adapter.get_positions())[0]

    assert p.canonical_symbol == "2330.TW"
    assert p.exchange == "TWSE"


async def test_get_positions_skips_contract_without_primary_exchange(store):
    """An instrument whose contractDetails has neither primaryExchange nor
    a recognizable exchange field should be dropped (with a log line), not
    returned with a wrong canonical_symbol."""
    contract = FakeContract(conId=999, symbol="MYSTERY", secType="STK", currency="USD")
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U1", contract=contract, position=1.0, avgCost=1.0)],
        contract_details={
            999: FakeContractDetails(
                contract=FakeContract(conId=999, symbol="MYSTERY", secType="STK", currency="USD", primaryExchange=""),
                longName="MYSTERY CORP",
            ),
        },
        last_prices={999: 1.0},
    )
    adapter = make_adapter(fake_ib, store)
    await adapter.connect()

    positions = await adapter.get_positions()

    assert positions == []


async def test_connect_requests_delayed_frozen_market_data_type(store):
    """Without delayed-frozen data, accounts that lack live subscriptions get
    empty tickers. reqMarketDataType(4) tells IB to use delayed-frozen data,
    which returns the last cached delayed price even when markets are closed
    AND no live subscription exists.
    """
    fake_ib = FakeIB(positions=[], contract_details={})
    adapter = make_adapter(fake_ib, store)

    await adapter.connect()

    assert 4 in fake_ib.market_data_type_calls, (
        "connect() should call reqMarketDataType(4) for delayed-frozen data"
    )


async def test_get_account_summary_returns_one_summary_per_account(store):
    """Multi-account support per Q2 of grilling — slice 7 will use this,
    but a list shape is part of the Protocol contract from slice 2 forward."""
    contract = FakeContract(conId=1, symbol="AAPL", secType="STK", currency="USD")
    fake_ib = FakeIB(
        positions=[FakeIBPosition(account="U1", contract=contract, position=10.0, avgCost=150.0)],
        contract_details={
            1: FakeContractDetails(
                contract=FakeContract(conId=1, symbol="AAPL", secType="STK", currency="USD", primaryExchange="NASDAQ"),
                longName="APPLE INC",
            ),
        },
        last_prices={1: 180.0},
    )
    adapter = make_adapter(fake_ib, store)
    await adapter.connect()

    summaries = await adapter.get_account_summary()

    assert isinstance(summaries, list)
    # At minimum, the accounts seen in positions should be present
    assert any(s.account_id == "U1" for s in summaries)
