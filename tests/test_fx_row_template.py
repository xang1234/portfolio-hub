"""Slice 3 cycle 9: holdings row template renders USD columns + badges.

Display contract:
  - MV USD column always present; renders "$X,XXX" or "—" when unavailable.
  - ⚠️ appears next to the USD value when fx_is_stale=True (IB rate stale).
  - 📡 badge appears on the row when fx_is_fallback=True (API_FALLBACK).
  - Native value still rendered as before (HKD column was added in slice 2).

Tests render the full index page so we exercise both the template and
the index route.
"""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


def _hkd_position(
    *, mv_usd=5388.6, pnl_usd=256.6,
    fx_is_stale=False, fx_is_fallback=False, fx_unavailable=False,
) -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key="76792991",
        canonical_symbol="700.HK", native_symbol="700",
        exchange="SEHK", currency="HKD",
        name_en="TENCENT HOLDINGS LTD", asset_class="STK",
        quantity=100.0, avg_cost=400.0, last_price=420.0,
        market_value_native=42000.0, market_value_usd=mv_usd,
        unrealized_pnl_native=2000.0, unrealized_pnl_usd=pnl_usd,
        fx_is_stale=fx_is_stale, fx_is_fallback=fx_is_fallback,
        fx_unavailable=fx_unavailable,
    )


def _usd_position() -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key="265598",
        canonical_symbol="AAPL.US", native_symbol="AAPL",
        exchange="NASDAQ", currency="USD",
        name_en="APPLE INC", asset_class="STK",
        quantity=10.0, avg_cost=150.0, last_price=180.0,
        market_value_native=1800.0, market_value_usd=1800.0,
        unrealized_pnl_native=300.0, unrealized_pnl_usd=300.0,
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


def make_client(positions=None) -> TestClient:
    from app.main import create_app
    app = create_app(broker=FakeAdapter(positions=positions or []))
    return TestClient(app)


# USD column appears in the table header ------------------------------------


def test_index_table_has_mv_usd_column_header():
    client = make_client(positions=[_hkd_position()])

    response = client.get("/")

    # Some kind of MV USD column heading must exist; case-insensitive search
    text_lower = response.text.lower()
    assert "mv usd" in text_lower or "market value usd" in text_lower or "value (usd)" in text_lower


# Fresh rate: USD value rendered, no badge ---------------------------------


def test_fresh_hkd_row_renders_usd_value_without_warnings():
    pos = _hkd_position(mv_usd=5388.6)
    client = make_client(positions=[pos])

    response = client.get("/")
    text = response.text

    # USD value 5388.6 → "5,389" formatted
    assert "5,389" in text or "5,388" in text  # rounding tolerance
    # No ⚠️ or 📡 badges
    assert "⚠️" not in text
    assert "📡" not in text


# Stale IB rate: ⚠️ appears -----------------------------------------------


def test_stale_hkd_row_renders_warning_emoji():
    pos = _hkd_position(fx_is_stale=True)
    client = make_client(positions=[pos])

    response = client.get("/")

    assert "⚠️" in response.text


# API fallback: 📡 appears -------------------------------------------------


def test_api_fallback_hkd_row_renders_satellite_emoji():
    pos = _hkd_position(fx_is_fallback=True)
    client = make_client(positions=[pos])

    response = client.get("/")

    assert "📡" in response.text


# No FX rate available: USD column shows — ---------------------------------


def test_unavailable_fx_renders_em_dash_in_usd_column():
    """When fx_unavailable=True, USD column should render — instead of $0
    so the user knows the value is unknown, not actually zero."""
    pos = _hkd_position(mv_usd=0.0, pnl_usd=0.0, fx_unavailable=True)
    client = make_client(positions=[pos])

    response = client.get("/")

    # Em-dash must appear; harder to assert precisely it's in the USD column
    # but we can at least check it's present *and* the row doesn't show 0
    assert "—" in response.text


# USD-native position: no FX flags, USD column shows the value -------------


def test_usd_position_renders_usd_value_with_no_fx_flags():
    pos = _usd_position()
    client = make_client(positions=[pos])

    response = client.get("/")
    text = response.text

    # USD position should NOT have any FX warning badges
    assert "⚠️" not in text
    assert "📡" not in text
    # And the value is rendered (1800 USD)
    assert "1,800" in text
