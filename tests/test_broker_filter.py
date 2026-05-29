"""Slice 8 cycle 2: ?broker= filter + chip strip.

V1 only enables IBKR, so the filter is mostly future-proofing for when
Futu / Tiger / Longbridge adapters land. The chip strip surfaces the
intended dimensions so the UI is structurally complete now and adapters
slot in cleanly later.

Disabled brokers (Futu, Tiger, Longbridge in v1) appear as grayed,
non-clickable chips so users know the dimension exists but can see at
a glance it isn't wired yet.
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


def _pos(broker="IBKR", sym="AAPL.US", name="APPLE"):
    return Position(
        broker=broker, account_id="U1", native_key=sym.split(".")[0],
        canonical_symbol=sym, native_symbol=sym.split(".")[0],
        exchange="NYSE", currency="USD",
        name_en=name, asset_class="STK",
        quantity=10.0, avg_cost=100.0, last_price=110.0,
        market_value_native=1000.0, market_value_usd=1000.0,
        unrealized_pnl_native=100.0, unrealized_pnl_usd=100.0,
    )


def _client(positions):
    from app.main import create_app
    app = create_app(broker=_FakeAdapter(positions=positions))
    return TestClient(app)


# Chip rendering ----------------------------------------------------------


def test_broker_chips_show_all_plus_ibkr_plus_grayed_others():
    """Chip strip surfaces every broker dimension. Disabled brokers
    render with the disabled class so users see "future" not "broken"."""
    response = _client([_pos()]).get("/")
    text = response.text

    assert "broker-chip" in text
    # Enabled
    assert ">All<" in text
    assert ">IBKR<" in text
    # Disabled-but-visible
    assert "Futu" in text
    assert "Tiger" in text
    assert "Longbridge" in text
    # The disabled ones carry a visual marker
    assert "broker-chip--disabled" in text


def test_disabled_broker_chips_are_not_clickable():
    """Disabled chips must not have an href (or be wrapped in <a>) —
    they're informational. Otherwise users would click and get a confusing
    empty page."""
    response = _client([_pos()]).get("/")
    text = response.text

    # The disabled chips must NOT include hrefs targeting their broker
    assert "?broker=Futu" not in text
    assert "?broker=Tiger" not in text
    assert "?broker=Longbridge" not in text


def test_active_broker_chip_marked():
    response = _client([_pos()]).get("/?broker=IBKR")

    text = response.text
    assert "broker-chip--active" in text or 'aria-current="page"' in text


# Filter behavior --------------------------------------------------------


def test_broker_ibkr_passes_through():
    """All current positions are IBKR, so the filter is a no-op."""
    response = _client([_pos()]).get("/?broker=IBKR")

    assert "APPLE" in response.text


def test_unknown_broker_falls_back_to_all():
    """Stale bookmark, typo, or grayed-broker URL hit by accident →
    show everything rather than blank page."""
    response = _client([_pos()]).get("/?broker=NotARealBroker")

    assert response.status_code == 200
    assert "APPLE" in response.text


# Composition with other filters ---------------------------------------


def test_broker_chips_carry_existing_account_and_asset_params():
    """Switching broker preserves the account+asset filter."""
    response = _client([_pos()]).get("/?account=U1&asset=STK")

    text = response.text
    # The IBKR chip's URL should carry the other filters
    assert "broker=IBKR" in text
    assert "account=U1" in text
    assert "asset=STK" in text


def test_account_chip_url_carries_active_broker():
    """The account/asset chips also need to carry ?broker= forward, so
    no filter is silently lost when switching dimensions."""
    response = _client([_pos()]).get("/?broker=IBKR")

    # asset chips should include broker=IBKR
    text = response.text
    assert "broker=IBKR" in text and "asset=STK" in text


def test_longbridge_chip_is_clickable_when_enabled(monkeypatch):
    monkeypatch.setenv("BROKERS_ENABLED", "ibkr,longbridge")

    response = _client([_pos()]).get("/")

    assert "?broker=Longbridge" in response.text
