"""SQLite-backed persistence layer.

Slice 2 surface: name_cache only. Future tables (fx_cache, name_overrides,
equity_snapshots, fills) arrive in later slices and extend the same Store.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite


NAME_CACHE_TTL_DAYS = 30
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _utcnow() -> datetime:
    """Wrapper so tests can monkeypatch time."""
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._db_path)
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def init_schema(self) -> None:
        conn = await self._connection()
        schema_sql = _SCHEMA_PATH.read_text()
        await conn.executescript(schema_sql)
        # Backfill price_magnifier column on existing DBs predating slice 3
        # follow-up. SQLite ALTER TABLE has no IF NOT EXISTS; check first.
        async with conn.execute("PRAGMA table_info(name_cache)") as cursor:
            cols = {row[1] async for row in cursor}
        if "price_magnifier" not in cols:
            await conn.execute(
                "ALTER TABLE name_cache ADD COLUMN price_magnifier INTEGER NOT NULL DEFAULT 1"
            )
        await conn.commit()

    async def put_name_cache(
        self,
        broker: str,
        native_key: str,
        canonical_symbol: str,
        name_en: str,
        price_magnifier: int = 1,
    ) -> None:
        conn = await self._connection()
        await conn.execute(
            """
            INSERT INTO name_cache(broker, native_key, canonical_symbol, name_en, price_magnifier, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(broker, native_key) DO UPDATE SET
                canonical_symbol = excluded.canonical_symbol,
                name_en          = excluded.name_en,
                price_magnifier  = excluded.price_magnifier,
                updated_at       = excluded.updated_at
            """,
            (broker, native_key, canonical_symbol, name_en, int(price_magnifier), _utcnow().isoformat()),
        )
        await conn.commit()

    async def get_name_cache(
        self,
        broker: str,
        native_key: str,
    ) -> tuple[str, str, int] | None:
        """Return (canonical_symbol, name_en, price_magnifier) or None."""
        conn = await self._connection()
        cutoff = (_utcnow() - timedelta(days=NAME_CACHE_TTL_DAYS)).isoformat()
        async with conn.execute(
            """
            SELECT canonical_symbol, name_en, price_magnifier
            FROM name_cache
            WHERE broker = ? AND native_key = ? AND updated_at >= ?
            """,
            (broker, native_key, cutoff),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return (row[0], row[1], int(row[2]) if row[2] is not None else 1)

    # ---- fx_cache (slice 3) -------------------------------------------------

    async def put_fx_rate(
        self,
        *,
        pair: str,
        rate: float,
        source: str,
        quoted_at: datetime,
    ) -> None:
        """Upsert the latest rate for `pair`. updated_at always advances to now()."""
        conn = await self._connection()
        await conn.execute(
            """
            INSERT INTO fx_cache(pair, rate, source, quoted_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pair) DO UPDATE SET
                rate       = excluded.rate,
                source     = excluded.source,
                quoted_at  = excluded.quoted_at,
                updated_at = excluded.updated_at
            """,
            (pair, rate, source, quoted_at.isoformat(), _utcnow().isoformat()),
        )
        await conn.commit()

    async def get_fx_rate(self, pair: str) -> dict | None:
        """Return the cached row for `pair`, or None if not present.

        Returned dict keys: pair, rate, source, quoted_at, updated_at.
        Timestamps are parsed back to aware datetime objects.
        """
        conn = await self._connection()
        async with conn.execute(
            """
            SELECT pair, rate, source, quoted_at, updated_at
            FROM fx_cache
            WHERE pair = ?
            """,
            (pair,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "pair": row[0],
            "rate": row[1],
            "source": row[2],
            "quoted_at": datetime.fromisoformat(row[3]),
            "updated_at": datetime.fromisoformat(row[4]),
        }

    # ---- fills (slice 11) --------------------------------------------------

    async def insert_fill(
        self,
        *,
        broker: str,
        account_id: str,
        execution_id: str,
        canonical_symbol: str,
        native_key: str,
        asset_class: str,
        side: str,
        quantity: float,
        price: float,
        currency: str,
        filled_at: datetime,
        fx_rate_at_fill: float | None = None,
        fees_native: float | None = None,
        fees_usd: float | None = None,
    ) -> bool:
        """INSERT OR IGNORE a fill row. Returns True if a new row was written,
        False if the (broker, execution_id) PK already existed.

        Idempotent by design — both the live `execDetailsEvent` stream and the
        EOD reqExecutions reconcile job can race to insert the same fill, and
        we silently keep the first one rather than raising.
        """
        conn = await self._connection()
        cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO fills(
                broker, account_id, execution_id, canonical_symbol, native_key,
                asset_class, side, quantity, price, currency,
                fx_rate_at_fill, fees_native, fees_usd, filled_at, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                broker, account_id, execution_id, canonical_symbol, native_key,
                asset_class, side, float(quantity), float(price), currency,
                fx_rate_at_fill, fees_native, fees_usd,
                filled_at.isoformat(), _utcnow().isoformat(),
            ),
        )
        await conn.commit()
        return cursor.rowcount > 0

    async def get_fills_since(
        self,
        *,
        broker: str,
        account_id: str,
        since: datetime,
    ) -> list[dict]:
        """Return fills for (broker, account_id) with filled_at >= since,
        oldest first. Empty list if none."""
        conn = await self._connection()
        async with conn.execute(
            """
            SELECT broker, account_id, execution_id, canonical_symbol, native_key,
                   asset_class, side, quantity, price, currency,
                   fx_rate_at_fill, fees_native, fees_usd, filled_at, captured_at
            FROM fills
            WHERE broker = ? AND account_id = ? AND filled_at >= ?
            ORDER BY filled_at ASC
            """,
            (broker, account_id, since.isoformat()),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "broker": r[0],
                "account_id": r[1],
                "execution_id": r[2],
                "canonical_symbol": r[3],
                "native_key": r[4],
                "asset_class": r[5],
                "side": r[6],
                "quantity": r[7],
                "price": r[8],
                "currency": r[9],
                "fx_rate_at_fill": r[10],
                "fees_native": r[11],
                "fees_usd": r[12],
                "filled_at": datetime.fromisoformat(r[13]),
                "captured_at": datetime.fromisoformat(r[14]),
            }
            for r in rows
        ]

    # ---- equity_snapshots (slice 10) ---------------------------------------

    async def insert_equity_snapshot(
        self,
        *,
        snapshot_at: datetime,
        snapshot_session: str,
        broker: str,
        account_id: str,
        base_currency: str,
        net_liquidation_native: float,
        net_liquidation_usd: float,
        gross_position_value_usd: float,
        cash_usd: float,
    ) -> bool:
        """INSERT OR IGNORE an equity snapshot row. Returns True if a new row
        was written, False on PK collision.

        Idempotent on (snapshot_at, snapshot_session, broker, account_id) so
        the scheduler and manual /admin/snapshot trigger can race without
        duplicating. snapshot_session disambiguates two exchange-close
        snapshots that resolve to the same UTC instant.
        """
        conn = await self._connection()
        cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO equity_snapshots(
                snapshot_at, snapshot_session, broker, account_id,
                base_currency, net_liquidation_native, net_liquidation_usd,
                gross_position_value_usd, cash_usd, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_at.isoformat(), snapshot_session, broker, account_id,
                base_currency,
                float(net_liquidation_native), float(net_liquidation_usd),
                float(gross_position_value_usd), float(cash_usd),
                _utcnow().isoformat(),
            ),
        )
        await conn.commit()
        return cursor.rowcount > 0

    async def get_equity_snapshots_since(
        self,
        *,
        broker: str,
        account_id: str,
        since: datetime,
    ) -> list[dict]:
        """Return equity snapshots for (broker, account_id) at or after
        `since`, oldest first."""
        conn = await self._connection()
        async with conn.execute(
            """
            SELECT snapshot_at, snapshot_session, broker, account_id,
                   base_currency, net_liquidation_native, net_liquidation_usd,
                   gross_position_value_usd, cash_usd, captured_at
            FROM equity_snapshots
            WHERE broker = ? AND account_id = ? AND snapshot_at >= ?
            ORDER BY snapshot_at ASC
            """,
            (broker, account_id, since.isoformat()),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "snapshot_at": datetime.fromisoformat(r[0]),
                "snapshot_session": r[1],
                "broker": r[2],
                "account_id": r[3],
                "base_currency": r[4],
                "net_liquidation_native": r[5],
                "net_liquidation_usd": r[6],
                "gross_position_value_usd": r[7],
                "cash_usd": r[8],
                "captured_at": datetime.fromisoformat(r[9]),
            }
            for r in rows
        ]

    async def get_equity_history(
        self,
        *,
        days: int,
        account_id: str | None = None,
    ) -> list[dict]:
        """Time series of net-liquidation USD over the last `days`, oldest
        first. Each element is `{"snapshot_at": datetime, "net_liquidation_usd": float}`.

        When `account_id` is None, sums NLV across all (broker, account_id)
        per (snapshot_at, snapshot_session) so the hero sparkline reflects
        the user's total exposure regardless of the active account filter.
        Multiple exchange-close sessions on the same UTC day stay as
        distinct points — gives sub-daily resolution to the curve at the
        cost of mild jitter when (e.g.) NYSE_CLOSE and HKEX_CLOSE land
        with materially different valuations.
        """
        since = _utcnow() - timedelta(days=days)
        conn = await self._connection()
        # ORDER BY includes snapshot_session so the rare case of two sessions
        # writing the same snapshot_at (e.g., MANUAL + NYSE_CLOSE racing to
        # the same UTC second) yields deterministic ordering rather than
        # undefined-order duplicate timestamps.
        if account_id is not None:
            sql = """
                SELECT snapshot_at, SUM(net_liquidation_usd)
                FROM equity_snapshots
                WHERE account_id = ? AND snapshot_at >= ?
                GROUP BY snapshot_at, snapshot_session
                ORDER BY snapshot_at ASC, snapshot_session ASC
            """
            params = (account_id, since.isoformat())
        else:
            sql = """
                SELECT snapshot_at, SUM(net_liquidation_usd)
                FROM equity_snapshots
                WHERE snapshot_at >= ?
                GROUP BY snapshot_at, snapshot_session
                ORDER BY snapshot_at ASC, snapshot_session ASC
            """
            params = (since.isoformat(),)
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            {"snapshot_at": datetime.fromisoformat(r[0]), "net_liquidation_usd": float(r[1])}
            for r in rows
        ]
