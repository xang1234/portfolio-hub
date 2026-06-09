"""Manual retry button: when backoff is exhausted and we're sitting in
DISCONNECTED, the badge becomes clickable so the user can kick off a
fresh start() without restarting the container.
"""

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState


class FakeAdapter:
    def __init__(self, *, state: ConnectionState) -> None:
        self.name = "IBKR"
        self._state = state
        self.start_calls = 0

    async def connect(self) -> None: pass
    async def disconnect(self) -> None: pass
    async def is_connected(self) -> bool: return self._state == ConnectionState.CONNECTED
    async def get_connection_state(self) -> ConnectionState: return self._state
    async def get_positions(self): return []
    async def get_account_summary(self): return []

    async def start(self) -> None:
        self.start_calls += 1
        # Simulate a successful boot — production start() would call connect()
        self._state = ConnectionState.CONNECTED


class FakeRetryNowAdapter(FakeAdapter):
    def __init__(self, *, state: ConnectionState) -> None:
        super().__init__(state=state)
        self.retry_now_calls = 0

    async def retry_now(self) -> None:
        self.retry_now_calls += 1
        self._state = ConnectionState.CONNECTED


def make_client(state: ConnectionState) -> tuple[TestClient, FakeAdapter]:
    from app.main import create_app

    adapter = FakeAdapter(state=state)
    app = create_app(broker=adapter)
    return TestClient(app), adapter


# Badge markup ----------------------------------------------------------------


def test_disconnected_badge_has_clickable_retry_action():
    """The DISCONNECTED badge should be a clickable element that triggers
    a manual retry via HTMX."""
    client, _ = make_client(ConnectionState.DISCONNECTED)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    # hx-post to a retry endpoint, fired on click
    assert "/healthz/retry" in response.text
    assert "hx-post" in response.text or "hx-get" in response.text


def test_disconnected_badge_shows_retry_affordance_in_label():
    """Users need to know the badge is interactive. Show 'retry' (or
    similar) in the label, not just the emoji."""
    client, _ = make_client(ConnectionState.DISCONNECTED)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "retry" in response.text.lower() or "click" in response.text.lower()


def test_connected_badge_has_no_retry_action():
    client, _ = make_client(ConnectionState.CONNECTED)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/healthz/retry" not in response.text


def test_reconnecting_badge_has_no_retry_action():
    """A loop is already running — don't expose a redundant button."""
    client, _ = make_client(ConnectionState.RECONNECTING)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/healthz/retry" not in response.text


# Endpoint behavior -----------------------------------------------------------


def test_post_healthz_retry_calls_start_when_disconnected():
    client, adapter = make_client(ConnectionState.DISCONNECTED)

    response = client.post("/healthz/retry")

    assert response.status_code == 200
    assert adapter.start_calls == 1


def test_post_healthz_retry_returns_updated_badge_fragment():
    """The endpoint returns the new badge so HTMX can swap it in place."""
    client, _ = make_client(ConnectionState.DISCONNECTED)

    response = client.post("/healthz/retry")

    # FakeAdapter's start() flips to CONNECTED, so the response should
    # show the green badge.
    assert "🟢" in response.text
    assert 'id="ibkr-status"' in response.text


def test_post_healthz_retry_is_noop_when_already_connected():
    """Clicking retry on a connected adapter must not call start() —
    that would tear down the live IB session."""
    client, adapter = make_client(ConnectionState.CONNECTED)

    response = client.post("/healthz/retry")

    assert response.status_code == 200
    assert adapter.start_calls == 0


def test_post_healthz_retry_is_noop_when_reconnecting():
    """A loop is already running — don't spawn a parallel one."""
    client, adapter = make_client(ConnectionState.RECONNECTING)

    response = client.post("/healthz/retry")

    assert response.status_code == 200
    assert adapter.start_calls == 0


def test_post_healthz_retry_calls_retry_now_when_reconnecting():
    from app.main import create_app

    adapter = FakeRetryNowAdapter(state=ConnectionState.RECONNECTING)
    client = TestClient(create_app(broker=adapter))

    response = client.post("/healthz/retry")

    assert response.status_code == 200
    assert adapter.retry_now_calls == 1
    assert adapter.start_calls == 0
    assert "connected" in response.text
