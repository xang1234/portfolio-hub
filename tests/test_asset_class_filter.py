"""Slice 8 cycle 1: ?asset=STK|CASH|All filter + chips.

Extends slice 7's ?account= filter. The two compose freely
(?account=U1&asset=CASH only shows U1's cash balances) and both
persist via URL query string so refresh / deep-link work.

The chip strip mirrors the account chips: native <a href> links with
htmx upgrades; active chip styled distinctly; All chip always present.
"""

from fastapi.testclient import TestClient

from app.core.broker import AccountSummary, ConnectionState, Position


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


def _stk(sym="AAPL.US", name="APPLE", account="U1"):
    return Position(
        broker="IBKR", account_id=account, native_key=sym.split(".")[0],
        canonical_symbol=sym, native_symbol=sym.split(".")[0],
        exchange="NYSE", currency="USD",
        name_en=name, asset_class="STK",
        quantity=10.0, avg_cost=100.0, last_price=110.0,
        market_value_native=1000.0, market_value_usd=1000.0,
        unrealized_pnl_native=100.0, unrealized_pnl_usd=100.0,
    )


def _cash(currency="HKD", name="Hong Kong Dollar", account="U1", mv_usd=6410.0):
    return Position(
        broker="IBKR", account_id=account,
        native_key=currency, canonical_symbol=currency, native_symbol=currency,
        exchange="", currency=currency,
        name_en=name, asset_class="CASH",
        quantity=50000.0, avg_cost=1.0, last_price=1.0,
        market_value_native=50000.0, market_value_usd=mv_usd,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
    )


def _client(positions, accounts=None):
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=positions, accounts=accounts or []))
    return TestClient(app)


# Filter behavior --------------------------------------------------------


def test_no_asset_filter_shows_both_stk_and_cash():
    positions = [_stk(name="APPLE"), _cash(name="Hong Kong Dollar")]
    response = _client(positions).get("/")

    assert "APPLE" in response.text
    assert "Hong Kong Dollar" in response.text


def test_asset_all_explicitly_shows_both():
    positions = [_stk(name="APPLE"), _cash(name="Hong Kong Dollar")]
    response = _client(positions).get("/?asset=All")

    assert "APPLE" in response.text
    assert "Hong Kong Dollar" in response.text


def test_asset_stk_hides_cash_rows():
    positions = [_stk(name="APPLE"), _cash(name="Hong Kong Dollar")]
    response = _client(positions).get("/?asset=STK")

    assert "APPLE" in response.text
    assert "Hong Kong Dollar" not in response.text


def test_asset_cash_hides_stk_rows():
    positions = [_stk(name="APPLE"), _cash(name="Hong Kong Dollar")]
    response = _client(positions).get("/?asset=CASH")

    assert "Hong Kong Dollar" in response.text
    assert "APPLE" not in response.text


def test_unknown_asset_filter_falls_back_to_all():
    """Stale bookmark or typo → show everything rather than blank page."""
    positions = [_stk(name="APPLE"), _cash(name="Hong Kong Dollar")]
    response = _client(positions).get("/?asset=BOND")

    assert response.status_code == 200
    assert "APPLE" in response.text
    assert "Hong Kong Dollar" in response.text


# Chip rendering ----------------------------------------------------------


def test_asset_chips_render_all_stk_cash():
    """The chip strip must include three options: All, STK, CASH.
    The All chip leaves the URL parameter-free; the others encode their
    value so deep-links round-trip."""
    positions = [_stk(), _cash()]
    response = _client(positions).get("/")
    text = response.text

    assert "asset-chip" in text
    # Three labels visible in chip nav
    assert ">All<" in text
    assert ">STK<" in text
    assert ">CASH<" in text
    assert "asset=STK" in text
    assert "asset=CASH" in text


def test_active_asset_chip_marked():
    positions = [_stk(), _cash()]
    response = _client(positions).get("/?asset=STK")

    assert "asset-chip--active" in response.text or 'aria-current="page"' in response.text


# Composition with account filter ---------------------------------------


def test_asset_and_account_filters_compose():
    """?account=U1&asset=CASH should only show U1's cash."""
    accounts = [
        AccountSummary(broker="IBKR", account_id="U1", base_currency="USD",
                       net_liquidation_usd=10000, cash_usd=6410, buying_power_usd=10000),
        AccountSummary(broker="IBKR", account_id="U2", base_currency="USD",
                       net_liquidation_usd=10000, cash_usd=0, buying_power_usd=10000),
    ]
    positions = [
        _stk(sym="AAPL.US", name="APPLE_U1", account="U1"),
        _stk(sym="MSFT.US", name="MICROSOFT_U2", account="U2"),
        _cash(account="U1", name="HKD_U1"),
    ]
    response = _client(positions, accounts).get("/?account=U1&asset=CASH")

    text = response.text
    assert "HKD_U1" in text
    assert "APPLE_U1" not in text
    assert "MICROSOFT_U2" not in text


def test_asset_chips_carry_existing_account_param():
    """Switching the asset chip while filtered to U1 must keep ?account=U1
    in the URL so the filter isn't dropped on click."""
    accounts = [
        AccountSummary(broker="IBKR", account_id="U1", base_currency="USD",
                       net_liquidation_usd=10000, cash_usd=6410, buying_power_usd=10000),
        AccountSummary(broker="IBKR", account_id="U2", base_currency="USD",
                       net_liquidation_usd=10000, cash_usd=0, buying_power_usd=10000),
    ]
    positions = [_stk(account="U1"), _stk(account="U2"), _cash(account="U1")]
    response = _client(positions, accounts).get("/?account=U1")

    # Asset chips should carry &account=U1 (or ?account=U1)
    text = response.text
    assert "account=U1" in text and "asset=STK" in text


def test_account_chips_carry_existing_asset_param():
    """Symmetric: switching account chip preserves the asset filter."""
    accounts = [
        AccountSummary(broker="IBKR", account_id="U1", base_currency="USD",
                       net_liquidation_usd=10000, cash_usd=6410, buying_power_usd=10000),
        AccountSummary(broker="IBKR", account_id="U2", base_currency="USD",
                       net_liquidation_usd=10000, cash_usd=0, buying_power_usd=10000),
    ]
    positions = [_stk(account="U1"), _stk(account="U2"), _cash(account="U1")]
    response = _client(positions, accounts).get("/?asset=CASH")

    text = response.text
    assert "asset=CASH" in text and "account=U1" in text


# Totals recompute on asset filter --------------------------------------


def test_totals_recompute_under_asset_filter():
    """Totals respect the active asset filter, just like the account filter."""
    positions = [_stk(name="APPLE"), _cash(name="HKD", mv_usd=6410)]
    # Unfiltered: 1000 + 6410 = 7410
    # ?asset=STK: 1000
    # ?asset=CASH: 6410
    unfiltered = _client(positions).get("/").text
    stk_only = _client(positions).get("/?asset=STK").text
    cash_only = _client(positions).get("/?asset=CASH").text

    assert "7,410" in unfiltered
    assert "1,000" in stk_only
    assert "7,410" not in stk_only
    assert "6,410" in cash_only
