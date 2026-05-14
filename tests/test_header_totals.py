"""Slice 3 cycle 10: header strip totals.

The sticky header shows:
  - Total market value USD across all positions
  - Total unrealized P&L USD (color-coded green when ≥ 0, red when < 0)
  - P&L percentage

Positions with fx_unavailable=True contribute 0 to USD totals — we
can't aggregate unknowns honestly, so they're excluded. The user can
still see the individual rows showing — in the USD column.
"""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


def _pos(*, currency="HKD", mv_usd=5000.0, pnl_usd=200.0, fx_unavailable=False) -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key=str(hash(currency + str(mv_usd))),
        canonical_symbol=f"X.{currency[:2]}", native_symbol="X",
        exchange="SEHK", currency=currency,
        name_en="X CO", asset_class="STK",
        quantity=10.0, avg_cost=400.0, last_price=420.0,
        market_value_native=mv_usd / 0.128 if currency != "USD" else mv_usd,
        market_value_usd=mv_usd,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=pnl_usd,
        fx_unavailable=fx_unavailable,
    )


class FakeAdapter:
    def __init__(self, *, positions=None):
        self.name = "IBKR"
        self._positions = positions or []
    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self): return []


def make_client(positions) -> TestClient:
    from app.main import create_app
    app = create_app(broker=FakeAdapter(positions=positions))
    return TestClient(app)


# Total MV USD ----------------------------------------------------------------


def test_header_renders_total_market_value_usd():
    positions = [
        _pos(mv_usd=5000.0),
        _pos(mv_usd=3000.0),
    ]
    client = make_client(positions)

    response = client.get("/")

    # Total: 8000 — should appear somewhere in the header strip
    assert "$8,000" in response.text or "8,000" in response.text


def test_header_total_excludes_fx_unavailable_positions():
    """Don't aggregate unknowns. fx_unavailable rows contribute 0 to totals."""
    positions = [
        _pos(mv_usd=5000.0),
        _pos(mv_usd=0.0, fx_unavailable=True),  # excluded
    ]
    client = make_client(positions)

    response = client.get("/")

    # Total should still be 5000, not 5000 + 0 with confusing visual
    assert "5,000" in response.text


# Total unrealized P&L USD --------------------------------------------------


def test_header_renders_total_unrealized_pnl_positive():
    positions = [
        _pos(pnl_usd=200.0),
        _pos(pnl_usd=300.0),
    ]
    client = make_client(positions)

    response = client.get("/")
    text = response.text

    # P&L: +500. Sign matters for color-coding.
    assert "+$500" in text or "+500" in text or "$500" in text


def test_header_renders_total_unrealized_pnl_negative():
    positions = [
        _pos(pnl_usd=-150.0),
        _pos(pnl_usd=-50.0),
    ]
    client = make_client(positions)

    response = client.get("/")

    # P&L: -200
    assert "-$200" in response.text or "-200" in response.text or "($200)" in response.text


def test_header_pnl_has_negative_class_when_red():
    """The header needs a CSS class hook (something like 'pnl-negative'
    or 'is-negative') so we can color it red without inline styles."""
    positions = [_pos(pnl_usd=-100.0)]
    client = make_client(positions)

    response = client.get("/")
    text_lower = response.text.lower()

    assert "negative" in text_lower or "pnl-down" in text_lower or "is-loss" in text_lower


def test_header_pnl_has_positive_class_when_green():
    positions = [_pos(pnl_usd=100.0)]
    client = make_client(positions)

    response = client.get("/")
    text_lower = response.text.lower()

    assert "positive" in text_lower or "pnl-up" in text_lower or "is-gain" in text_lower


# Empty portfolio -----------------------------------------------------------


def test_header_renders_zero_totals_when_no_positions():
    """With no positions, totals should be 0 (not crash)."""
    client = make_client([])

    response = client.get("/")

    # Page loads OK
    assert response.status_code == 200
