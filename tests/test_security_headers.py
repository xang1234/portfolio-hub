"""Security-hardening coverage: CSP header on every response, and the
opt-in Host-header allow-list (DNS-rebinding defense)."""

from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState


@dataclass
class _FakeAdapter:
    name: str = "IBKR"
    _state: ConnectionState = ConnectionState.CONNECTED

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return self._state
    async def get_positions(self): return []
    async def get_account_summary(self): return []


def _client():
    from app.main import create_app
    return TestClient(create_app(broker=_FakeAdapter()))


def test_csp_header_present_and_locks_down_origins():
    resp = _client().get("/healthz")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    # No third-party script origin; same-origin only.
    assert "script-src 'self'" in csp
    # Anti-exfiltration: SSE/fetch can only talk back to this origin.
    assert "connect-src 'self'" in csp
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


def test_allowed_hosts_unset_accepts_any_host(monkeypatch):
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    # TestClient sends Host: testserver — with no allow-list it must pass.
    assert _client().get("/healthz").status_code == 200


def test_allowed_hosts_rejects_unlisted_host(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "portfolio-hub.example.ts.net,localhost")
    client = _client()
    # Default Host "testserver" is not in the allow-list → 400.
    assert client.get("/healthz").status_code == 400
    # A listed host is accepted.
    ok = client.get("/healthz", headers={"Host": "localhost"})
    assert ok.status_code == 200
