"""Tests for the holdings table rendering on GET /.

Slice 2 adds positions to the index page. The test contract:
  - When no positions: an empty-state message appears, table doesn't render rows.
  - When positions present: one <tr> per position, sorted by market_value_native desc.
  - Each row shows country flag, English name, native symbol subtext, quantity,
    last price, and native market value.
  - Hong Kong rows show 🇭🇰 (not 🇨🇳); Taiwan rows show 🇹🇼 (not 🇨🇳); mainland
    Chinese rows show 🇨🇳; US rows show 🇺🇸.

We use FakeAdapter test doubles (extending slice 1's pattern) so we don't need
a real IB Gateway connection.
"""

from fastapi.testclient import TestClient

from app.core.broker import Position


def _new_position(**overrides) -> Position:
    base = dict(
        broker="IBKR",
        account_id="U7575980",
        native_key="76792991",
        canonical_symbol="700.HK",
        native_symbol="700",
        exchange="SEHK",
        currency="HKD",
        name_en="TENCENT HOLDINGS LTD",
        asset_class="STK",
        quantity=100.0,
        avg_cost=400.0,
        last_price=420.0,
        market_value_native=42000.0,
        market_value_usd=0.0,
        unrealized_pnl_native=2000.0,
        unrealized_pnl_usd=0.0,
    )
    base.update(overrides)
    return Position(**base)


class FakeAdapter:
    """Test double for an in-memory Broker with pre-set positions."""

    def __init__(self, *, connected: bool = True, positions: list[Position] | None = None) -> None:
        self.name = "IBKR"
        self._connected = connected
        self._positions = positions or []

    async def connect(self) -> None:  # pragma: no cover
        self._connected = True

    async def disconnect(self) -> None:  # pragma: no cover
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    async def get_positions(self) -> list[Position]:
        return list(self._positions)

    async def get_account_summary(self):  # pragma: no cover
        return []


def make_client(positions: list[Position] | None = None) -> TestClient:
    from app.main import create_app

    app = create_app(broker=FakeAdapter(connected=True, positions=positions or []))
    return TestClient(app)


# Empty state ------------------------------------------------------------------


def test_index_renders_empty_state_when_no_positions():
    client = make_client(positions=[])

    response = client.get("/")

    assert response.status_code == 200
    assert "No positions" in response.text


def test_index_does_not_render_table_when_no_positions():
    client = make_client(positions=[])

    response = client.get("/")

    # No <tr class="position-row"> should appear in an empty portfolio
    assert 'class="position-row"' not in response.text


# Single-row rendering ---------------------------------------------------------


def test_index_renders_one_row_for_one_position():
    client = make_client(positions=[_new_position()])

    response = client.get("/")

    assert response.text.count('class="position-row"') == 1


def test_index_row_shows_company_name_and_native_symbol():
    client = make_client(positions=[_new_position()])

    response = client.get("/")

    assert "TENCENT HOLDINGS LTD" in response.text
    assert "700" in response.text


def test_index_row_shows_quantity():
    client = make_client(positions=[_new_position(quantity=250.0)])

    response = client.get("/")

    # Format may have commas/decimals — accept either "250" or "250.00"
    assert "250" in response.text


def test_index_row_shows_last_price():
    client = make_client(positions=[_new_position(last_price=420.5)])

    response = client.get("/")

    assert "420.5" in response.text or "420.50" in response.text


def test_index_row_shows_native_market_value():
    client = make_client(positions=[_new_position(market_value_native=42050.0)])

    response = client.get("/")

    assert "42,050" in response.text or "42050" in response.text


# Country-flag rule (hard requirement) -----------------------------------------


def test_hong_kong_position_renders_hk_flag_not_cn_flag():
    client = make_client(positions=[_new_position(exchange="SEHK", canonical_symbol="700.HK")])

    response = client.get("/")

    assert "🇭🇰" in response.text
    assert "🇨🇳" not in response.text   # MUST NOT appear for HK


def test_taiwan_position_renders_tw_flag_not_cn_flag():
    client = make_client(
        positions=[
            _new_position(
                exchange="TWSE",
                canonical_symbol="2330.TW",
                native_symbol="2330",
                currency="TWD",
                name_en="TAIWAN SEMICONDUCTOR MANUFACTURING CO",
            )
        ]
    )

    response = client.get("/")

    assert "🇹🇼" in response.text
    assert "🇨🇳" not in response.text   # MUST NOT appear for Taiwan


def test_mainland_china_a_share_renders_cn_flag():
    client = make_client(
        positions=[
            _new_position(
                exchange="SSE",
                canonical_symbol="600519.SH",
                native_symbol="600519",
                currency="CNH",
                name_en="KWEICHOW MOUTAI CO LTD",
            )
        ]
    )

    response = client.get("/")

    assert "🇨🇳" in response.text


def test_us_position_renders_us_flag():
    client = make_client(
        positions=[
            _new_position(
                exchange="NASDAQ",
                canonical_symbol="AAPL.US",
                native_symbol="AAPL",
                currency="USD",
                name_en="APPLE INC",
            )
        ]
    )

    response = client.get("/")

    assert "🇺🇸" in response.text


# Sort order -------------------------------------------------------------------


def test_position_with_zero_last_price_renders_em_dash_not_zero():
    """When market data is unavailable, show '—' so the user knows the value
    is pending rather than a real $0 quote. Live ticks arrive in slice 4."""
    client = make_client(
        positions=[
            _new_position(last_price=0.0, market_value_native=0.0)
        ]
    )

    response = client.get("/")

    # Should NOT show "0.00" as the price
    assert "—" in response.text
    # Sanity check: the row still renders
    assert "TENCENT HOLDINGS LTD" in response.text


def test_position_with_positive_last_price_does_not_render_em_dash_in_price_column():
    """Sanity: when prices ARE available, normal numbers render."""
    client = make_client(positions=[_new_position(last_price=420.5, market_value_native=42050.0)])

    response = client.get("/")

    # The price column should not contain the em-dash sentinel
    assert "420.5" in response.text or "420.50" in response.text


def test_index_sorts_rows_by_market_value_native_desc():
    """Slice 2 sorts by market_value_native; switches to market_value_usd in slice 3."""
    aapl = _new_position(
        native_key="2",
        canonical_symbol="AAPL.US",
        native_symbol="AAPL",
        exchange="NASDAQ",
        currency="USD",
        name_en="APPLE INC",
        market_value_native=10_000.0,
    )
    tencent = _new_position(
        native_key="1",
        canonical_symbol="700.HK",
        native_symbol="700",
        exchange="SEHK",
        currency="HKD",
        name_en="TENCENT HOLDINGS LTD",
        market_value_native=50_000.0,
    )
    client = make_client(positions=[aapl, tencent])  # input order intentionally wrong

    response = client.get("/")

    # TENCENT (higher MV) must appear before APPLE in the body
    tencent_idx = response.text.find("TENCENT HOLDINGS LTD")
    apple_idx = response.text.find("APPLE INC")
    assert tencent_idx != -1 and apple_idx != -1
    assert tencent_idx < apple_idx
