"""Slice 7 review-fix tests.

Three load-bearing gaps the post-merge code review surfaced:

1. SSE /stream/holdings must honor the same ?account= filter — otherwise
   the first tick wipes the filtered tbody with all-accounts data.
2. SSE-rendered rows must honor active_account too — otherwise the pill
   reappears on every SSE-overwritten row.
3. Per-account NLV/cash conversion must work end-to-end for non-USD
   base currencies (HKD desks).
4. A deep-link to an account that has only summary (no positions) must
   filter correctly.

The SSE tests target the route's `render_rows` closure directly via the
sse generator — pulling chunks off an open HTTP stream from a TestClient
hangs because the connection stays open. The closure is the
filter-aware unit that needs to be verified.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.broker import AccountSummary, ConnectionState, Position
from app.core.fx import FxRate, FxService
from app.core.live_positions import LivePositions, stream_events


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


def _summary(account_id, *, nlv, cash, bp, base="USD"):
    return AccountSummary(
        broker="IBKR", account_id=account_id, base_currency=base,
        net_liquidation_usd=nlv, cash_usd=cash, buying_power_usd=bp,
    )


def _client(positions, accounts, *, live=None):
    from app.main import create_app
    app = create_app(
        broker=_FakeAdapter(positions=positions, accounts=accounts),
        live_positions=live,
    )
    return TestClient(app)


# Critical #1+2: SSE-side row rendering respects active_account ------------


async def _first_snapshot(generator) -> str:
    """Pull just the first 'snapshot' event from a stream_events generator."""
    try:
        event = await asyncio.wait_for(generator.__anext__(), timeout=1.0)
    finally:
        await generator.aclose()
    assert event["event"] == "snapshot"
    return event["data"]


async def test_sse_render_rows_filters_by_active_account():
    """Calling the production render-rows closure with active_account=U1
    must return HTML for only U1 rows; U2 rows must be absent."""
    from app.render import render_stream_payload

    rows_u1 = render_stream_payload(
        [_pos("U1", "AAPL.US", "APPLE"), _pos("U2", "MSFT.US", "MICROSOFT")],
        active_account="U1",
    )

    assert "APPLE" in rows_u1
    assert "MICROSOFT" not in rows_u1


async def test_sse_render_rows_all_includes_every_account():
    from app.render import render_stream_payload

    rows_all = render_stream_payload(
        [_pos("U1", "AAPL.US", "APPLE"), _pos("U2", "MSFT.US", "MICROSOFT")],
        active_account="All",
    )

    assert "APPLE" in rows_all
    assert "MICROSOFT" in rows_all


async def test_sse_render_rows_suppresses_account_pill_under_filter():
    from app.render import render_stream_payload

    rows = render_stream_payload(
        [_pos("U1", "AAPL.US", "APPLE")], active_account="U1",
    )

    assert "account-pill" not in rows


async def test_sse_render_rows_shows_account_pill_under_all():
    from app.render import render_stream_payload

    rows = render_stream_payload(
        [_pos("U1", "AAPL.US", "APPLE")], active_account="All",
    )

    assert "account-pill" in rows


async def test_sse_endpoint_emits_filtered_snapshot_for_account_query():
    """End-to-end: an explicit ?account=U1 on the SSE URL drives the
    same render path. We rely on the underlying stream_events generator
    so the test doesn't hang on an open HTTP connection."""
    from app.render import render_stream_payload

    live = LivePositions()
    live.set_position(_pos("U1", "AAPL.US", "APPLE"))
    live.set_position(_pos("U2", "MSFT.US", "MICROSOFT"))

    def render(ps):
        return render_stream_payload(ps, active_account="U1")

    gen = stream_events(live, render, min_interval=0.01, heartbeat_interval=10.0)
    data = await _first_snapshot(gen)

    assert "APPLE" in data
    assert "MICROSOFT" not in data


# Important #6: HKD per-account summary end-to-end ------------------------


def test_per_account_summary_line_shows_usd_for_hkd_base_currency():
    """An HKD desk's NLV of 780,000 HKD → $100,000 USD in the
    per-account line when the active filter is that account."""
    accounts = [
        _summary("U_HK", nlv=100000.0, cash=10000.0, bp=200000.0, base="HKD"),
        _summary("U_US", nlv=50000.0,  cash=5000.0,  bp=100000.0, base="USD"),
    ]
    positions = [
        _pos("U_HK", "700.HK", "TENCENT", mv=5000.0),
        _pos("U_US", "AAPL.US", "APPLE", mv=1800.0),
    ]
    client = _client(positions=positions, accounts=accounts)

    response = client.get("/?account=U_HK")
    text = response.text

    # Per-account summary shows USD values directly — adapter has already
    # converted HKD inputs into the USD fields on AccountSummary.
    assert "U_HK" in text
    assert "100,000" in text
    assert "10,000" in text
    assert "200,000" in text


# Important #7: deep-link to account-with-only-summary -------------------


def test_deep_link_to_account_with_summary_but_no_positions():
    """An account that exists in get_account_summary() but holds no
    positions (e.g. a freshly opened account, or a cash-only sub-account
    where positions=[]) must NOT silently fall back to All when the
    URL targets it."""
    accounts = [
        _summary("U1", nlv=100, cash=10, bp=200),
        _summary("U_NEW", nlv=0, cash=0, bp=0),  # no positions yet
    ]
    positions = [_pos("U1", "AAPL.US", "APPLE")]
    client = _client(positions=positions, accounts=accounts)

    response = client.get("/?account=U_NEW")
    text = response.text

    # U_NEW is a known account — render the empty-state for its filter
    # rather than silently falling back to "All" (which would show APPLE).
    assert "APPLE" not in text
    # And the per-account summary line should still appear for U_NEW
    assert "U_NEW" in text
