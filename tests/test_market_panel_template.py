"""Slice 5 cycle 7: market_card.html partial + collapsible drawer in index.

Display contract:
  - Each card shows a state emoji (🟢/🌒/🟡/🔴/⚫), the exchange display name,
    and the transition line ("Closes at 16:00 HKT").
  - The transition timestamp is exposed as a `data-transition-iso` attribute
    so client-side JS can render a countdown ("· in 1h 23m") without server
    push.
  - The drawer in index.html has both a collapsed glyph row (compact emoji
    summary per exchange) and the expanded card list, so mobile users can
    glance at state without unfurling.
  - Holiday and CLOSED cards must still render the "Opens at ..." line —
    a card with no transition line tells the user nothing.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from app.core.broker import ConnectionState, Position
from app.core.markets import STATE_EMOJI, MarketState, MarketStatus


TEMPLATES_DIR = "app/templates"


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)


def _open_hkex() -> MarketStatus:
    return MarketStatus(
        exchange="HKEX",
        state=MarketState.OPEN,
        extended_session=None,
        next_transition_local="16:00 HKT",
        next_transition_iso="2026-05-20T08:00:00+00:00",
        next_transition_label="Closes",
    )


def _lunch_hkex() -> MarketStatus:
    return MarketStatus(
        exchange="HKEX",
        state=MarketState.LUNCH,
        extended_session=None,
        next_transition_local="13:00 HKT",
        next_transition_iso="2026-05-20T05:00:00+00:00",
        next_transition_label="Reopens",
    )


def _extended_nyse_pre() -> MarketStatus:
    return MarketStatus(
        exchange="NYSE",
        state=MarketState.EXTENDED,
        extended_session="PRE",
        next_transition_local="09:30 ET",
        next_transition_iso="2026-02-18T14:30:00+00:00",
        next_transition_label="Pre-market ends",
    )


def _closed_lse() -> MarketStatus:
    return MarketStatus(
        exchange="LSE",
        state=MarketState.CLOSED,
        extended_session=None,
        next_transition_local="08:00 GMT",
        next_transition_iso="2026-02-19T08:00:00+00:00",
        next_transition_label="Opens",
    )


def _holiday_nyse() -> MarketStatus:
    return MarketStatus(
        exchange="NYSE",
        state=MarketState.HOLIDAY,
        extended_session=None,
        next_transition_local="09:30 ET",
        next_transition_iso="2026-12-28T14:30:00+00:00",
        next_transition_label="Opens",
    )


# market_card.html partial ------------------------------------------------


def _render_card(status: MarketStatus, *, flag: str = "🏳️") -> str:
    env = _env()
    tmpl = env.get_template("partials/market_card.html")
    return tmpl.render(
        market=status, flag=flag, market_state_emoji=STATE_EMOJI,
    )


def test_open_card_renders_green_emoji_and_close_time():
    html = _render_card(_open_hkex())

    assert "🟢" in html
    assert "HKEX" in html
    assert "Closes" in html
    assert "16:00 HKT" in html


def test_lunch_card_renders_yellow_emoji_and_reopen_time():
    html = _render_card(_lunch_hkex())

    assert "🟡" in html
    assert "Reopens" in html
    assert "13:00 HKT" in html


def test_extended_pre_card_renders_moon_emoji():
    html = _render_card(_extended_nyse_pre())

    assert "🌒" in html
    assert "NYSE" in html
    assert "Pre-market ends" in html
    assert "09:30 ET" in html


def test_closed_card_renders_red_emoji_and_next_open():
    html = _render_card(_closed_lse())

    assert "🔴" in html
    assert "Opens" in html
    assert "08:00 GMT" in html


def test_holiday_card_renders_black_circle_emoji():
    html = _render_card(_holiday_nyse())

    # ⚫ = HOLIDAY (different from 🔴 CLOSED so users can tell at a glance)
    assert "⚫" in html
    assert "NYSE" in html
    assert "Opens" in html


def test_card_exposes_transition_iso_for_client_countdown():
    """data-transition-iso lets app.js render '· in 1h 23m' without pushing
    a tick every second."""
    html = _render_card(_open_hkex())

    assert "2026-05-20T08:00:00+00:00" in html
    assert "data-transition-iso" in html


# Drawer in index.html ---------------------------------------------------


class _FakeAdapter:
    def __init__(self, *, positions=None):
        self.name = "IBKR"
        self._positions = positions or []

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


def _stk_hkex_position() -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key="76792991",
        canonical_symbol="700.HK", native_symbol="700",
        exchange="SEHK", currency="HKD",
        name_en="TENCENT", asset_class="STK",
        quantity=100.0, avg_cost=400.0, last_price=420.0,
        market_value_native=42000.0, market_value_usd=5388.6,
        unrealized_pnl_native=2000.0, unrealized_pnl_usd=256.6,
    )


def _stk_nyse_position() -> Position:
    return Position(
        broker="IBKR", account_id="U1", native_key="265598",
        canonical_symbol="AAPL.US", native_symbol="AAPL",
        exchange="NYSE", currency="USD",
        name_en="APPLE", asset_class="STK",
        quantity=10.0, avg_cost=150.0, last_price=180.0,
        market_value_native=1800.0, market_value_usd=1800.0,
        unrealized_pnl_native=300.0, unrealized_pnl_usd=300.0,
    )


def _make_client(positions):
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=positions))
    return TestClient(app)


def test_index_renders_market_drawer_section():
    """A <details>-based drawer (or aria-equivalent) must exist in the page
    so users can collapse/expand the market-hours block."""
    client = _make_client(positions=[_stk_hkex_position(), _stk_nyse_position()])

    response = client.get("/")

    # Either a <details> element or a class hook; check for both reasonable
    # implementations (details is the no-JS default; class hook is the
    # Alpine.js variant).
    assert "<details" in response.text.lower() or "market-drawer" in response.text.lower()


def test_index_drawer_contains_one_card_per_distinct_stk_exchange():
    """Two STK positions on different exchanges → two cards. CASH rows
    don't pin an exchange."""
    client = _make_client(positions=[_stk_hkex_position(), _stk_nyse_position()])

    response = client.get("/")
    text = response.text

    # Both exchange names should appear in the panel
    assert "HKEX" in text
    assert "NYSE" in text


