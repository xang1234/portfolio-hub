"""Slice 5 cycle 8: client-side countdown wiring.

The countdown itself runs in the browser — JS reads `data-transition-iso`
on each card and writes "· in 1h 23m" into `[data-countdown]`. These tests
verify the wiring (script tag, dataset attributes, target slot exist), not
the JS logic.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position


STATIC_DIR = Path("app/static")


class _FakeAdapter:
    def __init__(self, positions):
        self.name = "IBKR"
        self._positions = positions

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_connected(self):
        return True

    async def get_connection_state(self):
        return ConnectionState.CONNECTED

    async def get_positions(self):
        return list(self._positions)

    async def get_account_summary(self):
        return []


def _stk_position(exchange="SEHK") -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key="76792991",
        canonical_symbol="700.HK", native_symbol="700",
        exchange=exchange, currency="HKD",
        name_en="TENCENT", asset_class="STK",
        quantity=100.0, avg_cost=400.0, last_price=420.0,
        market_value_native=42000.0, market_value_usd=5388.6,
        unrealized_pnl_native=2000.0, unrealized_pnl_usd=256.6,
    )


def _make_client():
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=[_stk_position()]))
    return TestClient(app)


# JS file ------------------------------------------------------------------


def test_app_js_exists():
    """A static app.js must ship so the countdown can run in the browser."""
    assert (STATIC_DIR / "app.js").is_file()


def test_app_js_references_transition_iso_selector():
    """The countdown JS must query elements by `data-transition-iso` and
    write into `data-countdown`."""
    content = (STATIC_DIR / "app.js").read_text()

    assert "data-transition-iso" in content
    assert "data-countdown" in content


# Wiring in HTML -----------------------------------------------------------


def test_index_page_includes_app_js_script_tag():
    client = _make_client()

    response = client.get("/")

    assert "/static/app.js" in response.text


def test_market_card_has_countdown_slot():
    """Each card must have a `[data-countdown]` element JS can target."""
    client = _make_client()

    response = client.get("/")

    assert "data-countdown" in response.text


def test_market_card_has_transition_iso_attribute():
    """And the source of truth for the countdown lives in
    `data-transition-iso`."""
    client = _make_client()

    response = client.get("/")

    assert "data-transition-iso" in response.text
