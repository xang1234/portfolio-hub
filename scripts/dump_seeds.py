#!/usr/bin/env python3
"""Slice 12 HITL — dump recent rows from the seed tables.

A read-only helper for the HITL gate. Connects to the SQLite store and
prints recent rows from `equity_snapshots`, `fills`, or live account
summaries (via the broker), so the operator can compare against IB TWS
during verification.

USAGE
-----
    scripts/dump_seeds.py --table equity_snapshots          # default --since 7d
    scripts/dump_seeds.py --table equity_snapshots --since 24h
    scripts/dump_seeds.py --table fills --since 1h
    scripts/dump_seeds.py --table account-summary           # live, not stored

ENVIRONMENT
-----------
    DATA_DIR   — directory containing portfolio.db (default ./data)
    IB_HOST    — only used for --table account-summary (default ib-gateway)
    IB_PORT    — same (default 4003)
"""

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _parse_since(text: str) -> timedelta:
    """Tiny parser for '24h' / '7d' / '30m' style durations."""
    m = re.fullmatch(r"(\d+)([hdm])", text.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(
            f"--since must look like '24h' / '7d' / '30m', got {text!r}"
        )
    n, unit = int(m.group(1)), m.group(2)
    return {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]


def _format_row(row: dict, columns: list[str]) -> str:
    return " | ".join(str(row.get(c, "")) for c in columns)


async def _dump_equity_snapshots(store, since: datetime) -> None:
    # Intentionally reaches past store.get_equity_snapshots_since (which is
    # per-account) — the HITL helper wants "every account since N". If Store
    # grows a public cross-account query, swap this for that and drop the
    # _connection() back-door.
    conn = await store._connection()
    async with conn.execute(
        """
        SELECT snapshot_at, snapshot_session, broker, account_id, base_currency,
               net_liquidation_native, net_liquidation_usd,
               gross_position_value_usd, cash_usd, captured_at
        FROM equity_snapshots
        WHERE snapshot_at >= ?
        ORDER BY snapshot_at DESC
        LIMIT 500
        """,
        (since.isoformat(),),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        print(f"no equity_snapshots rows since {since.isoformat()}")
        return
    print(f"{len(rows)} equity_snapshots row(s) since {since.isoformat()}:\n")
    print("snapshot_at | session | account | base | NLV native | NLV USD | GPV USD | cash USD")
    print("-" * 100)
    for r in rows:
        print(f"{r[0]} | {r[1]} | {r[3]} | {r[4]} | {r[5]:,.2f} | "
              f"{r[6]:,.2f} | {r[7]:,.2f} | {r[8]:,.2f}")


async def _dump_fills(store, since: datetime) -> None:
    # Same back-door rationale as _dump_equity_snapshots above.
    conn = await store._connection()
    async with conn.execute(
        """
        SELECT filled_at, broker, account_id, canonical_symbol, side,
               quantity, price, currency, fx_rate_at_fill, fees_native, fees_usd,
               execution_id
        FROM fills
        WHERE filled_at >= ?
        ORDER BY filled_at DESC
        LIMIT 500
        """,
        (since.isoformat(),),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        print(f"no fills rows since {since.isoformat()}")
        return
    print(f"{len(rows)} fills row(s) since {since.isoformat()}:\n")
    print("filled_at | account | symbol | side | qty | price | ccy | fx | fees_native | fees_usd | exec_id")
    print("-" * 120)
    for r in rows:
        fx = f"{r[8]:.4f}" if r[8] is not None else "-"
        fees_usd = f"{r[10]:.2f}" if r[10] is not None else "-"
        print(f"{r[0]} | {r[2]} | {r[3]} | {r[4]} | {r[5]:g} | {r[6]:g} | "
              f"{r[7]} | {fx} | {r[9]:g} | {fees_usd} | {r[11]}")


async def _dump_account_summary() -> None:
    """Live call to the broker — useful for spot-checking NLV against TWS."""
    try:
        from ib_async import IB
    except ImportError:
        print("ib_async not installed; --table account-summary needs ib_async", file=sys.stderr)
        sys.exit(2)

    from app.adapters.ibkr import IbkrAdapter

    import random

    host = os.environ.get("IB_HOST", "ib-gateway")
    port = int(os.environ.get("IB_PORT", "4003"))
    # Randomize clientId so re-runs (or parallel runs of this script and
    # the verify_read_only_api.py script) don't collide on a single slot
    # and inadvertently kick the dashboard's connection (clientId=1).
    client_id = int(os.environ.get("IB_CLIENT_ID") or random.randint(50, 90))

    adapter = IbkrAdapter(host=host, port=port, client_id=client_id)
    try:
        await adapter.connect()
    except Exception as exc:
        print(f"could not connect to gateway at {host}:{port}: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        summaries = await adapter.get_account_summary()
        if not summaries:
            print("no account summaries returned")
            return
        print(f"{len(summaries)} account summary row(s):\n")
        print("account | base | NLV native | NLV USD | GPV USD | cash USD | buying power USD")
        print("-" * 100)
        for s in summaries:
            print(f"{s.account_id} | {s.base_currency} | "
                  f"{s.net_liquidation_native:,.2f} | {s.net_liquidation_usd:,.2f} | "
                  f"{s.gross_position_value_usd:,.2f} | {s.cash_usd:,.2f} | "
                  f"{s.buying_power_usd:,.2f}")
    finally:
        await adapter.disconnect()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Dump seed-table rows for HITL verification.")
    parser.add_argument(
        "--table",
        choices=["equity_snapshots", "fills", "account-summary"],
        required=True,
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=_parse_since("7d"),
        help="Look-back window (e.g. 24h, 7d, 30m). Default 7d. Ignored for account-summary.",
    )
    args = parser.parse_args()

    if args.table == "account-summary":
        await _dump_account_summary()
        return 0

    from app.db.store import Store
    db_path = Path(os.environ.get("DATA_DIR", "./data")) / "portfolio.db"
    if not db_path.exists():
        print(f"no DB found at {db_path}; set DATA_DIR or run from the repo root",
              file=sys.stderr)
        return 2

    store = Store(db_path)
    try:
        since_dt = datetime.now(timezone.utc) - args.since
        if args.table == "equity_snapshots":
            await _dump_equity_snapshots(store, since_dt)
        else:
            await _dump_fills(store, since_dt)
    finally:
        await store.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
