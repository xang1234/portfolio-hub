"""Slice 11 cycle 5: POST /admin/reconcile-fills.

A manual trigger for operators (and for testing the EOD reconcile path
without having to wait until 23:00). Returns the count of new fills
inserted as JSON. GET is rejected — this is a state-mutating action.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.core.broker import ConnectionState


@dataclass
class FakeContract:
    conId: int = 265598
    symbol: str = "AAPL"
    secType: str = "STK"
    currency: str = "USD"
    exchange: str = "SMART"
    primaryExchange: str = "NASDAQ"


@dataclass
class FakeExecution:
    execId: str = "e-1"
    acctNumber: str = "U1"
    side: str = "BOT"
    shares: float = 10.0
    price: float = 180.0
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc))


@dataclass
class FakeCommissionReport:
    execId: str = "e-1"
    commission: float = 1.0
    currency: str = "USD"


@dataclass
class FakeFill:
    contract: FakeContract = field(default_factory=FakeContract)
    execution: FakeExecution = field(default_factory=FakeExecution)
    commissionReport: FakeCommissionReport = field(default_factory=FakeCommissionReport)
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc))


class FakeAdapter:
    name = "IBKR"

    def __init__(self, fills):
        self._fills = fills
        self._fx_service = None

    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return []
    async def get_account_summary(self): return []
    async def _req_executions(self): return list(self._fills)


def _client(adapter, store):
    from app.main import create_app
    app = create_app(broker=adapter)
    # Inject the store via app.state so the endpoint can find it.
    app.state.store = store
    return TestClient(app)


# Happy path -----------------------------------------------------------------


def test_post_reconcile_returns_inserted_count(tmp_path):
    import asyncio
    from app.db.store import Store

    async def _setup():
        s = Store(tmp_path / "test.db")
        await s.init_schema()
        return s
    store = asyncio.run(_setup())

    adapter = FakeAdapter([
        FakeFill(execution=FakeExecution(execId="e-1")),
        FakeFill(execution=FakeExecution(execId="e-2")),
    ])
    response = _client(adapter, store).post("/admin/reconcile-fills")
    assert response.status_code == 200
    assert response.json() == {"inserted": 2}

    # Running it again should report 0 (idempotent).
    response2 = _client(adapter, store).post("/admin/reconcile-fills")
    assert response2.json() == {"inserted": 0}

    asyncio.run(store.close())


# GET is rejected ------------------------------------------------------------


def test_get_admin_reconcile_returns_405(tmp_path):
    import asyncio
    from app.db.store import Store

    async def _setup():
        s = Store(tmp_path / "test.db")
        await s.init_schema()
        return s
    store = asyncio.run(_setup())

    adapter = FakeAdapter([])
    response = _client(adapter, store).get("/admin/reconcile-fills")
    assert response.status_code == 405  # Method Not Allowed
    asyncio.run(store.close())


# No store wired returns a clear error --------------------------------------


def test_post_reconcile_without_store_returns_503(tmp_path):
    from app.main import create_app
    adapter = FakeAdapter([])
    app = create_app(broker=adapter)
    # Deliberately don't attach app.state.store
    client = TestClient(app)
    response = client.post("/admin/reconcile-fills")
    assert response.status_code == 503
    body = response.json()
    assert "store" in body.get("detail", "").lower()


# Shared-secret auth ----------------------------------------------------------


def test_admin_endpoint_rejects_missing_token_when_configured(tmp_path, monkeypatch):
    """When ADMIN_TOKEN is set in the environment, callers must provide
    the matching X-Admin-Token header. Missing → 401."""
    import asyncio
    from app.db.store import Store

    async def _setup():
        s = Store(tmp_path / "test.db"); await s.init_schema(); return s
    store = asyncio.run(_setup())

    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    adapter = FakeAdapter([])
    response = _client(adapter, store).post("/admin/reconcile-fills")
    assert response.status_code == 401
    asyncio.run(store.close())


def test_admin_endpoint_rejects_wrong_token(tmp_path, monkeypatch):
    import asyncio
    from app.db.store import Store

    async def _setup():
        s = Store(tmp_path / "test.db"); await s.init_schema(); return s
    store = asyncio.run(_setup())

    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    adapter = FakeAdapter([])
    response = _client(adapter, store).post(
        "/admin/reconcile-fills",
        headers={"X-Admin-Token": "wrong"},
    )
    assert response.status_code == 401
    asyncio.run(store.close())


def test_admin_endpoint_accepts_correct_token(tmp_path, monkeypatch):
    import asyncio
    from app.db.store import Store

    async def _setup():
        s = Store(tmp_path / "test.db"); await s.init_schema(); return s
    store = asyncio.run(_setup())

    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    adapter = FakeAdapter([])
    response = _client(adapter, store).post(
        "/admin/reconcile-fills",
        headers={"X-Admin-Token": "s3cret"},
    )
    assert response.status_code == 200
    asyncio.run(store.close())


def test_admin_endpoint_open_when_no_token_env_var(tmp_path, monkeypatch):
    """When ADMIN_TOKEN is unset (default for dev / tests), no auth is
    enforced — preserves backward compatibility for non-production setups.
    Operators opting into auth set the env var; Tailscale is the network
    boundary otherwise."""
    import asyncio
    from app.db.store import Store

    async def _setup():
        s = Store(tmp_path / "test.db"); await s.init_schema(); return s
    store = asyncio.run(_setup())

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    adapter = FakeAdapter([])
    response = _client(adapter, store).post("/admin/reconcile-fills")
    assert response.status_code == 200
    asyncio.run(store.close())
