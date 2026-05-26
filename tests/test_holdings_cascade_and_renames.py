"""Cascade + naming regression guards introduced after a code review.

Three concerns:

1. **P&L color cascade.** `.holdings-table tbody td` sets a default `color:
   var(--ph-text)` for row text. The `.pnl-positive` / `.pnl-negative`
   modifier classes paint green / red onto the P&L wrapper, and they only
   stay visible because the td color rule is scoped via `:where()` (zero
   specificity) — so the modifier classes win the cascade. If anyone ever
   rewrites the td rule with normal specificity, the modifier classes lose
   silently and red losses turn white. These tests assert the *intent*: the
   modifier class actually lands on the wrapper element.

2. **Euronext rename.** SBF / AEB display as just "Euronext" rather than
   "Euronext Paris" / "Euronext Amsterdam". The country flag on each market
   card differentiates them. Asserts the renamed display strings so a
   revert can't land silently.

3. **Price-source tags stack.** `prev close` + `broker mark` + `delayed`
   render under a single `.price-source-tags` wrapper rendered *below* the
   intraday `.chg-pct` line — not inline with the price. Asserts the
   structure so a future cell refactor doesn't drift back to the wide,
   inline layout.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.core.broker import ConnectionState, Position
from app.core.markets import display_name_for_ib_exchange


# ---- Shared fixtures ------------------------------------------------------


class _Fake:
    name = "IBKR"

    def __init__(self, positions):
        self._p = positions

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return list(self._p)
    async def get_account_summary(self): return []


def _stk(**overrides) -> Position:
    base = dict(
        broker="IBKR", account_id="U1", native_key="0",
        canonical_symbol="X.US", native_symbol="X",
        exchange="NASDAQ", currency="USD",
        name_en="X Corp", asset_class="STK",
        quantity=100.0, avg_cost=50.0, last_price=60.0,
        market_value_native=6000.0, market_value_usd=6000.0,
        unrealized_pnl_native=1000.0, unrealized_pnl_usd=1000.0,
    )
    base.update(overrides)
    return Position(**base)


def _client(positions):
    from app.main import create_app
    return TestClient(create_app(broker=_Fake(positions)))


# ---- P&L color cascade ----------------------------------------------------


def test_positive_pnl_row_carries_pnl_positive_class():
    """Locks in that the modifier class lands on the .pnl-stack wrapper.
    Combined with the :where()-scoped td color rule, this is the only
    invariant the cascade needs."""
    positions = [_stk(unrealized_pnl_usd=500, market_value_usd=5500)]
    text = _client(positions).get("/").text

    assert re.search(r'<div class="pnl-stack pnl-positive">', text)


def test_negative_pnl_row_carries_pnl_negative_class():
    positions = [_stk(unrealized_pnl_usd=-200, market_value_usd=4800)]
    text = _client(positions).get("/").text

    assert re.search(r'<div class="pnl-stack pnl-negative">', text)


def test_holdings_td_color_rule_is_scoped_via_where_for_zero_specificity():
    """Static CSS check: the default-text rule on tbody td must live inside
    :where() so .pnl-positive (specificity 0,1,0) wins the cascade against
    it. Without the where(), the td rule (specificity 0,2,1) would beat any
    single-class modifier and silently turn coloured P&L back to white."""
    from pathlib import Path

    css = Path("app/static/app.css").read_text()
    assert ":where(.holdings-table tbody td) { color:" in css, (
        "color rule on tbody td must be inside :where() — see commit 9 "
        "review notes"
    )


# ---- Euronext rename ------------------------------------------------------


@pytest.mark.parametrize("code", ["SBF", "AEB"])
def test_euronext_venues_display_as_just_euronext(code):
    """SBF (Paris) and AEB (Amsterdam) both render as "Euronext". The
    country flag on each market card differentiates them — saves
    horizontal space in the rail when both are open."""
    assert display_name_for_ib_exchange(code) == "Euronext"


def test_euronext_paris_and_amsterdam_render_distinct_flags():
    """When both SBF (Paris) and AEB (Amsterdam) are present, each market
    card must show its own country flag — Paris 🇫🇷, Amsterdam 🇳🇱. An
    earlier bug keyed the flag dict by display name; both venues display
    as "Euronext", so the second insert silently overwrote the first and
    both cards rendered with the same flag.

    Assertions are scoped to the market-card partial's flag markup
    (`market-card__flag">🇫🇷` etc., see partials/market_card.html). A
    page-wide `"🇫🇷" in text` check is too loose — holdings_row.html
    also emits the country flag per STK row, so the emojis would appear
    in the rendered HTML even with the rail bug present.
    """
    paris = _stk(
        canonical_symbol="AIR.FR", native_symbol="AIR",
        exchange="SBF", currency="EUR",
        name_en="Airbus SE", asset_class="STK",
    )
    amsterdam = _stk(
        canonical_symbol="ASML.NL", native_symbol="ASML",
        exchange="AEB", currency="EUR",
        name_en="ASML Holding NV", asset_class="STK",
    )
    text = _client([paris, amsterdam]).get("/").text

    # Both flags must appear inside the market-card__flag span — this is
    # the rail markup specifically, not the holdings-row flag chips.
    assert 'market-card__flag">🇫🇷' in text, (
        "Paris market card should render the French flag"
    )
    assert 'market-card__flag">🇳🇱' in text, (
        "Amsterdam market card should render the Dutch flag"
    )


# ---- Price-source tags stack ---------------------------------------------


def test_price_source_tags_stack_below_price_under_one_wrapper():
    """When a row carries multiple price-source flags (prev_close +
    broker_mark + delayed) they render together under a single
    .price-source-tags wrapper *below* the intraday %, not inline with
    the price. Locks in the user-requested "tags under the price to
    avoid making too wide" layout."""
    p = _stk(
        last_price_is_previous_close=True,
        last_price_is_broker_mark=True,
        last_price_is_delayed=True,
        previous_close=58.0,  # so intraday chg-pct renders too
    )
    text = _client([p]).get("/").text

    # All three tags emit
    assert "prev close" in text
    assert "broker mark" in text
    assert "delayed" in text

    # Inside a single price-source-tags wrapper
    wrapper = re.search(
        r'<span class="price-source-tags">.*?</span>', text, re.DOTALL,
    )
    assert wrapper, "expected a .price-source-tags wrapper"
    body = wrapper.group(0)
    assert "prev close" in body
    assert "broker mark" in body
    assert "delayed" in body


def test_price_source_tags_omitted_when_no_source_flags():
    """No flag → no wrapper. The cell stays compact for the common case."""
    p = _stk()  # no last_price_is_* flags
    text = _client([p]).get("/").text

    assert "price-source-tags" not in text


def test_stale_badge_stays_inline_with_price():
    """The ⚠️ stale glyph is a single character that *should* sit inline
    with the price (not stacked) — it's a visual urgency cue, not a tag."""
    p = _stk(last_price_is_stale=True)
    text = _client([p]).get("/").text

    # ⚠️ appears inside the last-price-line span (same line as the price),
    # not inside a price-source-tags wrapper.
    last_price_block = re.search(
        r'<span[^>]*>\s*60\.00.*?</span>', text, re.DOTALL,
    )
    assert last_price_block
    assert "price-stale-badge" in last_price_block.group(0)
