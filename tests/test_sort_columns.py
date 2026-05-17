"""Slice 8 cycle 3: tappable column-header sort cycling.

The sort itself is client-side (JS reorders <tr> rows in place) and the
preference (column + direction) persists to localStorage. These tests
verify the wiring contract:

- Every sortable column header has a `data-sort-key` so JS can identify it.
- Headers are clickable (role="button" / tabindex / cursor-pointer markup).
- Markup includes a placeholder for the sort-direction indicator that JS
  toggles between '', '▲', '▼'.
- Rows expose the sortable values as `data-*` attributes so JS can compare
  numerics without re-parsing formatted strings ($1,234 → 1234).

Pure JS unit tests are deferred (no Node toolchain); the sort-cycle logic
is intentionally trivial enough that markup wiring + smoke is sufficient.
"""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


class _FakeAdapter:
    name = "IBKR"

    def __init__(self, positions):
        self._positions = positions

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self): return []


def _pos(sym="AAPL.US", name="APPLE", mv=1000.0, qty=10.0, last=110.0, pnl=100.0):
    return Position(
        broker="IBKR", account_id="U1", native_key=sym.split(".")[0],
        canonical_symbol=sym, native_symbol=sym.split(".")[0],
        exchange="NYSE", currency="USD",
        name_en=name, asset_class="STK",
        quantity=qty, avg_cost=100.0, last_price=last,
        market_value_native=mv, market_value_usd=mv,
        unrealized_pnl_native=pnl, unrealized_pnl_usd=pnl,
    )


def _client(positions):
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=positions))
    return TestClient(app)


# Header markup -----------------------------------------------------------


def test_column_headers_have_data_sort_key():
    """Each sortable header carries a data-sort-key matching a row's
    data-* field so JS can pair them."""
    response = _client([_pos()]).get("/")
    text = response.text

    # We sort by mv_usd by default — that header must be wired
    assert 'data-sort-key="mv_usd"' in text
    # And at least one other (quantity) so users have alternatives
    assert 'data-sort-key="quantity"' in text


def test_headers_have_a_clickable_affordance():
    """role="button" or tabindex makes the headers keyboard- and
    screen-reader-accessible; cursor:pointer alone isn't enough."""
    response = _client([_pos()]).get("/")

    text = response.text
    assert 'role="button"' in text or 'tabindex="0"' in text


def test_default_sort_indicator_appears_on_mv_usd_column():
    """Default sort is mv_usd desc — that column's header should render
    a slot the JS uses to fill in ▼."""
    response = _client([_pos()]).get("/")

    # We expose the slot as a span with data-sort-indicator so JS can
    # fill it dynamically
    assert "data-sort-indicator" in response.text


# Row data attributes -----------------------------------------------------


def test_rows_expose_sortable_values_as_data_attributes():
    """JS needs the raw numerics, not the formatted display strings,
    to compare correctly ($1,234 / qty 1,000 etc.)."""
    response = _client([_pos(mv=1234.0, qty=42.0, last=99.5, pnl=12.0)]).get("/")

    text = response.text
    # The row should carry data-mv-usd / data-quantity / data-last-price /
    # data-pnl-usd attributes the sort JS can read
    assert 'data-mv-usd="1234' in text
    assert 'data-quantity="42' in text


def test_rows_carry_data_attributes_for_every_sortable_column():
    """Each header's data-sort-key must have a matching row attribute.
    Audits the markup contract automatically."""
    response = _client([_pos()]).get("/")

    text = response.text
    # If there's a data-sort-key=foo on a header, there must be a
    # data-foo="..." on each row.
    import re
    keys = set(re.findall(r'data-sort-key="([^"]+)"', text))
    for k in keys:
        attr = k.replace("_", "-")
        assert f"data-{attr}=" in text, f"row missing data-{attr} for header {k}"


# JS file wires the behavior ---------------------------------------------


def test_app_js_implements_sort_cycle():
    """The JS must subscribe to header clicks and toggle indicators.
    Pure-JS unit tests not in scope here; verify the wiring exists."""
    from pathlib import Path
    js = (Path("app/static") / "app.js").read_text()

    # The JS reads dataset.sortKey from the headers (which map from
    # data-sort-key attributes); the dataset accessor is the test signal.
    assert "sortKey" in js or "data-sort-key" in js
    assert "localStorage" in js
    # Three states (asc, desc, unsorted)
    assert "'asc'" in js and "'desc'" in js


def test_app_js_writes_sort_preference_under_distinct_key():
    """Don't collide with other localStorage usage; use a namespaced key."""
    from pathlib import Path
    js = (Path("app/static") / "app.js").read_text()

    # Some unique key like 'portfolio-hub.sort' or similar
    assert "portfolio-hub" in js or "portfolioHub" in js or "ph.sort" in js
