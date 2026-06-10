"""Manual retry button: when backoff is exhausted and we're sitting in
DISCONNECTED, the badge becomes clickable so the user can kick off a
fresh start() without restarting the container.
"""

from html.parser import HTMLParser

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState


class FakeAdapter:
    def __init__(self, *, state: ConnectionState, name: str = "IBKR") -> None:
        self.name = name
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


class ConnectOnlyAdapter:
    name = "IBKR"

    def __init__(self, *, state: ConnectionState) -> None:
        self._state = state
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        self._state = ConnectionState.CONNECTED

    async def disconnect(self) -> None: pass
    async def is_connected(self) -> bool: return self._state == ConnectionState.CONNECTED
    async def get_connection_state(self) -> ConnectionState: return self._state
    async def get_positions(self): return []
    async def get_account_summary(self): return []


def make_client(state: ConnectionState) -> tuple[TestClient, FakeAdapter]:
    from app.main import create_app

    adapter = FakeAdapter(state=state)
    app = create_app(broker=adapter)
    return TestClient(app), adapter


class _ButtonAttrsByPost(HTMLParser):
    def __init__(self, hx_post: str) -> None:
        super().__init__()
        self._hx_post = hx_post
        self.attrs: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        current_attrs = dict(attrs)
        if tag == "button" and current_attrs.get("hx-post") == self._hx_post:
            self.attrs = current_attrs


def _button_attrs_by_post(html: str, hx_post: str) -> dict[str, str | None]:
    parser = _ButtonAttrsByPost(hx_post)
    parser.feed(html)
    assert parser.attrs is not None
    return parser.attrs


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


def test_reconnecting_badge_has_retry_action():
    """RECONNECTING exposes retry_now so users can wake the loop immediately."""
    client, _ = make_client(ConnectionState.RECONNECTING)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/healthz/retry" in response.text
    assert "Retry now" in response.text


def test_restart_action_is_hidden_when_not_configured(monkeypatch):
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "   ")
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    client, _ = make_client(ConnectionState.DISCONNECTED)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/admin/ibkr-gateway/restart" not in response.text
    assert "Restart Gateway" not in response.text


def test_restart_action_is_hidden_when_token_auth_is_configured(monkeypatch):
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "true")
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    monkeypatch.delenv("ADMIN_ALLOW_NO_AUTH", raising=False)
    client, _ = make_client(ConnectionState.RECONNECTING)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/admin/ibkr-gateway/restart" not in response.text
    assert "Restart Gateway" not in response.text


def test_restart_action_is_hidden_when_token_precedes_allow_no_auth(monkeypatch):
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "true")
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    client, _ = make_client(ConnectionState.RECONNECTING)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/admin/ibkr-gateway/restart" not in response.text
    assert "Restart Gateway" not in response.text


def test_restart_action_is_hidden_when_whitespace_token_precedes_allow_no_auth(monkeypatch):
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "true")
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("ADMIN_TOKEN", "   ")
    client, _ = make_client(ConnectionState.RECONNECTING)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/admin/ibkr-gateway/restart" not in response.text
    assert "Restart Gateway" not in response.text


def test_restart_action_is_hidden_when_admin_auth_is_not_satisfied(monkeypatch):
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "true")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_ALLOW_NO_AUTH", raising=False)
    client, _ = make_client(ConnectionState.DISCONNECTED)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/admin/ibkr-gateway/restart" not in response.text
    assert "Restart Gateway" not in response.text


def test_restart_action_is_shown_when_no_auth_admin_restart_is_configured(monkeypatch):
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "true")
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    client, _ = make_client(ConnectionState.RECONNECTING)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/admin/ibkr-gateway/restart" in response.text
    assert "Restart Gateway" in response.text


def test_restart_action_is_hidden_when_only_non_ibkr_child_is_down(monkeypatch):
    from app.core.composite_broker import CompositeBroker
    from app.main import create_app

    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "true")
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = create_app(
        broker=CompositeBroker([
            FakeAdapter(state=ConnectionState.CONNECTED, name="IBKR"),
            FakeAdapter(state=ConnectionState.DISCONNECTED, name="Futu"),
        ])
    )
    response = TestClient(app).get("/healthz", headers={"HX-Request": "true"})

    assert "/admin/ibkr-gateway/restart" not in response.text
    assert "Restart Gateway" not in response.text


def test_restart_action_is_shown_when_ibkr_child_is_down(monkeypatch):
    from app.core.composite_broker import CompositeBroker
    from app.main import create_app

    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "true")
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = create_app(
        broker=CompositeBroker([
            FakeAdapter(state=ConnectionState.DISCONNECTED, name="IBKR"),
            FakeAdapter(state=ConnectionState.CONNECTED, name="Futu"),
        ])
    )
    response = TestClient(app).get("/healthz", headers={"HX-Request": "true"})

    assert "/admin/ibkr-gateway/restart" in response.text
    assert "Restart Gateway" in response.text


def test_index_restart_action_uses_same_visibility_context(monkeypatch):
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "true")
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    client, _ = make_client(ConnectionState.RECONNECTING)
    response = client.get("/")

    assert "/admin/ibkr-gateway/restart" in response.text
    assert "Restart Gateway" in response.text


def test_index_restart_action_hides_when_token_precedes_allow_no_auth(monkeypatch):
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "true")
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    client, _ = make_client(ConnectionState.RECONNECTING)
    response = client.get("/")

    assert "/admin/ibkr-gateway/restart" not in response.text
    assert "Restart Gateway" not in response.text


def test_restart_action_does_not_swap_json_into_status_badge(monkeypatch):
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "true")
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    client, _ = make_client(ConnectionState.RECONNECTING)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    restart_button = _button_attrs_by_post(response.text, "/admin/ibkr-gateway/restart")
    assert restart_button.get("hx-swap") == "none"
    assert "hx-target" not in restart_button


# Endpoint behavior -----------------------------------------------------------


def test_post_healthz_retry_calls_start_when_disconnected():
    client, adapter = make_client(ConnectionState.DISCONNECTED)

    response = client.post("/healthz/retry")

    assert response.status_code == 200
    assert adapter.start_calls == 1


def test_post_healthz_retry_calls_connect_when_start_absent():
    from app.main import create_app

    adapter = ConnectOnlyAdapter(state=ConnectionState.DISCONNECTED)
    client = TestClient(create_app(broker=adapter))

    response = client.post("/healthz/retry")

    assert response.status_code == 200
    assert adapter.connect_calls == 1
    assert "connected" in response.text


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