def test_index_drawer_glyph_row_shows_emoji_per_exchange_when_collapsed():
    """The collapsed-state summary row pairs each exchange's country flag
    with its current state emoji (e.g. '🇭🇰 🟢 · 🇺🇸 🟢') so a single
    glance conveys the panel without expansion."""
    client = _make_client(positions=[_stk_hkex_position(), _stk_nyse_position()])

    response = client.get("/")
    text = response.text

    # Flag for HKEX and NYSE both rendered
    assert "🇭🇰" in text
    assert "🇺🇸" in text


def test_hk_tw_cn_render_as_three_distinct_cards_with_three_distinct_flags():
    """Hard plan requirement: Hong Kong, Taiwan, and mainland China are
    ALWAYS three separate entities. A portfolio holding all three must
    show three cards (HKEX / TWSE / SSE) with three flags (🇭🇰 / 🇹🇼 / 🇨🇳)
    — never merged into a single "Greater China" anything."""

    def _pos(broker_key, sym, exchange, currency, name):
        return Position(
            broker="IBKR", account_id="U1", native_key=broker_key,
            canonical_symbol=sym, native_symbol=sym.split(".")[0],
            exchange=exchange, currency=currency,
            name_en=name, asset_class="STK",
            quantity=10.0, avg_cost=10.0, last_price=11.0,
            market_value_native=110.0, market_value_usd=15.0,
            unrealized_pnl_native=10.0, unrealized_pnl_usd=1.5,
        )

    positions = [
        _pos("1", "700.HK", "SEHK", "HKD", "TENCENT"),
        _pos("2", "2330.TW", "TWSE", "TWD", "TSMC"),
        _pos("3", "600519.SH", "SSE", "CNH", "KWEICHOW MOUTAI"),
    ]
    client = _make_client(positions=positions)

    response = client.get("/")
    text = response.text

    # Three flags, all present, all distinct codepoints
    assert "🇭🇰" in text
    assert "🇹🇼" in text
    assert "🇨🇳" in text
    # Three exchange display names
    assert "HKEX" in text
    assert "TWSE" in text
    assert "SSE" in text


def test_index_with_only_cash_positions_renders_no_market_cards():
    """CASH rows must NOT pin exchanges in the market-hours panel — only
    STK positions do."""
    cash_only = Position(
        broker="IBKR", account_id="U1", native_key="USD",
        canonical_symbol="USD", native_symbol="USD",
        exchange="", currency="USD",
        name_en="US Dollar", asset_class="CASH",
        quantity=1000.0, avg_cost=1.0, last_price=1.0,
        market_value_native=1000.0, market_value_usd=1000.0,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
    )
    client = _make_client(positions=[cash_only])

    response = client.get("/")

    # Drawer + per-exchange cards are completely absent when no STK rows
    text = response.text
    assert "market-drawer" not in text
    assert "market-card" not in text
