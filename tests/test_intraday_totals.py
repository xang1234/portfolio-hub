"""_compute_totals: intraday aggregation + has_intraday gating, plus the
hero "Today" row in index.html.

The `has_intraday` flag exists so a freshly booted dashboard (no prev-close
cache yet, all positions still pre-backfill) doesn't render a misleading
"Today $0 (+0.00%)" that looks like a real flat-day reading. The line
only paints once at least one position has a populated previous_close.
"""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position
from app.main import _compute_totals


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


def _stk(**kw) -> Position:
    base = dict(
        broker="IBKR", account_id="U1", native_key="1",
        canonical_symbol="AAPL.US", native_symbol="AAPL",
        exchange="NASDAQ", currency="USD",
        name_en="Apple", asset_class="STK",
        quantity=100.0, avg_cost=150.0, last_price=210.0,
        market_value_native=21000.0, market_value_usd=21000.0,
        unrealized_pnl_native=6000.0, unrealized_pnl_usd=6000.0,
        previous_close=200.0,
    )
    base.update(kw)
    return Position(**base)


def _cash(**kw) -> Position:
    base = dict(
        broker="IBKR", account_id="U1", native_key="USD",
        canonical_symbol="USD", native_symbol="USD",
        exchange="", currency="USD",
        name_en="US Dollar", asset_class="CASH",
        quantity=10000.0, avg_cost=1.0, last_price=1.0,
        market_value_native=10000.0, market_value_usd=10000.0,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
    )
    base.update(kw)
    return Position(**base)


# _compute_totals ---------------------------------------------------------


def test_totals_includes_intraday_fields():
    positions = [_stk()]
    t = _compute_totals(positions)
    for k in ("intraday_pnl_usd", "intraday_pnl_pct",
              "intraday_pnl_is_positive", "has_intraday"):
        assert k in t, f"missing totals key: {k}"


def test_has_intraday_false_when_no_position_has_prev_close():
    # Pure cash + a stock with previous_close=0 (e.g., fresh boot before backfill)
    positions = [_cash(), _stk(previous_close=0.0)]
    t = _compute_totals(positions)
    assert t["has_intraday"] is False


def test_has_intraday_true_when_any_position_has_prev_close():
    positions = [_cash(), _stk(previous_close=200.0)]
    t = _compute_totals(positions)
    assert t["has_intraday"] is True


def test_intraday_pnl_sums_contributing_positions_only():
    positions = [
        _stk(quantity=100.0, last_price=210.0, previous_close=200.0,
             market_value_native=21000.0, market_value_usd=21000.0),  # +$1000
        _stk(native_key="2", canonical_symbol="MSFT.US",
             quantity=50.0, last_price=395.0, previous_close=400.0,
             market_value_native=19750.0, market_value_usd=19750.0),  # -$250
        _cash(),  # contributes 0
    ]
    t = _compute_totals(positions)
    # 100*(210-200) + 50*(395-400) = +1000 - 250 = +750
    assert abs(t["intraday_pnl_usd"] - 750.0) < 1e-6
    assert t["intraday_pnl_is_positive"] is True


def test_intraday_pnl_pct_is_zero_when_basis_unknown():
    # Cash-only portfolio: total mv_usd is positive but no intraday data.
    # Pct should be 0.0, not a div-by-zero crash.
    t = _compute_totals([_cash()])
    assert t["intraday_pnl_pct"] == 0.0


def test_intraday_pnl_negative_direction():
    positions = [_stk(last_price=190.0, previous_close=200.0,
                      market_value_native=19000.0, market_value_usd=19000.0)]
    t = _compute_totals(positions)
    # 100*(190-200) = -1000
    assert abs(t["intraday_pnl_usd"] - (-1000.0)) < 1e-6
    assert t["intraday_pnl_is_positive"] is False


# Hero "Today" rendering --------------------------------------------------


def _client(positions):
    from app.main import create_app
    return TestClient(create_app(broker=_Fake(positions)))


def test_index_renders_hero_today_when_intraday_present():
    response = _client([_stk(previous_close=200.0, last_price=210.0,
                             market_value_native=21000.0, market_value_usd=21000.0)]).get("/")
    text = response.text
    assert "hero-today" in text
    # The "Today" label appears next to the day-change value
    assert ">Today<" in text


def test_index_omits_hero_today_when_no_prev_close():
    response = _client([_stk(previous_close=0.0)]).get("/")
    assert "hero-today" not in response.text


def test_index_omits_hero_today_for_cash_only_portfolio():
    response = _client([_cash()]).get("/")
    assert "hero-today" not in response.text


# Per-row chg-pct rendering -----------------------------------------------


def test_row_renders_chg_pct_when_intraday_present():
    response = _client([_stk(last_price=210.0, previous_close=200.0)]).get("/")
    text = response.text
    assert "chg-pct" in text
    # +5.00 % — formatted to 2 decimals in the template
    assert "+5.00%" in text


def test_row_omits_chg_pct_when_prev_close_missing():
    response = _client([_stk(previous_close=0.0)]).get("/")
    assert "chg-pct" not in response.text


def test_row_omits_chg_pct_for_cash():
    response = _client([_cash()]).get("/")
    assert "chg-pct" not in response.text
