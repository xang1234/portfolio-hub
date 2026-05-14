"""Tests for the FastAPI app surface in slice 1.

Two endpoints are in scope:
  - GET /healthz  -> JSON {"ibkr": "connected"|"disconnected"}
  - GET /         -> HTML with a sticky connection badge that auto-refreshes
                     via HTMX hx-get="/healthz" hx-trigger="every 5s"

We inject a FakeAdapter via the create_app factory so tests don't need a real
IB Gateway and the app doesn't auto-connect on import.
"""

import pytest
from fastapi.testclient import TestClient


class FakeAdapter:
    """Test double satisfying the Broker Protocol for slice 1 endpoint tests."""

    def __init__(self, *, connected: bool) -> None:
        self.name = "IBKR"
        self._connected = connected

    async def connect(self) -> None:  # pragma: no cover — not exercised in slice 1 endpoint tests
        self._connected = True

    async def disconnect(self) -> None:  # pragma: no cover
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    async def get_positions(self):  # pragma: no cover — slice 1 tests don't render rows
        return []

    async def get_account_summary(self):  # pragma: no cover
        return []


def make_client(*, connected: bool) -> TestClient:
    from app.main import create_app

    app = create_app(broker=FakeAdapter(connected=connected))
    return TestClient(app)


# /healthz ---------------------------------------------------------------------


def test_healthz_reports_connected_when_adapter_is_connected():
    client = make_client(connected=True)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ibkr": "connected"}


def test_healthz_reports_disconnected_when_adapter_is_not_connected():
    client = make_client(connected=False)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ibkr": "disconnected"}


def test_healthz_content_type_is_application_json():
    client = make_client(connected=True)

    response = client.get("/healthz")

    assert response.headers["content-type"].startswith("application/json")


# / (index) --------------------------------------------------------------------


def test_index_returns_200_and_html():
    client = make_client(connected=True)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_index_contains_viewport_meta_for_mobile():
    client = make_client(connected=True)

    response = client.get("/")

    assert 'name="viewport"' in response.text
    assert "width=device-width" in response.text


def test_index_loads_picocss():
    client = make_client(connected=True)

    response = client.get("/")

    assert "pico" in response.text.lower()


def test_index_loads_htmx():
    client = make_client(connected=True)

    response = client.get("/")

    assert "htmx" in response.text.lower()


def test_index_contains_connection_badge_element_with_known_id():
    client = make_client(connected=True)

    response = client.get("/")

    # The badge element must have a known id so SSE / HTMX can target it later
    assert 'id="ibkr-status"' in response.text


def test_index_badge_auto_refreshes_via_htmx_every_5s():
    client = make_client(connected=True)

    response = client.get("/")

    # Slice 1 acceptance criterion: badge auto-refreshes every 5s
    assert 'hx-get="/healthz"' in response.text
    assert 'hx-trigger="every 5s"' in response.text


def test_index_initial_badge_renders_connected_when_adapter_is_connected():
    client = make_client(connected=True)

    response = client.get("/")

    # Initial server-rendered state should show connected — no flash of "disconnected"
    assert "🟢" in response.text
    assert "IBKR connected" in response.text


def test_index_initial_badge_renders_disconnected_when_adapter_is_not_connected():
    client = make_client(connected=False)

    response = client.get("/")

    assert "🔴" in response.text
    assert "IBKR disconnected" in response.text
