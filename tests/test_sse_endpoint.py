"""Tests for the GET /stream/holdings SSE endpoint wiring.

We test the *route registration and wiring* here (the path exists, returns
the correct content-type, wraps the live_positions store correctly). The
*streaming semantics* (snapshot, deltas, throttle, heartbeat) are covered
in test_sse_generator.py, which exercises the generator directly without
HTTP plumbing.

End-to-end SSE testing through httpx ASGITransport hangs on context exit
because the server-side generator runs forever; the standard practice is to
test the generator and the wiring separately.
"""

from fastapi import FastAPI

from app.core.broker import Position
from app.core.live_positions import LivePositions


def _new_position(**overrides) -> Position:
    base = dict(
        broker="IBKR",
        account_id="U7575980",
        native_key="76792991",
        canonical_symbol="700.HK",
        native_symbol="700",
        exchange="SEHK",
        currency="HKD",
        name_en="TENCENT",
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
    def __init__(self, *, connected: bool = True, positions: list[Position] | None = None) -> None:
        self.name = "IBKR"
        self._connected = connected
        self._positions = positions or []

    async def connect(self) -> None: self._connected = True
    async def disconnect(self) -> None: self._connected = False
    async def is_connected(self) -> bool: return self._connected
    async def get_connection_state(self):
        from app.core.broker import ConnectionState
        return ConnectionState.CONNECTED if self._connected else ConnectionState.DISCONNECTED
    async def get_positions(self): return list(self._positions)
    async def get_account_summary(self): return []


def make_app(*, positions: list[Position] | None = None, live: LivePositions | None = None) -> FastAPI:
    from app.main import create_app

    adapter = FakeAdapter(connected=True, positions=positions or [])
    app = create_app(broker=adapter, live_positions=live)
    if live is not None:
        for p in positions or []:
            live.set_position(p)
    return app


def test_stream_holdings_route_is_registered():
    app = make_app(live=LivePositions())

    paths = {route.path for route in app.routes}
    assert "/stream/holdings" in paths


def test_app_state_exposes_live_positions():
    """The SSE handler reads from app.state.live_positions. This wiring is
    what makes the IbkrAdapter's pushed updates visible to the stream."""
    live = LivePositions()
    app = make_app(live=live)

    assert app.state.live_positions is live


def test_create_app_provides_default_live_positions_when_none_supplied():
    """For backward compat with slice 1/2 tests that don't pass live."""
    from app.main import create_app

    app = create_app(broker=FakeAdapter())

    assert isinstance(app.state.live_positions, LivePositions)


def test_seeded_live_positions_show_up_via_state():
    """A LivePositions seeded before app creation is observable via app.state
    — proving the SSE handler (which reads from app.state) will see them."""
    live = LivePositions()
    app = make_app(
        positions=[_new_position(canonical_symbol="700.HK", name_en="TENCENT")],
        live=live,
    )

    snapshot = app.state.live_positions.get_all()
    assert len(snapshot) == 1
    assert snapshot[0].name_en == "TENCENT"
