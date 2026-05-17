"""Slice 8 review-fix: SSE must honor every filter dimension.

Slice 7 added the ?account= filter to /stream/holdings; slice 8 added
?asset= and ?broker= to the route but originally forgot to thread them
into the SSE path. Without this fix, the first 'snapshot' tick would
overwrite the filtered tbody with the unfiltered set, silently undoing
asset/broker filters within seconds.

These tests target `_render_rows_for_filter` and `_apply_filters`
directly (the SSE generator just hands a closure to `stream_events`,
so the unit-level test is the cleanest signal).
"""

import asyncio

import pytest

from app.core.broker import Position
from app.core.live_positions import LivePositions, stream_events


def _stk(account="U1", broker="IBKR", sym="AAPL.US", name="APPLE"):
    return Position(
        broker=broker, account_id=account,
        native_key=sym.split(".")[0],
        canonical_symbol=sym, native_symbol=sym.split(".")[0],
        exchange="NYSE", currency="USD",
        name_en=name, asset_class="STK",
        quantity=10.0, avg_cost=100.0, last_price=110.0,
        market_value_native=1100.0, market_value_usd=1100.0,
        unrealized_pnl_native=100.0, unrealized_pnl_usd=100.0,
    )


def _cash(account="U1", broker="IBKR", currency="HKD", name="Hong Kong Dollar"):
    return Position(
        broker=broker, account_id=account,
        native_key=currency, canonical_symbol=currency, native_symbol=currency,
        exchange="", currency=currency,
        name_en=name, asset_class="CASH",
        quantity=50000.0, avg_cost=1.0, last_price=1.0,
        market_value_native=50000.0, market_value_usd=6410.0,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
    )


# Helper: render via the production function ------------------------


def _render(positions, **filters):
    from app.main import _render_rows_for_filter
    return _render_rows_for_filter(positions, **filters)


# Asset filter on SSE path ----------------------------------------------


def test_sse_render_filters_by_asset_stk_excludes_cash():
    rows = _render(
        [_stk(name="APPLE"), _cash(name="Hong Kong Dollar")],
        active_asset="STK",
    )

    assert "APPLE" in rows
    assert "Hong Kong Dollar" not in rows


def test_sse_render_filters_by_asset_cash_excludes_stk():
    rows = _render(
        [_stk(name="APPLE"), _cash(name="Hong Kong Dollar")],
        active_asset="CASH",
    )

    assert "Hong Kong Dollar" in rows
    assert "APPLE" not in rows


# Broker filter on SSE path ---------------------------------------------


def test_sse_render_filters_by_broker():
    """V1 only has IBKR positions but the filter logic must still
    select-only the named broker. Future Futu/Tiger adapters will need
    this to work end-to-end."""
    rows = _render(
        [_stk(name="APPLE", broker="IBKR"), _stk(name="FUTU_STOCK", broker="Futu")],
        active_broker="IBKR",
    )

    assert "APPLE" in rows
    assert "FUTU_STOCK" not in rows


# All three filters compose --------------------------------------------


def test_sse_render_account_asset_broker_compose():
    positions = [
        _stk(name="AAPL_U1_IBKR", account="U1", broker="IBKR"),
        _stk(name="MSFT_U2_IBKR", account="U2", broker="IBKR"),
        _cash(name="HKD_U1_IBKR",  account="U1", broker="IBKR"),
    ]

    rows = _render(
        positions,
        active_account="U1", active_asset="STK", active_broker="IBKR",
    )

    assert "AAPL_U1_IBKR" in rows
    assert "MSFT_U2_IBKR" not in rows
    assert "HKD_U1_IBKR" not in rows


# End-to-end: stream_events delivers a filtered snapshot ---------------


async def _first_snapshot(generator) -> str:
    try:
        event = await asyncio.wait_for(generator.__anext__(), timeout=1.0)
    finally:
        await generator.aclose()
    assert event["event"] == "snapshot"
    return event["data"]


async def test_sse_snapshot_honors_asset_filter_end_to_end():
    """Plug the renderer into stream_events the way the SSE route
    does and assert the resulting wire-format event is filtered."""
    from app.main import _render_rows_for_filter

    live = LivePositions()
    live.set_position(_stk(name="APPLE"))
    live.set_position(_cash(name="Hong Kong Dollar"))

    def render(ps):
        return _render_rows_for_filter(ps, active_asset="STK")

    gen = stream_events(live, render, min_interval=0.01, heartbeat_interval=10.0)
    data = await _first_snapshot(gen)

    assert "APPLE" in data
    assert "Hong Kong Dollar" not in data


# Page wires the filters into the sse-connect URL ----------------------


def test_index_sse_connect_url_carries_all_active_filters():
    """Index must emit sse-connect="/stream/holdings?asset=STK&..."
    when filters are active so the SSE connection picks them up."""
    from fastapi.testclient import TestClient
    from app.core.broker import ConnectionState

    class FakeAdapter:
        name = "IBKR"
        async def connect(self): pass
        async def disconnect(self): pass
        async def is_connected(self): return True
        async def get_connection_state(self): return ConnectionState.CONNECTED
        async def get_positions(self): return [_stk()]
        async def get_account_summary(self): return []

    from app.main import create_app
    client = TestClient(create_app(broker=FakeAdapter()))

    response = client.get("/?asset=STK&broker=IBKR")

    text = response.text
    assert "/stream/holdings?" in text
    assert "asset=STK" in text
    assert "broker=IBKR" in text
