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
        await conn.commit()

    async def put_name_cache(
        self,
        broker: str,
        native_key: str,
        canonical_symbol: str,
        name_en: str,
    ) -> None:
        conn = await self._connection()
        await conn.execute(
            """
            INSERT INTO name_cache(broker, native_key, canonical_symbol, name_en, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(broker, native_key) DO UPDATE SET
                canonical_symbol = excluded.canonical_symbol,
                name_en          = excluded.name_en,
                updated_at       = excluded.updated_at
            """,
            (broker, native_key, canonical_symbol, name_en, _utcnow().isoformat()),
        )
        await conn.commit()

    async def get_name_cache(
        self,
        broker: str,
        native_key: str,
    ) -> tuple[str, str] | None:
        conn = await self._connection()
        cutoff = (_utcnow() - timedelta(days=NAME_CACHE_TTL_DAYS)).isoformat()
        async with conn.execute(
            """
            SELECT canonical_symbol, name_en
            FROM name_cache
            WHERE broker = ? AND native_key = ? AND updated_at >= ?
            """,
            (broker, native_key, cutoff),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return (row[0], row[1])

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
