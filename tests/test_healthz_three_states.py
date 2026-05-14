"""Tests that /healthz reports CONNECTED / RECONNECTING / DISCONNECTED
and the status badge template renders each state distinctly.
"""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState


class FakeAdapter:
    """Fake Broker that lets tests set the connection state directly."""

    def __init__(self, *, state: ConnectionState = ConnectionState.DISCONNECTED) -> None:
        self.name = "IBKR"
        self._state = state

    async def connect(self) -> None: pass
    async def disconnect(self) -> None: pass
    async def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED
    async def get_connection_state(self) -> ConnectionState:
        return self._state
    async def get_positions(self): return []
    async def get_account_summary(self): return []


def make_client(state: ConnectionState) -> TestClient:
    from app.main import create_app

    app = create_app(broker=FakeAdapter(state=state))
    return TestClient(app)


# JSON shape -------------------------------------------------------------------


def test_healthz_returns_connected_string_when_state_is_CONNECTED():
    response = make_client(ConnectionState.CONNECTED).get("/healthz")
    assert response.json() == {"ibkr": "connected"}


def test_healthz_returns_reconnecting_string_when_state_is_RECONNECTING():
    response = make_client(ConnectionState.RECONNECTING).get("/healthz")
    assert response.json() == {"ibkr": "reconnecting"}


def test_healthz_returns_disconnected_string_when_state_is_DISCONNECTED():
    response = make_client(ConnectionState.DISCONNECTED).get("/healthz")
    assert response.json() == {"ibkr": "disconnected"}


# HTMX badge fragment ---------------------------------------------------------


def test_badge_renders_yellow_dot_and_reconnecting_label_for_RECONNECTING():
    client = make_client(ConnectionState.RECONNECTING)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "🟡" in response.text
    assert "IBKR reconnecting" in response.text


def test_badge_renders_green_for_CONNECTED():
    client = make_client(ConnectionState.CONNECTED)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "🟢" in response.text
    assert "IBKR connected" in response.text


def test_badge_renders_red_for_DISCONNECTED():
    client = make_client(ConnectionState.DISCONNECTED)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "🔴" in response.text
    assert "IBKR disconnected" in response.text


# Initial page-load badge -----------------------------------------------------


def test_index_page_initial_badge_reflects_reconnecting_state():
    """The first time you load the page during a reconnect window, you should
    see 🟡 right away — not 🔴 (until the next HTMX poll updates it)."""
    client = make_client(ConnectionState.RECONNECTING)

    response = client.get("/")

    assert "🟡" in response.text
    assert "IBKR reconnecting" in response.text
