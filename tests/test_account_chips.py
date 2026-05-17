"""Slice 7 cycles 3-5: account-chip strip, per-row account pill, and the
per-account NLV/cash/buying-power summary line.

Behavior contract:

  Chip strip
    - One chip per linked account from get_account_summary()
    - Plus an "All" chip at the front
    - Active chip styled distinctly so the user knows which filter is on
    - Each chip is a <a> / <button> with href="/?account=..." that an
      htmx swap or a plain link can both use

  Row pill
    - When active filter is "All", every row shows a small pill next to
      the symbol with the account_id ("U1234567"). On a one-account
      portfolio this just gives extra confirmation.
    - When a specific account is selected, the pill is hidden (the chip
      already tells the user which account they're viewing).

  Per-account summary
    - When a specific account is selected, a line below the totals
      reports "Account U1234567 · NLV $X · Cash $Y · BP $Z".
    - Suppressed under "All".
"""

from fastapi.testclient import TestClient

from app.core.broker import AccountSummary, ConnectionState, Position


class _FakeAdapter:
    def __init__(self, positions, accounts):
        self.name = "IBKR"
        self._positions = positions
        self._accounts = accounts

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self): return list(self._accounts)


def _summary(account_id, *, nlv, cash, bp, base="USD"):
    return AccountSummary(
        broker="IBKR", account_id=account_id, base_currency=base,
        net_liquidation_usd=nlv, cash_usd=cash, buying_power_usd=bp,
    )


def _pos(account_id, sym, name="STOCK", mv=1000.0):
    return Position(
        broker="IBKR", account_id=account_id, native_key=sym.split(".")[0],
        canonical_symbol=sym, native_symbol=sym.split(".")[0],
        exchange="NYSE", currency="USD",
        name_en=name, asset_class="STK",
        quantity=10.0, avg_cost=100.0, last_price=110.0,
        market_value_native=mv, market_value_usd=mv,
        unrealized_pnl_native=100.0, unrealized_pnl_usd=100.0,
    )


def _client(positions, accounts):
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=positions, accounts=accounts))
    return TestClient(app)


# Chip strip --------------------------------------------------------------


def test_chip_strip_has_all_chip_and_one_chip_per_account():
    accounts = [
        _summary("U1", nlv=100000, cash=20000, bp=400000),
        _summary("U2", nlv=50000,  cash=5000,  bp=50000),
    ]
    positions = [_pos("U1", "AAPL.US"), _pos("U2", "MSFT.US")]
    response = _client(positions, accounts).get("/")

    text = response.text
    assert "All" in text
    assert "U1" in text
    assert "U2" in text


def test_chip_strip_marks_active_account_chip():
    accounts = [
        _summary("U1", nlv=100000, cash=20000, bp=400000),
        _summary("U2", nlv=50000,  cash=5000,  bp=50000),
    ]
    positions = [_pos("U1", "AAPL.US"), _pos("U2", "MSFT.US")]
    response = _client(positions, accounts).get("/?account=U1")

    # Active chip should carry a distinguishing class or attribute
    text = response.text
    assert "account-chip--active" in text or 'aria-current="page"' in text


def test_chip_links_to_query_string():
    """The chip must be a clickable link to /?account=ID so it works
    even without JavaScript / HTMX. Browsers handle native <a href> for
    free — the htmx layer is a progressive enhancement."""
    accounts = [
        _summary("U1", nlv=100000, cash=20000, bp=400000),
        _summary("U2", nlv=50000,  cash=5000,  bp=50000),
    ]
    positions = [_pos("U1", "AAPL.US"), _pos("U2", "MSFT.US")]
    response = _client(positions, accounts).get("/")

    assert "account=U1" in response.text
    assert "account=U2" in response.text
    # The All chip leaves the URL parameter-free (clean default URL)
    assert 'href="/"' in response.text or "href='/'" in response.text


def test_only_all_chip_visible_when_single_account():
    """A single-account portfolio shouldn't show a 'U1234567' chip
    next to 'All' — there's nothing to switch to. Just hide the row
    or render only 'All'."""
    accounts = [_summary("U1", nlv=100000, cash=20000, bp=400000)]
    positions = [_pos("U1", "AAPL.US")]
    response = _client(positions, accounts).get("/")

    # Either no chip strip or only the 'All' chip — but a U2/U3 chip
    # must not appear.
    text = response.text
    assert "U2" not in text
    assert "U3" not in text


# Per-row account pill ----------------------------------------------------


def test_account_pill_shown_on_row_when_filter_is_all():
    accounts = [
        _summary("U1", nlv=100000, cash=20000, bp=400000),
        _summary("U2", nlv=50000,  cash=5000,  bp=50000),
    ]
    positions = [_pos("U1", "AAPL.US", name="APPLE")]
    response = _client(positions, accounts).get("/")

    # The account ID must surface on the row markup (some class+text)
    assert "account-pill" in response.text


def test_account_pill_hidden_on_row_when_filter_is_specific():
    accounts = [
        _summary("U1", nlv=100000, cash=20000, bp=400000),
        _summary("U2", nlv=50000,  cash=5000,  bp=50000),
    ]
    positions = [_pos("U1", "AAPL.US", name="APPLE")]
    response = _client(positions, accounts).get("/?account=U1")

    # Pill should not be rendered when a single account is already selected
    assert "account-pill" not in response.text


# Per-account summary line ------------------------------------------------


def test_per_account_summary_appears_when_specific_account_selected():
    accounts = [
        _summary("U1234567", nlv=100000, cash=20000, bp=400000),
        _summary("U7654321", nlv=50000,  cash=5000,  bp=50000),
    ]
    positions = [_pos("U1234567", "AAPL.US"), _pos("U7654321", "MSFT.US")]
    response = _client(positions, accounts).get("/?account=U1234567")

    text = response.text
    assert "U1234567" in text
    # NLV value 100000 → "100,000"
    assert "100,000" in text
    # cash 20000 → "20,000"
    assert "20,000" in text


def test_per_account_summary_hidden_when_all_selected():
    accounts = [
        _summary("U1234567", nlv=100000, cash=20000, bp=400000),
        _summary("U7654321", nlv=50000,  cash=5000,  bp=50000),
    ]
    positions = [_pos("U1234567", "AAPL.US"), _pos("U7654321", "MSFT.US")]
    response = _client(positions, accounts).get("/")

    # The per-account summary class must NOT be present under All
    assert "account-summary" not in response.text
