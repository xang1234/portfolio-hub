#!/usr/bin/env python3
"""Run the dashboard locally with a realistic mock portfolio.

Purpose: produce screenshots and demos without touching a real broker
account. Wires two fake adapters (IBKR + Futu) behind CompositeBroker
so the UI shows multi-broker chips, multi-currency rows, intraday P&L,
and the closures-this-week strip — all from in-memory fixtures.

Usage:
    BROKERS_ENABLED=ibkr,futu .venv/bin/python scripts/serve_mock.py
        # listens on http://127.0.0.1:8765 by default

    PORT=9000 .venv/bin/python scripts/serve_mock.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.core.broker import AccountSummary, ConnectionState, Position  # noqa: E402
from app.core.composite_broker import CompositeBroker  # noqa: E402
from app.core.live_positions import LivePositions  # noqa: E402


class _MockAdapter:
    """Minimal Broker implementation backed by a fixed positions list."""

    def __init__(self, name: str, positions: list[Position], account_summaries: list[AccountSummary] | None = None) -> None:
        self.name = name
        self._positions = positions
        self._summaries = account_summaries or []

    async def connect(self) -> None: pass
    async def disconnect(self) -> None: pass
    async def start(self) -> None: pass
    async def is_connected(self) -> bool: return True
    async def get_connection_state(self) -> ConnectionState: return ConnectionState.CONNECTED
    async def get_positions(self) -> list[Position]: return list(self._positions)
    async def get_account_summary(self) -> list[AccountSummary]: return list(self._summaries)


# ---------------------------------------------------------------------------
# Mock portfolio — all numbers are illustrative. No real holdings.
# Mix of US/HK/JP/EU/AU equities and cash balances across two brokers,
# with realistic prices, P&L, intraday change, and a stale FX badge so
# the UI surfaces every state worth seeing in a screenshot.
# ---------------------------------------------------------------------------


def _stk(
    broker: str, account: str, native: str, name: str, exch: str, ccy: str,
    qty: float, avg: float, last: float, prev: float, fx_to_usd: float = 1.0,
    canonical: str | None = None, native_key: str | None = None,
) -> Position:
    mv_native = qty * last
    mv_usd = mv_native * fx_to_usd
    pnl_native = (last - avg) * qty
    pnl_usd = pnl_native * fx_to_usd
    return Position(
        broker=broker, account_id=account,
        native_key=native_key or f"{broker}-{native}",
        canonical_symbol=canonical or f"{native}.US",
        native_symbol=native, exchange=exch, currency=ccy,
        name_en=name, asset_class="STK",
        quantity=qty, avg_cost=avg, last_price=last,
        market_value_native=mv_native, market_value_usd=mv_usd,
        unrealized_pnl_native=pnl_native, unrealized_pnl_usd=pnl_usd,
        previous_close=prev,
    )


def _cash(broker: str, account: str, ccy: str, amount: float, fx_to_usd: float) -> Position:
    return Position(
        broker=broker, account_id=account,
        native_key=f"{broker}-CASH-{ccy}",
        canonical_symbol=f"CASH.{ccy}", native_symbol=ccy, exchange="",
        currency=ccy, name_en=f"{ccy} cash", asset_class="CASH",
        quantity=amount, avg_cost=1.0, last_price=1.0,
        market_value_native=amount, market_value_usd=amount * fx_to_usd,
        unrealized_pnl_native=0.0, unrealized_pnl_usd=0.0,
    )


IBKR_POSITIONS: list[Position] = [
    # US tech — large position, modest intraday gain
    _stk("IBKR", "DEMO-IBKR", "NVDA", "NVIDIA Corp", "NASDAQ", "USD",
         qty=180, avg=420.00, last=512.40, prev=508.20),
    _stk("IBKR", "DEMO-IBKR", "AAPL", "Apple Inc", "NASDAQ", "USD",
         qty=250, avg=152.30, last=189.75, prev=191.20),
    _stk("IBKR", "DEMO-IBKR", "MSFT", "Microsoft Corp", "NASDAQ", "USD",
         qty=120, avg=295.60, last=412.85, prev=410.10),
    # Japan — Toyota on TSE, JPY → USD
    _stk("IBKR", "DEMO-IBKR", "7203", "Toyota Motor Corp", "TSEJ", "JPY",
         qty=400, avg=2100.0, last=2685.0, prev=2702.0, fx_to_usd=0.0064,
         canonical="7203.JP"),
    # UK — pence-quoted (price_magnifier visible on real IBKR)
    _stk("IBKR", "DEMO-IBKR", "BP", "BP plc", "LSE", "GBP",
         qty=600, avg=4.80, last=5.42, prev=5.37, fx_to_usd=1.27,
         canonical="BP.UK"),
    # Cash
    _cash("IBKR", "DEMO-IBKR", "USD", 12_450.00, fx_to_usd=1.0),
    _cash("IBKR", "DEMO-IBKR", "JPY", 285_000.0, fx_to_usd=0.0064),
]

FUTU_POSITIONS: list[Position] = [
    # HK — Tencent (Stock Connect / native HK)
    _stk("Futu", "DEMO-FUTU", "700", "Tencent Holdings Ltd", "SEHK", "HKD",
         qty=400, avg=305.50, last=398.40, prev=395.80, fx_to_usd=0.1282,
         canonical="700.HK"),
    # HK — HSBC
    _stk("Futu", "DEMO-FUTU", "5", "HSBC Holdings plc", "SEHK", "HKD",
         qty=800, avg=58.20, last=72.15, prev=71.90, fx_to_usd=0.1282,
         canonical="5.HK"),
    # US dual-listed for the same Futu account
    _stk("Futu", "DEMO-FUTU", "TSLA", "Tesla Inc", "NASDAQ", "USD",
         qty=85, avg=210.40, last=242.80, prev=248.10),
    # Taiwan — TSMC on TWSE (kept strictly distinct from mainland CN)
    _stk("Futu", "DEMO-FUTU", "2330", "Taiwan Semiconductor Mfg", "TWSE", "TWD",
         qty=300, avg=580.0, last=712.0, prev=706.0, fx_to_usd=0.0312,
         canonical="2330.TW"),
    # Cash
    _cash("Futu", "DEMO-FUTU", "HKD", 28_400.00, fx_to_usd=0.1282),
]


def build_broker() -> CompositeBroker:
    """Two fake adapters joined by CompositeBroker so the dashboard sees
    multiple connected brokers — broker chips render lit-up, account
    column populates with both account IDs."""
    return CompositeBroker([
        _MockAdapter(name="IBKR", positions=IBKR_POSITIONS),
        _MockAdapter(name="Futu", positions=FUTU_POSITIONS),
    ])


def main() -> None:
    os.environ.setdefault("BROKERS_ENABLED", "ibkr,futu")

    import uvicorn
    from app.main import create_app
    from app.core.markets import MarketHours

    # Pre-seed LivePositions so the SSE `snapshot` event doesn't blank out
    # the server-rendered tbody on page connect. In production this seeding
    # happens via the broker adapter's tick stream; in the mock we just push
    # the static fixtures up-front and leave them there.
    live = LivePositions()
    live.replace_all(IBKR_POSITIONS + FUTU_POSITIONS)

    # By default the mock uses the real clock, so the live market rail and the
    # "Updated …" / countdown timers (computed client-side against the browser's
    # clock) all stay correct while you browse.
    #
    # Set MOCK_CLOCK=demo to instead freeze the *server* clock to a curated
    # moment that exercises every market-status and closure state at once —
    # handy for regenerating the README screenshots. MarketHours accepts an
    # injectable clock; create_app forwards it to every market calculation.
    #
    # Demo moment: Wed 25 Nov 2026, 04:30 UTC.
    #   · HKEX 12:30 HKT → LUNCH 🟡     · TSE 13:30 JST → OPEN 🟢 (afternoon)
    #   · TWSE 12:30     → OPEN 🟢      · NYSE/LSE      → CLOSED 🔴
    #   · Closures this week (Mon 23–Fri 27): Thanksgiving Thu 26 (full HOLIDAY)
    #     + Black Friday 27 early close 13:00 ET (EARLY_CLOSE) — both kinds, and
    #     both still ahead of "today" so neither is dimmed as past.
    # (In demo mode the page's client-side countdowns will read against the real
    # browser clock; freeze it browser-side too when capturing screenshots.)
    market_hours = None
    if os.environ.get("MOCK_CLOCK", "").lower() in ("demo", "1", "true", "yes", "on"):
        from datetime import datetime, timezone
        frozen = datetime(2026, 11, 25, 4, 30, tzinfo=timezone.utc)
        market_hours = MarketHours(clock=lambda: frozen)
        print(f"  (MOCK_CLOCK=demo → server clock frozen at {frozen.isoformat()})", flush=True)

    app = create_app(broker=build_broker(), live_positions=live, market_hours=market_hours)
    port = int(os.environ.get("PORT", "8765"))
    print(f"\n  mock dashboard → http://127.0.0.1:{port}\n", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
