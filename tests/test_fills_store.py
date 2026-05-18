"""Slice 11 cycle 1: fills table + Store CRUD.

Contract:
- `fills` table is created by init_schema (extends existing schema.sql).
- PK is (broker, execution_id) so re-inserting the same execution is a
  no-op (idempotency for the EOD reconcile job).
- `insert_fill` uses INSERT OR IGNORE — duplicates silently dropped, no
  exception, returns whether the row was inserted (True/False).
- `get_fills_since(account_id, since)` returns rows for the account
  newer than `since`, ordered by filled_at ASC.
"""

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
async def store(tmp_path):
    from app.db.store import Store
    s = Store(tmp_path / "test.db")
    await s.init_schema()
    yield s
    await s.close()


def _utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)


def _fill_kwargs(**over):
    base = dict(
        broker="IBKR",
        account_id="U7575980",
        execution_id="exec-1",
        canonical_symbol="700.HK",
        native_key="76792991",
        asset_class="STK",
        side="BUY",
        quantity=100.0,
        price=420.0,
        currency="HKD",
        fx_rate_at_fill=0.1283,
        fees_native=15.0,
        fees_usd=1.92,
        filled_at=_utc("2026-05-15T09:30:12+00:00"),
    )
    base.update(over)
    return base


# Schema present -------------------------------------------------------------


async def test_fills_table_exists_after_init(store):
    """init_schema must create the fills table."""
    conn = await store._connection()
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fills'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "init_schema() did not create the fills table"


# Insert + read --------------------------------------------------------------


async def test_insert_fill_round_trips(store):
    inserted = await store.insert_fill(**_fill_kwargs())
    assert inserted is True, "first insert should return True (row written)"

    rows = await store.get_fills_since(
        broker="IBKR", account_id="U7575980", since=_utc("2026-05-15T00:00:00+00:00"),
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["execution_id"] == "exec-1"
    assert r["canonical_symbol"] == "700.HK"
    assert r["side"] == "BUY"
    assert r["quantity"] == 100.0
    assert r["price"] == 420.0
    assert r["currency"] == "HKD"
    assert r["fx_rate_at_fill"] == pytest.approx(0.1283)
    assert r["fees_native"] == 15.0
    assert r["fees_usd"] == pytest.approx(1.92)
    assert r["filled_at"].tzinfo is not None


# Idempotency on PK ----------------------------------------------------------


async def test_insert_same_execution_id_is_idempotent(store):
    """The EOD reconciliation must be safe to re-run — same execution_id
    inserted twice should leave one row, not two, and not raise."""
    await store.insert_fill(**_fill_kwargs())
    second = await store.insert_fill(**_fill_kwargs())
    assert second is False, "second insert of same PK should return False"

    rows = await store.get_fills_since(
        broker="IBKR", account_id="U7575980", since=_utc("2026-05-15T00:00:00+00:00"),
    )
    assert len(rows) == 1


async def test_different_broker_same_execution_id_is_distinct(store):
    """execution_id is unique within a broker; another broker reporting the
    same string is a separate row."""
    await store.insert_fill(**_fill_kwargs(broker="IBKR"))
    await store.insert_fill(**_fill_kwargs(broker="Futu"))

    ibkr = await store.get_fills_since(
        broker="IBKR", account_id="U7575980", since=_utc("2026-05-15T00:00:00+00:00"),
    )
    futu = await store.get_fills_since(
        broker="Futu", account_id="U7575980", since=_utc("2026-05-15T00:00:00+00:00"),
    )
    assert len(ibkr) == 1
    assert len(futu) == 1


# Filtering ------------------------------------------------------------------


async def test_get_fills_filters_by_account(store):
    await store.insert_fill(**_fill_kwargs(account_id="U1", execution_id="e1"))
    await store.insert_fill(**_fill_kwargs(account_id="U2", execution_id="e2"))

    u1 = await store.get_fills_since(
        broker="IBKR", account_id="U1", since=_utc("2026-05-15T00:00:00+00:00"),
    )
    assert len(u1) == 1
    assert u1[0]["account_id"] == "U1"


async def test_get_fills_since_filters_by_timestamp(store):
    await store.insert_fill(**_fill_kwargs(
        execution_id="old", filled_at=_utc("2026-05-10T10:00:00+00:00"),
    ))
    await store.insert_fill(**_fill_kwargs(
        execution_id="new", filled_at=_utc("2026-05-15T10:00:00+00:00"),
    ))

    rows = await store.get_fills_since(
        broker="IBKR", account_id="U7575980",
        since=_utc("2026-05-12T00:00:00+00:00"),
    )
    assert [r["execution_id"] for r in rows] == ["new"]


async def test_get_fills_ordered_by_filled_at_asc(store):
    await store.insert_fill(**_fill_kwargs(
        execution_id="b", filled_at=_utc("2026-05-15T11:00:00+00:00"),
    ))
    await store.insert_fill(**_fill_kwargs(
        execution_id="a", filled_at=_utc("2026-05-15T09:00:00+00:00"),
    ))
    await store.insert_fill(**_fill_kwargs(
        execution_id="c", filled_at=_utc("2026-05-15T12:00:00+00:00"),
    ))

    rows = await store.get_fills_since(
        broker="IBKR", account_id="U7575980", since=_utc("2026-05-15T00:00:00+00:00"),
    )
    assert [r["execution_id"] for r in rows] == ["a", "b", "c"]


# NULL fx_rate_at_fill is allowed (USD trades don't need it) -----------------


async def test_usd_trade_with_null_fx_rate(store):
    await store.insert_fill(**_fill_kwargs(
        execution_id="usd-1", currency="USD", fx_rate_at_fill=None,
    ))
    rows = await store.get_fills_since(
        broker="IBKR", account_id="U7575980", since=_utc("2026-05-15T00:00:00+00:00"),
    )
    assert rows[0]["fx_rate_at_fill"] is None
