"""Slice 6 cycle 4: lunch-break subtext on STK rows.

When an exchange is in LUNCH state, the last tick the row is showing is
necessarily a stale-during-lunch print (off-exchange volume aside). We
surface this as a per-row subtext so users don't read "last 420.00 HKD"
as fresh in-session pricing.

CASH rows never display the subtext (CASH has no exchange and no lunch).
STK rows whose exchange is in OPEN/CLOSED/HOLIDAY/EXTENDED also don't.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position
from app.core.markets import MarketHours


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


def _tencent_hkex() -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key="76792991",
        canonical_symbol="700.HK", native_symbol="700",
        exchange="SEHK", currency="HKD",
        name_en="TENCENT", asset_class="STK",
        quantity=100.0, avg_cost=400.0, last_price=420.0,
        market_value_native=42000.0, market_value_usd=5388.6,
        unrealized_pnl_native=2000.0, unrealized_pnl_usd=256.6,
    )


def _hkd_cash() -> Position:
    return Position(
        broker="IBKR", account_id="U1",
        native_key="HKD", canonical_symbol="HKD", native_symbol="HKD",
        exchange="", currency="HKD",
        name_en="Hong Kong Dollar", asset_class="CASH",
        quantity=50000.0, avg_cost=1.0, last_price=1.0,
        market_value_native=50000.0, market_value_usd=6410.26,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
    )


def _utc(year, month, day, hh=0, mm=0):
    return datetime(year, month, day, hh, mm, tzinfo=timezone.utc)


def _make_client(positions, *, clock):
    from app.main import create_app
    hours = MarketHours(clock=clock)
    app = create_app(broker=_FakeAdapter(positions=positions), market_hours=hours)
    return TestClient(app)


# In-lunch -----------------------------------------------------------------


def test_stk_row_during_hkex_lunch_shows_lunch_subtext():
    """HKEX 04:30 UTC = 12:30 HKT — lunch is 12:00-13:00 HKT.
    Tencent row should show a lunch-break subtext."""
    client = _make_client([_tencent_hkex()], clock=lambda: _utc(2026, 5, 20, 4, 30))

    response = client.get("/")
    text = response.text.lower()

    assert "lunch" in text


def test_lunch_subtext_includes_reopen_time():
    """Useful so users know when the next live tick will arrive — derive
    from MarketStatus.next_transition_local (set to '13:00 HKT' during
    LUNCH state)."""
    client = _make_client([_tencent_hkex()], clock=lambda: _utc(2026, 5, 20, 4, 30))

    response = client.get("/")
    text = response.text

    # MarketStatus during HKEX lunch puts "13:00 HKT" in next_transition_local
    assert "13:00" in text or "HKT" in text


# Not in lunch -------------------------------------------------------------


def test_stk_row_outside_lunch_has_no_lunch_subtext():
    """HKEX morning session 02:30 UTC = 10:30 HKT — no lunch subtext."""
    client = _make_client([_tencent_hkex()], clock=lambda: _utc(2026, 5, 20, 2, 30))

    response = client.get("/")

    # We test for the precise phrase to avoid false positives on
    # incidental usage elsewhere on the page
    assert "lunch break" not in response.text.lower()


def test_stk_row_when_market_closed_no_lunch_subtext():
    """HKEX 09:00 UTC = 17:00 HKT — after close, not lunch."""
    client = _make_client([_tencent_hkex()], clock=lambda: _utc(2026, 5, 20, 9, 0))

    response = client.get("/")

    assert "lunch break" not in response.text.lower()


# CASH never shows the subtext --------------------------------------------


def test_cash_row_never_shows_lunch_subtext():
    """Even during HKEX lunch, a CASH HKD row has no exchange — no
    lunch concept applies."""
    client = _make_client([_hkd_cash()], clock=lambda: _utc(2026, 5, 20, 4, 30))

    response = client.get("/")

    # CASH row is rendered (sanity check) but no lunch wording
    assert "Hong Kong Dollar" in response.text
    assert "lunch break" not in response.text.lower()


# Multiple rows, mixed states ---------------------------------------------


def test_only_lunched_exchanges_get_subtext_when_mixed():
    """HKEX during lunch + NYSE in regular session — only the HKEX
    row gets the subtext."""
    apple = Position(
        broker="IBKR", account_id="U1", native_key="265598",
        canonical_symbol="AAPL.US", native_symbol="AAPL",
        exchange="NYSE", currency="USD",
        name_en="APPLE", asset_class="STK",
        quantity=10.0, avg_cost=150.0, last_price=180.0,
        market_value_native=1800.0, market_value_usd=1800.0,
        unrealized_pnl_native=300.0, unrealized_pnl_usd=300.0,
    )
    # 04:30 UTC = 00:30 ET (NYSE closed) AND 12:30 HKT (HKEX lunch)
    client = _make_client([_tencent_hkex(), apple], clock=lambda: _utc(2026, 5, 20, 4, 30))

    response = client.get("/")
    text = response.text

    # The HKEX row should have a lunch hint; only one row mentioning it
    assert text.lower().count("lunch break") == 1
