"""Slice 11 cycle 4: EOD reconcile via reqExecutions.

The EOD job is the backstop for execDetailsEvent — if the dashboard was
disconnected when a fill happened, the live stream missed it but IB has
the execution recorded server-side. reqExecutions() returns the last
~24-48 hours of executions; we INSERT OR IGNORE each one, so already-
captured fills are silently dropped and missed ones are picked up.

Contract:
- `reconcile_fills(adapter, store)` returns the count of NEW rows written.
- Idempotent: running it twice in a row inserts 0 the second time.
- Re-uses build_fill_row + Store.insert_fill (no logic duplication).
- Doesn't crash if reqExecutions raises (logs and returns 0 — the
  next run will retry).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest


@dataclass
class FakeContract:
    conId: int
    symbol: str
    secType: str = "STK"
    currency: str = "USD"
    exchange: str = "SMART"
    primaryExchange: str = ""


@dataclass
class FakeExecution:
    execId: str
    acctNumber: str
    side: str
    shares: float
    price: float
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc))


@dataclass
class FakeCommissionReport:
    execId: str
    commission: float = 1.0
    currency: str = "USD"


@dataclass
class FakeFill:
    contract: FakeContract
    execution: FakeExecution
    commissionReport: FakeCommissionReport
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc))


class FakeAdapter:
    """Just enough surface for reconcile_fills."""
    name = "IBKR"

    def __init__(self, executions, *, raise_on_call=False):
        self._executions = executions
        self._store = None  # set by the test
        self._fx_service = None
        self._raise = raise_on_call
        self.req_calls = 0

    async def _req_executions(self):
        """Mimic the wrapper around IB.reqExecutionsAsync that the reconcile
        function calls. Adapter owns this so tests don't need to plumb an IB."""
        self.req_calls += 1
        if self._raise:
            raise RuntimeError("simulated IB error")
        return list(self._executions)


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store
    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _aapl_fill(exec_id, price=180.0):
    contract = FakeContract(
        conId=265598, symbol="AAPL", secType="STK", currency="USD",
        primaryExchange="NASDAQ",
    )
    execution = FakeExecution(
        execId=exec_id, acctNumber="U1", side="BOT",
        shares=10.0, price=price,
    )
    return FakeFill(
        contract=contract, execution=execution,
        commissionReport=FakeCommissionReport(execId=exec_id, commission=1.0),
    )


# Inserts only NEW rows -------------------------------------------------------


async def test_reconcile_inserts_unseen_fills(store):
    from app.jobs.fills_reconcile import reconcile_fills

    adapter = FakeAdapter([_aapl_fill("e-1"), _aapl_fill("e-2")])
    adapter._store = store
    inserted = await reconcile_fills(adapter, store)
    assert inserted == 2


async def test_reconcile_skips_already_present_fills(store):
    from app.jobs.fills_reconcile import reconcile_fills

    # Pre-seed one of the executions into the table.
    await store.insert_fill(
        broker="IBKR", account_id="U1", execution_id="e-1",
        canonical_symbol="AAPL.US", native_key="265598",
        asset_class="STK", side="BUY", quantity=10.0, price=180.0,
        currency="USD", fx_rate_at_fill=None,
        fees_native=1.0, fees_usd=1.0,
        filled_at=datetime(2026, 5, 17, 14, 0, tzinfo=timezone.utc),
    )

    adapter = FakeAdapter([_aapl_fill("e-1"), _aapl_fill("e-2")])
    adapter._store = store
    inserted = await reconcile_fills(adapter, store)
    assert inserted == 1, (
        "should only insert the previously-missed e-2; e-1 is already there"
    )


# Idempotency ----------------------------------------------------------------


async def test_running_reconcile_twice_in_a_row_is_idempotent(store):
    from app.jobs.fills_reconcile import reconcile_fills

    fills = [_aapl_fill("e-1"), _aapl_fill("e-2")]
    adapter = FakeAdapter(fills)
    adapter._store = store

    first = await reconcile_fills(adapter, store)
    second = await reconcile_fills(adapter, store)
    assert first == 2
    assert second == 0


# Doesn't crash on broker error ----------------------------------------------


async def test_reconcile_returns_zero_when_ib_raises(store):
    from app.jobs.fills_reconcile import reconcile_fills

    adapter = FakeAdapter([], raise_on_call=True)
    adapter._store = store
    # Should NOT raise — operator runs this on a schedule; one bad call
    # shouldn't blow up the next attempt.
    result = await reconcile_fills(adapter, store)
    assert result == 0
