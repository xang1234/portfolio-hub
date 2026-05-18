#!/usr/bin/env python3
"""Slice 12 HITL — verify the IB Gateway is in READ_ONLY_API=yes mode.

Connects directly to the running gateway (NOT through the dashboard),
submits a tiny limit order well below market on a US ETF, and asserts the
response references read-only mode. Then disconnects without leaving any
orders queued.

This script is INTENTIONALLY OUTSIDE the dashboard process. The dashboard
must NEVER have an order-placement code path, even in tests. We run this
once during HITL verification (and after any gateway upgrade) to confirm
the gateway-side enforcement is on.

USAGE
-----
    .venv/bin/python scripts/verify_read_only_api.py

ENVIRONMENT
-----------
    IB_HOST       (default "127.0.0.1" — change to "ib-gateway" if running
                   inside docker-compose network)
    IB_PORT       (default 4001)
    IB_CLIENT_ID  (default 99 — use a clientId NOT in use by the dashboard)

EXIT CODES
----------
    0  — read-only mode VERIFIED (gateway rejected the order as expected)
    1  — read-only mode NOT enforced (order was accepted — DANGER, audit
         the gateway config immediately)
    2  — could not connect / unrelated error (didn't get a definitive answer)

WHEN TO RE-RUN
--------------
- Before going live for the first time (HITL slice 12).
- After any docker-compose change that touches the gateway service.
- After any gnzsnz/ib-gateway image bump.
"""

import asyncio
import logging
import os
import sys
from typing import Iterable


_READ_ONLY_HINTS: tuple[str, ...] = (
    "read-only",       # IB sometimes formats it this way
    "read only",
    "READ_ONLY_API",
    "readonly",
    "order placement is disabled",
)


def _looks_like_read_only_rejection(messages: Iterable[str]) -> bool:
    blob = " ".join(messages).lower()
    return any(hint.lower() in blob for hint in _READ_ONLY_HINTS)


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("verify_read_only_api")

    try:
        from ib_async import IB, Contract, LimitOrder
    except ImportError:
        log.error("ib_async not installed in the active venv")
        return 2

    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "4001"))
    client_id = int(os.environ.get("IB_CLIENT_ID", "99"))

    ib = IB()
    captured_errors: list[str] = []

    def _error_handler(reqId, errorCode, errorString, contract=None):
        captured_errors.append(f"[{errorCode}] {errorString}")
        log.warning("IB error %s: %s", errorCode, errorString)

    ib.errorEvent += _error_handler

    log.info("Connecting to gateway at %s:%s (clientId=%s)...", host, port, client_id)
    try:
        await ib.connectAsync(host, port, clientId=client_id)
    except Exception as exc:
        log.error("Could not connect to gateway: %s", exc)
        return 2

    try:
        # SPY (S&P 500 ETF) chosen for ubiquity + tight spread. Limit at
        # $1.00 — far below any plausible market level so this CANNOT fill
        # even if (somehow) the gateway accepts the order.
        contract = Contract(
            symbol="SPY", secType="STK", exchange="SMART", currency="USD",
        )
        # Qualify so we have a real conId / primaryExchange:
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            log.error("Could not qualify SPY contract — gateway didn't return details")
            return 2
        contract = qualified[0]

        order = LimitOrder("BUY", totalQuantity=1, lmtPrice=1.00)
        # Belt-and-suspenders: also set transmit=False would prevent submission,
        # but the whole POINT here is to TRY to submit and confirm rejection.
        order.transmit = True

        log.info("Attempting placeOrder (BUY 1 SPY LMT 1.00) — expecting rejection...")
        trade = ib.placeOrder(contract, order)

        # Give the gateway a moment to respond.
        await asyncio.sleep(3.0)

        order_status = trade.orderStatus.status if trade.orderStatus else "(no status)"
        log_messages = [entry.message for entry in (trade.log or [])]
        all_msgs = captured_errors + log_messages

        log.info("Order status: %s", order_status)
        for m in log_messages:
            log.info("Trade log: %s", m)

        # Defensive: try to cancel even if rejected, so nothing lingers.
        try:
            ib.cancelOrder(order)
        except Exception:
            pass

        if _looks_like_read_only_rejection(all_msgs):
            log.info("READ-ONLY VERIFIED — gateway rejected the order: %s",
                     "; ".join(all_msgs)[:200])
            return 0

        # If the gateway sent Submitted / PreSubmitted, READ_ONLY_API is OFF.
        if order_status in ("Submitted", "PreSubmitted", "Filled", "PartiallyFilled"):
            log.error(
                "ORDER WAS ACCEPTED (status=%s). READ_ONLY_API is NOT enforced. "
                "STOP and fix the gateway config before resuming.", order_status,
            )
            return 1

        # No definitive read-only error AND not an accepted status: this could be
        # a network timeout, a permissions error, or a misconfiguration. Don't
        # claim success — the operator should investigate.
        log.error(
            "INCONCLUSIVE — no read-only-mode rejection seen, but order also "
            "wasn't accepted. Messages: %s", "; ".join(all_msgs) or "(none)",
        )
        return 2
    finally:
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
