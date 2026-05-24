"""GET /api/equity-history serves the hero sparkline.

Returns [{t: iso, v: usd}] for the most recent `?days=` window. Aggregates
across accounts when `?account=` is missing or "All" so the sparkline
reflects total exposure even when the holdings filter is narrowed to a
single account. When the app has no Store attached (tests that skip
lifespan), returns [] so the client-side sparkline silently no-ops
instead of throwing a 503.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.broker import ConnectionState


class _Fake:
    name = "IBKR"
    async def connect(self): pass
    async def disconnect(self): pass
    async def is_connected(self): return True
    async def get_connection_state(self): return ConnectionState.CONNECTED
    async def get_positions(self): return []
    async def get_account_summary(self): return []


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store
    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


async def _seed(store, *, when, account, nlv, session="NYSE_CLOSE", broker="IBKR"):
    await store.insert_equity_snapshot(
        snapshot_at=when, snapshot_session=session, broker=broker,
        account_id=account, base_currency="USD",
        net_liquidation_native=nlv, net_liquidation_usd=nlv,
        gross_position_value_usd=nlv * 0.9, cash_usd=nlv * 0.1,
    )


def _client_with_store(store):
    from app.main import create_app
    app = create_app(broker=_Fake())
    app.state.store = store  # bypass lifespan; tests own the store lifecycle
    return TestClient(app)


# --------------------------------------------------------------------------


def test_returns_empty_array_when_no_store_attached():
    """create_app with broker=fake skips lifespan, so app.state.store is None.
    The route must degrade to [] rather than 503 so the spark fetch no-ops."""
    from app.main import create_app
    app = create_app(broker=_Fake())
    # Ensure no store leaks in from a previous test
    if hasattr(app.state, "store"):
        delattr(app.state, "store")
    client = TestClient(app)
    r = client.get("/api/equity-history")
    assert r.status_code == 200
    assert r.json() == []


async def test_returns_empty_array_when_no_snapshots(store):
    client = _client_with_store(store)
    r = client.get("/api/equity-history")
    assert r.status_code == 200
    assert r.json() == []


async def test_returns_recent_snapshots_for_specific_account(store):
    now = datetime.now(timezone.utc)
    await _seed(store, when=now - timedelta(days=3), account="U1", nlv=100_000)
    await _seed(store, when=now - timedelta(days=2), account="U1", nlv=102_500)
    await _seed(store, when=now - timedelta(days=1), account="U1", nlv=105_000)

    client = _client_with_store(store)
    r = client.get("/api/equity-history?account=U1&days=10")

    assert r.status_code == 200
    payload = r.json()
    assert len(payload) == 3
    assert [p["v"] for p in payload] == [100_000, 102_500, 105_000]
    # oldest-first ordering
    assert payload[0]["t"] < payload[-1]["t"]


async def test_all_accounts_aggregates_per_snapshot(store):
    now = datetime.now(timezone.utc)
    when = now - timedelta(hours=2)
    # Same snapshot_at, two accounts → one summed point.
    await _seed(store, when=when, account="U1", nlv=100_000)
    await _seed(store, when=when, account="U2", nlv=50_000)

    client = _client_with_store(store)
    r = client.get("/api/equity-history")  # no ?account=

    payload = r.json()
    assert len(payload) == 1
    assert payload[0]["v"] == 150_000.0


async def test_account_filter_excludes_other_accounts(store):
    now = datetime.now(timezone.utc)
    await _seed(store, when=now - timedelta(days=1), account="U1", nlv=100_000)
    await _seed(store, when=now - timedelta(days=1), account="U2", nlv=50_000)

    client = _client_with_store(store)
    r = client.get("/api/equity-history?account=U1")

    payload = r.json()
    assert len(payload) == 1
    assert payload[0]["v"] == 100_000.0  # U2 is excluded


async def test_days_window_excludes_older_snapshots(store):
    now = datetime.now(timezone.utc)
    # Way outside the 7-day window
    await _seed(store, when=now - timedelta(days=30), account="U1", nlv=80_000)
    # Inside
    await _seed(store, when=now - timedelta(days=2), account="U1", nlv=100_000)

    client = _client_with_store(store)
    r = client.get("/api/equity-history?account=U1&days=7")

    payload = r.json()
    assert [p["v"] for p in payload] == [100_000.0]


async def test_days_param_is_clamped(store):
    """Negative or absurd ?days= shouldn't crash — clamp to [1, 400]."""
    client = _client_with_store(store)
    # Negative → clamped to 1 (no snapshots in last 1 day → [])
    r1 = client.get("/api/equity-history?days=-99")
    assert r1.status_code == 200
    assert r1.json() == []
    # Huge → clamped to 400 (still no snapshots → [])
    r2 = client.get("/api/equity-history?days=999999")
    assert r2.status_code == 200
    assert r2.json() == []


async def test_payload_shape_is_compact(store):
    """{t, v} only — the sparkline doesn't need broker/session/etc., and
    a slimmer payload means the hero render stays snappy under poor net."""
    now = datetime.now(timezone.utc)
    await _seed(store, when=now - timedelta(days=1), account="U1", nlv=100_000)

    client = _client_with_store(store)
    payload = client.get("/api/equity-history").json()

    assert payload[0].keys() == {"t", "v"}


async def test_account_all_explicit_is_equivalent_to_omitted(store):
    now = datetime.now(timezone.utc)
    await _seed(store, when=now - timedelta(hours=1), account="U1", nlv=100_000)
    await _seed(store, when=now - timedelta(hours=1), account="U2", nlv=50_000)

    client = _client_with_store(store)
    explicit = client.get("/api/equity-history?account=All").json()
    omitted = client.get("/api/equity-history").json()
    assert explicit == omitted
