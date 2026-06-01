"""The SSE stream must keep the hero totals ("Total exposure" / "Today")
live, not just the holdings rows.

The hero lives OUTSIDE the #positions-tbody that the SSE swap targets, so
without an out-of-band (OOB) fragment it freezes between full page reloads.
render_stream_payload renders the rows AND the hero figures as htmx OOB
elements (hx-swap-oob="true", stable ids) from one filtered/aggregated pass,
so each SSE tick updates the headline total alongside the rows. Totals are
computed against the *filtered* set so the hero matches whatever
account/asset/broker the client is viewing.
"""

from app.core.broker import Position


def _pos(*, canonical="X.HK", account="U1", broker="IBKR", asset_class="STK",
         mv_usd=5000.0, pnl_usd=200.0, fx_unavailable=False) -> Position:
    return Position(
        broker=broker, account_id=account, native_key=canonical,
        canonical_symbol=canonical, native_symbol="X", exchange="SEHK",
        currency="HKD", name_en="X CO", asset_class=asset_class,
        quantity=10.0, avg_cost=400.0, last_price=420.0,
        market_value_native=42000.0, market_value_usd=mv_usd,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=pnl_usd,
        fx_unavailable=fx_unavailable,
    )


def test_stream_payload_carries_rows_and_oob_hero():
    from app.render import render_stream_payload

    html = render_stream_payload(
        [_pos(canonical="700.HK", mv_usd=1000.0, pnl_usd=100.0),
         _pos(canonical="AAPL.US", mv_usd=500.0, pnl_usd=-50.0)],
    )

    # Rows are present (the swap target's content)...
    assert "700.HK" in html
    # ...and the hero rides along as an out-of-band fragment htmx swaps
    # outside the tbody, with a stable id and the summed (1000 + 500) total.
    assert 'hx-swap-oob="true"' in html
    assert 'id="ph-total-mv"' in html
    assert "$1,500" in html


def test_stream_payload_hero_respects_account_filter():
    from app.render import render_stream_payload

    html = render_stream_payload(
        [_pos(canonical="700.HK", account="U1", mv_usd=1000.0),
         _pos(canonical="AAPL.US", account="U2", mv_usd=9999.0)],
        active_account="U1",
    )

    # Only U1's $1,000 counts toward the hero; U2's row and value drop out.
    assert "$1,000" in html
    assert "9,999" not in html
    assert "AAPL.US" not in html
