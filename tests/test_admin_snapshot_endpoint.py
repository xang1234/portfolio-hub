"""Slice 10 cycle 6: POST /admin/snapshot?session=MANUAL.

Manual trigger for operators verifying the snapshot capture path. Inserts
ONE row per linked (broker, account_id), tagged with the supplied session
(or 'MANUAL' by default). Reuses the same shared-secret X-Admin-Token
auth as /admin/reconcile-fills.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.core.broker import AccountSummary, ConnectionState


class _FakeAdapter:
    name = "IBKR"
    def __init__(self, summaries):
        self._summaries = summaries
    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return []
    async def get_account_summary(self): return list(self._summaries)


def _summary(account="U1"):
    return AccountSummary(
        broker="IBKR", account_id=account, base_currency="USD",
        net_liquidation_usd=125_000.0, cash_usd=30_000.0, buying_power_usd=200_000.0,
        net_liquidation_native=125_000.0, gross_position_value_usd=95_000.0,
    )


def _client(adapter, store):
    from app.main import create_app
    app = create_app(broker=adapter)
    app.state.store = store
    return TestClient(app)


# Happy path -----------------------------------------------------------------


def test_post_snapshot_returns_inserted_count(tmp_path, monkeypatch):
    import asyncio
    from app.db.store import Store

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")

    async def _setup():
        s = Store(tmp_path / "test.db"); await s.init_schema(); return s
    store = asyncio.run(_setup())

    adapter = _FakeAdapter([_summary("U1"), _summary("U2")])
    response = _client(adapter, store).post("/admin/snapshot")
    assert response.status_code == 200
    body = response.json()
    assert body["inserted"] == 2

    # Each MANUAL click stamps snapshot_at=now() so subsequent calls write
    # fresh rows (intentional: an operator clicking twice records two
    # distinct moments). Scheduler-driven inserts collide because they
    # supply the exchange-close moment.
    response2 = _client(adapter, store).post("/admin/snapshot")
    assert response2.json()["inserted"] == 2
    asyncio.run(store.close())


def test_custom_session_tag(tmp_path, monkeypatch):
    """?session=foo overrides the default 'MANUAL' tag."""
    import asyncio
    from app.db.store import Store

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")

    async def _setup():
        s = Store(tmp_path / "test.db"); await s.init_schema(); return s
    store = asyncio.run(_setup())

    adapter = _FakeAdapter([_summary("U1")])
    response = _client(adapter, store).post("/admin/snapshot?session=BACKFILL_2026")
    assert response.status_code == 200

    rows = asyncio.run(store.get_equity_snapshots_since(
        broker="IBKR", account_id="U1",
        since=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))
    assert len(rows) == 1
    assert rows[0]["snapshot_session"] == "BACKFILL_2026"
    asyncio.run(store.close())


# HTTP shape -----------------------------------------------------------------


def test_get_admin_snapshot_returns_405(tmp_path):
    import asyncio
    from app.db.store import Store
    async def _setup():
        s = Store(tmp_path / "test.db"); await s.init_schema(); return s
    store = asyncio.run(_setup())
    response = _client(_FakeAdapter([]), store).get("/admin/snapshot")
    assert response.status_code == 405
    asyncio.run(store.close())


def test_post_snapshot_without_store_returns_503(tmp_path, monkeypatch):
    from app.main import create_app
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    app = create_app(broker=_FakeAdapter([]))
    client = TestClient(app)
    response = client.post("/admin/snapshot")
    assert response.status_code == 503


# Auth -----------------------------------------------------------------------


def test_snapshot_endpoint_rejects_missing_token_when_configured(tmp_path, monkeypatch):
    """Same shared-secret auth as /admin/reconcile-fills."""
    import asyncio
    from app.db.store import Store
    async def _setup():
        s = Store(tmp_path / "test.db"); await s.init_schema(); return s
    store = asyncio.run(_setup())

    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    response = _client(_FakeAdapter([_summary()]), store).post("/admin/snapshot")
    assert response.status_code == 401
    asyncio.run(store.close())


def test_snapshot_endpoint_accepts_correct_token(tmp_path, monkeypatch):
    import asyncio
    from app.db.store import Store
    async def _setup():
        s = Store(tmp_path / "test.db"); await s.init_schema(); return s
    store = asyncio.run(_setup())

    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    response = _client(_FakeAdapter([_summary()]), store).post(
        "/admin/snapshot",
        headers={"X-Admin-Token": "s3cret"},
    )
    assert response.status_code == 200
    asyncio.run(store.close())
