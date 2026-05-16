"""Slice 7 cycle 2: GET /?account=U1234567 filter.

Selecting an account narrows the positions table and totals to just
that account's rows. ?account=All (or omitted) shows everything.
Unknown account values fall back to "All" rather than rendering an
empty page (defensive — handles bookmarked stale account IDs).
"""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


class _FakeAdapter:
    def __init__(self, positions, accounts=None):
        self.name = "IBKR"
        self._positions = positions
        self._accounts = accounts or []

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self): return list(self._accounts)


def _pos(account_id, symbol, name, mv_usd, pnl_usd=0.0) -> Position:
    return Position(
        broker="IBKR", account_id=account_id, native_key=symbol.split(".")[0],
        canonical_symbol=symbol, native_symbol=symbol.split(".")[0],
        exchange="NYSE", currency="USD",
        name_en=name, asset_class="STK",
        quantity=10.0, avg_cost=100.0, last_price=110.0,
        market_value_native=mv_usd, market_value_usd=mv_usd,
        unrealized_pnl_native=pnl_usd, unrealized_pnl_usd=pnl_usd,
    )


def _client(positions, accounts=None):
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=positions, accounts=accounts or []))
    return TestClient(app)


# No filter: All --------------------------------------------------------


def test_no_filter_shows_all_positions():
    """Without a query string, every position must render."""
    ps = [
        _pos("U1", "AAPL.US", "APPLE", 1000.0),
        _pos("U2", "MSFT.US", "MICROSOFT", 2000.0),
    ]
    response = _client(ps).get("/")

    assert "APPLE" in response.text
    assert "MICROSOFT" in response.text


def test_account_all_explicitly_shows_everything():
    ps = [
        _pos("U1", "AAPL.US", "APPLE", 1000.0),
        _pos("U2", "MSFT.US", "MICROSOFT", 2000.0),
    ]
    response = _client(ps).get("/?account=All")

    assert "APPLE" in response.text
    assert "MICROSOFT" in response.text


# Specific filter -------------------------------------------------------


def test_account_filter_hides_other_account_rows():
    """?account=U1 must hide MSFT (held in U2)."""
    ps = [
        _pos("U1", "AAPL.US", "APPLE", 1000.0),
        _pos("U2", "MSFT.US", "MICROSOFT", 2000.0),
    ]
    response = _client(ps).get("/?account=U1")

    assert "APPLE" in response.text
    assert "MICROSOFT" not in response.text


def test_account_filter_keeps_only_target_account_rows():
    """?account=U2 — only MSFT visible."""
    ps = [
        _pos("U1", "AAPL.US", "APPLE", 1000.0),
        _pos("U2", "MSFT.US", "MICROSOFT", 2000.0),
    ]
    response = _client(ps).get("/?account=U2")

    assert "MICROSOFT" in response.text
    assert "APPLE" not in response.text


# Same symbol, two accounts: BOTH render under All; only one under filter --


def test_duplicate_symbol_across_accounts_shows_two_rows_under_all():
    """AAPL in U1 (avg 100) AND U2 (avg 120) → two rows under no filter.
    Tax-lot honesty rule: never merge same-symbol-across-accounts."""
    ps = [
        _pos("U1", "AAPL.US", "APPLE", 1000.0),
        Position(  # same symbol, different account, different avg cost
            broker="IBKR", account_id="U2", native_key="AAPL",
            canonical_symbol="AAPL.US", native_symbol="AAPL",
            exchange="NYSE", currency="USD",
            name_en="APPLE", asset_class="STK",
            quantity=5.0, avg_cost=120.0, last_price=110.0,
            market_value_native=550.0, market_value_usd=550.0,
            unrealized_pnl_native=-50.0, unrealized_pnl_usd=-50.0,
        ),
    ]
    response = _client(ps).get("/")

    # Two rows for AAPL (one per account)
    assert response.text.count("AAPL.US") >= 2


# Totals recompute with filter ------------------------------------------


def test_header_totals_reflect_filtered_set():
    """Totals strip must show only the active account's totals."""
    ps = [
        _pos("U1", "AAPL.US", "APPLE", 1000.0),
        _pos("U2", "MSFT.US", "MICROSOFT", 2000.0),
    ]
    # Without filter: total = 3000
    no_filter = _client(ps).get("/").text
    # With ?account=U1: total = 1000
    filtered = _client(ps).get("/?account=U1").text

    assert "3,000" in no_filter
    assert "1,000" in filtered
    # Make sure 3,000 is no longer in the filtered output
    assert "3,000" not in filtered


# Unknown account: degrade to All ---------------------------------------


def test_unknown_account_falls_back_to_all():
    """A stale bookmark for an account that's been closed shouldn't blank
    the page — show everything and let the chips re-orient the user."""
    ps = [_pos("U1", "AAPL.US", "APPLE", 1000.0)]

    response = _client(ps).get("/?account=U_DELETED")

    assert response.status_code == 200
    # Either we render U1 (graceful fallback) — preferred
    assert "APPLE" in response.text
