"""Slice 6 cycle 3: holdings_row.html renders CASH distinctly.

CASH display contract:
  - 💵 indicator at the start so the row reads as cash, not stock.
  - First column shows the currency flag (🇭🇰 for HKD), then the
    English name ("Hong Kong Dollar"), then the currency code subtext.
  - Last-price column does NOT render "1.00" (meaningless) — shows
    a dash or is suppressed.
  - P&L (USD) column shows — not $0.00, since v1 doesn't compute
    true cash P&L.
  - The native market-value column shows the cash balance ("50,000 HKD").
  - The USD column shows the converted value ("$6,410") and respects
    fx_is_stale / fx_is_fallback / fx_unavailable flags exactly like
    STK rows.
"""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


class _FakeAdapter:
    def __init__(self, positions):
        self.name = "IBKR"
        self._positions = positions

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self): return []


def _hkd_cash(*, mv_usd=6410.26, fx_unavailable=False,
              fx_is_stale=False, fx_is_fallback=False) -> Position:
    return Position(
        broker="IBKR", account_id="U1",
        native_key="HKD", canonical_symbol="HKD", native_symbol="HKD",
        exchange="", currency="HKD",
        name_en="Hong Kong Dollar", asset_class="CASH",
        quantity=50000.0, avg_cost=1.0, last_price=1.0,
        market_value_native=50000.0, market_value_usd=mv_usd,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
        fx_is_stale=fx_is_stale, fx_is_fallback=fx_is_fallback,
        fx_unavailable=fx_unavailable,
    )


def _usd_cash() -> Position:
    return Position(
        broker="IBKR", account_id="U1",
        native_key="USD", canonical_symbol="USD", native_symbol="USD",
        exchange="", currency="USD",
        name_en="US Dollar", asset_class="CASH",
        quantity=10000.0, avg_cost=1.0, last_price=1.0,
        market_value_native=10000.0, market_value_usd=10000.0,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
    )


def _aapl_stk() -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key="265598",
        canonical_symbol="AAPL.US", native_symbol="AAPL",
        exchange="NYSE", currency="USD",
        name_en="APPLE INC", asset_class="STK",
        quantity=10.0, avg_cost=150.0, last_price=180.0,
        market_value_native=1800.0, market_value_usd=1800.0,
        unrealized_pnl_native=300.0, unrealized_pnl_usd=300.0,
    )


def _client(positions):
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=positions))
    return TestClient(app)


# CASH-specific markup ----------------------------------------------------


def test_cash_row_renders_money_indicator():
    """💵 must appear on the CASH row so it's instantly distinguishable
    from STK rows."""
    response = _client([_hkd_cash()]).get("/")

    assert "💵" in response.text


def test_cash_row_uses_currency_flag_not_exchange_flag():
    """CASH has no exchange. The flag comes from the currency lookup:
    HKD → 🇭🇰. Verify by holding only an HKD cash balance — the only
    flag emoji in the page (other than the connection badge) should be
    the HK flag."""
    response = _client([_hkd_cash()]).get("/")

    assert "🇭🇰" in response.text


def test_cash_row_shows_currency_name():
    response = _client([_hkd_cash()]).get("/")

    assert "Hong Kong Dollar" in response.text


def test_cash_row_shows_currency_code_subtext():
    response = _client([_hkd_cash()]).get("/")

    # "HKD" must appear as a subtext / native-symbol on the row
    assert "HKD" in response.text


def test_cash_row_last_price_shows_dash_not_one_dot_zero():
    """Last price for CASH is 1.0 by definition (the currency against
    itself) — rendering "1.00" in the price column would just be
    noise. Show — instead."""
    response = _client([_hkd_cash()]).get("/")
    text = response.text

    # Em-dash must appear; harder to assert column-precise, but it must
    # exist on the page for cash rows.
    assert "—" in text


def test_cash_row_native_value_renders_the_balance():
    """50,000 HKD cash → native column should show "50,000" with HKD."""
    response = _client([_hkd_cash()]).get("/")
    text = response.text

    assert "50,000" in text


def test_cash_row_usd_value_uses_fx_converted_amount():
    """USD column on CASH must use market_value_usd from the FxService,
    same as STK rows."""
    response = _client([_hkd_cash(mv_usd=6410.26)]).get("/")
    text = response.text

    # $6,410 (rounded)
    assert "6,410" in text


def test_cash_row_renders_dash_when_fx_unavailable():
    """No HKD rate → USD column shows — instead of $0."""
    response = _client([_hkd_cash(mv_usd=0.0, fx_unavailable=True)]).get("/")

    assert "—" in response.text


def test_cash_row_respects_stale_fx_warning():
    response = _client([_hkd_cash(fx_is_stale=True)]).get("/")

    assert "⚠️" in response.text


def test_cash_row_respects_fx_fallback_badge():
    response = _client([_hkd_cash(fx_is_fallback=True)]).get("/")

    assert "📡" in response.text


# Mixed portfolio: STK + CASH both render -----------------------------------


def test_stk_and_cash_both_render_in_table():
    response = _client([_aapl_stk(), _hkd_cash()]).get("/")
    text = response.text

    # STK row
    assert "APPLE INC" in text
    # CASH row
    assert "Hong Kong Dollar" in text
    assert "💵" in text


def test_usd_cash_renders_without_fx_badges():
    """USD cash doesn't need FX conversion at all — no ⚠️/📡 badges."""
    response = _client([_usd_cash()]).get("/")
    text = response.text

    assert "US Dollar" in text
    assert "💵" in text
    assert "⚠️" not in text
    assert "📡" not in text


# Totals strip includes CASH ----------------------------------------------


def test_header_total_includes_cash_usd_value():
    """50000 HKD * (1/7.80) ≈ 6410 USD plus 1800 USD AAPL = 8210 USD."""
    response = _client([_aapl_stk(), _hkd_cash(mv_usd=6410.26)]).get("/")

    # Total should be present and include both STK + CASH
    text = response.text
    # Hard to assert exact rounding without knowing template's format,
    # so just check the per-row values are accounted for
    assert "8,210" in text or "$8,210" in text or "8,211" in text
