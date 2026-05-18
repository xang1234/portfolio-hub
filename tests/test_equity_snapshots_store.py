"""Slice 10 cycle 1: equity_snapshots table + Store CRUD.

Contract:
- `equity_snapshots` table is created by init_schema.
- PK is (snapshot_at, snapshot_session, broker, account_id) — so two
  exchanges resolving to the same UTC moment (e.g. midnight overlap)
  both retain their rows because snapshot_session disambiguates.
- `insert_equity_snapshot` uses INSERT OR IGNORE — duplicates silently
  dropped, returns True/False.
- `get_equity_snapshots_since(broker, account_id, since)` returns rows
  for that account newer than `since`, ordered by snapshot_at ASC.
"""

from datetime import datetime, timezone

import pytest


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store
    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _kwargs(**over):
    base = dict(
        snapshot_at=_utc("2026-05-17T16:00:00+00:00"),
        snapshot_session="NYSE_CLOSE",
        broker="IBKR",
        account_id="U7575980",
        base_currency="USD",
        net_liquidation_native=125_000.0,
        net_liquidation_usd=125_000.0,
        gross_position_value_usd=95_000.0,
        cash_usd=30_000.0,
    )
    base.update(over)
    return base


# Schema present -------------------------------------------------------------


async def test_equity_snapshots_table_exists_after_init(store):
    conn = await store._connection()
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='equity_snapshots'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "init_schema() did not create equity_snapshots"


# Round-trip -----------------------------------------------------------------


async def test_insert_and_read_back(store):
    inserted = await store.insert_equity_snapshot(**_kwargs())
    assert inserted is True

    rows = await store.get_equity_snapshots_since(
        broker="IBKR", account_id="U7575980",
        since=_utc("2026-05-01T00:00:00+00:00"),
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["snapshot_session"] == "NYSE_CLOSE"
    assert r["base_currency"] == "USD"
    assert r["net_liquidation_native"] == pytest.approx(125_000.0)
    assert r["net_liquidation_usd"] == pytest.approx(125_000.0)
    assert r["gross_position_value_usd"] == pytest.approx(95_000.0)
    assert r["cash_usd"] == pytest.approx(30_000.0)
    assert r["snapshot_at"].tzinfo is not None
    assert r["captured_at"].tzinfo is not None


# Idempotency ----------------------------------------------------------------


async def test_insert_same_pk_is_no_op(store):
    """Manual + scheduled trigger could race on the same key. INSERT OR
    IGNORE drops the second insert silently."""
    await store.insert_equity_snapshot(**_kwargs())
    second = await store.insert_equity_snapshot(**_kwargs())
    assert second is False
    rows = await store.get_equity_snapshots_since(
        broker="IBKR", account_id="U7575980",
        since=_utc("2026-05-01T00:00:00+00:00"),
    )
    assert len(rows) == 1


# Two exchanges, same UTC instant, distinct rows -----------------------------


async def test_two_sessions_at_same_instant_both_retained(store):
    """If HKEX_CLOSE and TSE_CLOSE resolve to the same UTC moment (rare
    but possible across DST transitions), both rows must survive. Including
    snapshot_session in the PK is what makes this safe."""
    at = _utc("2026-05-17T08:00:00+00:00")
    await store.insert_equity_snapshot(**_kwargs(snapshot_at=at, snapshot_session="HKEX_CLOSE"))
    await store.insert_equity_snapshot(**_kwargs(snapshot_at=at, snapshot_session="TSE_CLOSE"))

    rows = await store.get_equity_snapshots_since(
        broker="IBKR", account_id="U7575980",
        since=_utc("2026-05-01T00:00:00+00:00"),
    )
    sessions = {r["snapshot_session"] for r in rows}
    assert sessions == {"HKEX_CLOSE", "TSE_CLOSE"}


# Per-account scoping --------------------------------------------------------


async def test_filters_by_account(store):
    await store.insert_equity_snapshot(**_kwargs(account_id="U1"))
    await store.insert_equity_snapshot(**_kwargs(account_id="U2"))

    u1 = await store.get_equity_snapshots_since(
        broker="IBKR", account_id="U1",
        since=_utc("2026-05-01T00:00:00+00:00"),
    )
    assert len(u1) == 1
    assert u1[0]["account_id"] == "U1"


# Ordered by snapshot_at ASC -------------------------------------------------


async def test_ordered_by_snapshot_at_asc(store):
    await store.insert_equity_snapshot(**_kwargs(
        snapshot_at=_utc("2026-05-17T20:00:00+00:00"),
        snapshot_session="LSE_CLOSE",
    ))
    await store.insert_equity_snapshot(**_kwargs(
        snapshot_at=_utc("2026-05-17T08:00:00+00:00"),
        snapshot_session="HKEX_CLOSE",
    ))
    await store.insert_equity_snapshot(**_kwargs(
        snapshot_at=_utc("2026-05-17T16:00:00+00:00"),
        snapshot_session="NYSE_CLOSE",
    ))
    rows = await store.get_equity_snapshots_since(
        broker="IBKR", account_id="U7575980",
        since=_utc("2026-05-17T00:00:00+00:00"),
    )
    assert [r["snapshot_session"] for r in rows] == [
        "HKEX_CLOSE", "NYSE_CLOSE", "LSE_CLOSE",
    ]


# Migration: pre-existing DB without the table ------------------------------


async def test_migration_adds_table_to_pre_slice_10_db(tmp_path):
    """Same migration guard as the fills slice — booting against a
    pre-existing portfolio.db must add equity_snapshots without disturbing
    other data."""
    import aiosqlite
    from app.db.store import Store

    db_path = tmp_path / "pre.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript("""
            CREATE TABLE name_cache (
                broker TEXT NOT NULL, native_key TEXT NOT NULL,
                canonical_symbol TEXT NOT NULL, name_en TEXT NOT NULL,
                price_magnifier INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY (broker, native_key)
            );
        """)
        await conn.execute(
            "INSERT INTO name_cache VALUES "
            "('IBKR', '1', 'AAPL.US', 'APPLE INC', 1, '2026-01-01')"
        )
        await conn.commit()

    s = Store(db_path)
    await s.init_schema()
    try:
        conn = await s._connection()
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='equity_snapshots'"
        ) as cur:
            assert await cur.fetchone() is not None
        async with conn.execute(
            "SELECT name_en FROM name_cache WHERE native_key='1'"
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == "APPLE INC"
    finally:
        await s.close()
