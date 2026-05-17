"""Slice 8 cycle 4: long-press / right-click row detail modal.

The modal itself is rendered on the page (hidden by default) and Alpine.js
drives the show/hide + which row's data populates it. These tests verify
the wiring contract — full UX behavior (touch hold timing, escape key
dismissal) is verified manually since there's no headless browser here.

Contract:
- Rows expose every field the modal needs as data-* attributes so the
  modal handler doesn't have to look up the Position again.
- The modal markup exists in the page (with the .row-detail-modal class)
  even when no row is selected, so Alpine can toggle visibility without
  needing to inject HTML.
- The fields the modal shows must include: avg_cost (native + USD),
  current price, broker, account_id, exchange, canonical_symbol,
  native_key, currency.
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


def _pos():
    return Position(
        broker="IBKR", account_id="U7575980", native_key="76792991",
        canonical_symbol="700.HK", native_symbol="700",
        exchange="SEHK", currency="HKD",
        name_en="TENCENT", asset_class="STK",
        quantity=100.0, avg_cost=380.0, last_price=420.0,
        market_value_native=42000.0, market_value_usd=5388.6,
        unrealized_pnl_native=4000.0, unrealized_pnl_usd=512.5,
    )


def _client(positions):
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=positions))
    return TestClient(app)


# Modal markup -----------------------------------------------------------


def test_row_detail_modal_present_in_page():
    """The modal element exists even without a row selected so Alpine
    can toggle it instead of injecting markup."""
    response = _client([_pos()]).get("/")

    assert "row-detail-modal" in response.text


def test_modal_uses_alpine_for_visibility_toggle():
    """The modal must be Alpine-controlled (x-show / x-data) so it
    actually hides by default."""
    response = _client([_pos()]).get("/")
    text = response.text

    # We use x-data on an ancestor and x-show on the modal
    assert "x-data" in text
    assert "x-show" in text or "x-cloak" in text


# Row data attributes the modal reads -----------------------------------


def test_row_exposes_avg_cost_attribute():
    response = _client([_pos()]).get("/")

    # avg_cost is needed by the modal but isn't in the visible columns
    assert "data-avg-cost=" in response.text


def test_row_exposes_full_identifying_fields():
    """The modal shows broker, account_id, exchange, currency, native_key,
    canonical_symbol — these need either to live on the row dataset or
    be derivable from row attributes already in place."""
    response = _client([_pos()]).get("/")
    text = response.text

    # broker/account/exchange/currency/native_key/canonical
    # Some are already in id, but data-* is cleaner for JS lookup
    assert "data-broker=" in text
    assert "data-account=" in text
    assert "data-exchange=" in text
    assert "data-currency=" in text
    assert "data-native-key=" in text
    assert "data-canonical-symbol=" in text


# Trigger affordances -----------------------------------------------------


def test_row_wired_with_long_press_handler():
    """Touch-hold (mobile) → open detail. The row must reference an
    Alpine handler the modal listens to (custom event or shared state)."""
    response = _client([_pos()]).get("/")

    text = response.text
    # We use a custom JS handler attribute (data-row-detail) or alpine
    # @contextmenu / @touchstart bindings
    assert ("@contextmenu" in text or "@touchstart" in text or
            "data-row-detail" in text)


# JS implementation -----------------------------------------------------


def test_app_js_implements_long_press_and_right_click():
    """Strict check: the JS must register an actual `contextmenu` and
    `touchstart` listener, not just mention them in a comment. The
    pre-fix version of this test passed against an Alpine-only binding
    that lived in the template and never executed JS code; that bug
    would have shipped if we'd trusted the keyword match."""
    from pathlib import Path
    js = (Path("app/static") / "app.js").read_text()

    # Right-click: must register a real listener on the document
    assert "addEventListener('contextmenu'" in js \
        or 'addEventListener("contextmenu"' in js
    # Long-press: must register a real touchstart listener
    assert "addEventListener('touchstart'" in js \
        or 'addEventListener("touchstart"' in js
