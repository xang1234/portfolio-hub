"""The Weight column shows each row's share of total USD exposure.

The contract: per-row weight is computed as `position.market_value_usd /
totals.mv_usd × 100`, where `totals` is derived from the current filtered
set (same set the table renders). Sum across rendered weight cells must
therefore equal ~100 %, modulo rounding.

This invariant prevents a class of regressions where a future change to
`_compute_totals` or the row partial accidentally divides by a different
denominator (e.g. unfiltered totals while the table shows a filtered
subset) — which would silently produce a column that no longer accounts
for the visible portfolio.
"""

import re

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


class _Fake:
    name = "IBKR"

    def __init__(self, positions):
        self._p = positions

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._p)
    async def get_account_summary(self): return []


def _stk(*, mv_usd: float, **overrides) -> Position:
    base = dict(
        broker="IBKR", account_id="U1", native_key="0",
        canonical_symbol="X.US", native_symbol="X",
        exchange="NASDAQ", currency="USD",
        name_en="Stock", asset_class="STK",
        quantity=100.0, avg_cost=50.0, last_price=60.0,
        market_value_native=mv_usd, market_value_usd=mv_usd,
        unrealized_pnl_native=mv_usd * 0.1, unrealized_pnl_usd=mv_usd * 0.1,
    )
    base.update(overrides)
    return Position(**base)


def _cash(*, mv_usd: float) -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key="USD",
        canonical_symbol="USD", native_symbol="USD",
        exchange="", currency="USD",
        name_en="USD cash", asset_class="CASH",
        quantity=mv_usd, avg_cost=1.0, last_price=1.0,
        market_value_native=mv_usd, market_value_usd=mv_usd,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
    )


def _client(positions):
    from app.main import create_app
    return TestClient(create_app(broker=_Fake(positions)))


def _extract_weight_pcts(html: str) -> list[float]:
    """Return the floats inside every `<span class="col-alloc__pct">X.X%</span>`."""
    matches = re.findall(r'class="col-alloc__pct">([\d.]+)%</span>', html)
    return [float(m) for m in matches]


def test_weight_cells_sum_to_approximately_100_percent():
    positions = [
        _stk(native_key="1", canonical_symbol="700.HK", mv_usd=40_200),
        _stk(native_key="2", canonical_symbol="AAPL.US", mv_usd=47_146),
        _stk(native_key="3", canonical_symbol="2330.TW", mv_usd=15_600),
        _cash(mv_usd=10_000),
    ]
    text = _client(positions).get("/").text
    pcts = _extract_weight_pcts(text)

    assert len(pcts) == len(positions), (
        "every position must render a weight cell"
    )
    # Generous rounding tolerance: each .1f label can lose up to 0.05pp, so
    # across N rows the sum can drift by ~N × 0.05. 1.0pp covers it for any
    # plausible portfolio size.
    assert abs(sum(pcts) - 100.0) < 1.0


def test_weight_renders_dash_when_total_is_zero():
    """All-fx-unavailable portfolio → totals.mv_usd == 0 → row guard kicks
    in and the weight cell renders — rather than dividing by zero."""
    p = Position(
        broker="IBKR", account_id="U1", native_key="1",
        canonical_symbol="X.EU", native_symbol="X",
        exchange="IBIS", currency="EUR",
        name_en="EU Stock", asset_class="STK",
        quantity=100.0, avg_cost=10.0, last_price=10.0,
        market_value_native=1000.0, market_value_usd=0.0,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
        fx_unavailable=True,
    )
    text = _client([p]).get("/").text
    assert _extract_weight_pcts(text) == []
    # The cell must actually contain the dash glyph + .pnl-na class — not
    # just an empty <td>. Without this assertion the test passes even if a
    # future change removes the {% else %} branch from the row partial.
    assert 'class="pnl-na">—</span>' in text
    assert "col-alloc" in text  # cell + header still render


def test_weight_label_uses_one_decimal_precision():
    """Long-tail holdings (<0.5 %) must show a real value rather than
    rounding to 0 %. .1f precision keeps a 0.3 %-weight position
    distinguishable from a true zero."""
    positions = [
        _stk(native_key="1", canonical_symbol="BIG.US", mv_usd=99_700),
        _stk(native_key="2", canonical_symbol="TINY.US", mv_usd=300),
    ]
    text = _client(positions).get("/").text
    pcts = _extract_weight_pcts(text)
    # 300 / 100_000 × 100 = 0.3 %
    assert 0.3 in pcts
    # And the big one rounds to 99.7
    assert 99.7 in pcts
