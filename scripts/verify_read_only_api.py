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
Run INSIDE the dashboard container so the script reaches the gateway over
the internal Docker network (port 4001 is NOT exposed on the host):

    docker compose run --rm dashboard \\
        .venv/bin/python scripts/verify_read_only_api.py

If you really want to run on the host, you'll need to add a temporary
`ports: ["127.0.0.1:4001:4001"]` to docker-compose.override.yml AND
set IB_HOST=127.0.0.1 — but the container approach is preferred because
it leaves no port-forward residue behind.

ENVIRONMENT
-----------
    IB_HOST       (default "ib-gateway" — Docker internal hostname)
    IB_PORT       (default 4001)
    IB_CLIENT_ID  (default randomized 50-90 to avoid collisions with the
                   dashboard's clientId=1 and re-runs of this script)

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
# IB error codes that signal the read-only API path. 201 is the documented
# "Order rejected" code and is the most stable signal (string text changes
# across gateway versions, codes do not). Other codes added defensively.
_READ_ONLY_ERROR_CODES: frozenset[int] = frozenset({201, 504, 10148})

_ACCEPTED_STATUSES: frozenset[str] = frozenset({
    "Submitted", "PreSubmitted", "Filled", "PartiallyFilled",
})


def _looks_like_read_only_rejection(messages: Iterable[str]) -> bool:
    """Substring match against the joined message blob. Best-effort —
    paired with error-code matching and the order-status check below for
    a definitive PASS signal."""
    blob = " ".join(messages).lower()
    return any(hint.lower() in blob for hint in _READ_ONLY_HINTS)


def _has_read_only_error_code(error_codes: Iterable[int]) -> bool:
    return any(c in _READ_ONLY_ERROR_CODES for c in error_codes)


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("verify_read_only_api")

    try:
        from ib_async import IB, Contract, LimitOrder
    except ImportError:
        log.error("ib_async not installed in the active venv")
        return 2

    import random

    host = os.environ.get("IB_HOST", "ib-gateway")
    port = int(os.environ.get("IB_PORT", "4001"))
    # Randomize clientId so re-runs don't collide with each other or with
    # the dashboard (clientId=1). Range 50-90 stays clear of common defaults.
    client_id = int(os.environ.get("IB_CLIENT_ID") or random.randint(50, 90))

    ib = IB()
    captured_errors: list[str] = []
    captured_error_codes: list[int] = []

    def _error_handler(reqId, errorCode, errorString, contract=None):
        captured_errors.append(f"[{errorCode}] {errorString}")
        try:
            captured_error_codes.append(int(errorCode))
        except (TypeError, ValueError):
            pass
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

        # Capture BEFORE cancelling so the cancel response can't add a
        # post-hoc "read-only" hint that misleads the substring matcher.
        accepted_by_gateway = order_status in _ACCEPTED_STATUSES
        read_only_signal = (
            _has_read_only_error_code(captured_error_codes)
            or _looks_like_read_only_rejection(all_msgs)
        )

        # Defensive: try to cancel even if rejected, so nothing lingers.
        try:
            ib.cancelOrder(order)
        except Exception:
            pass

        # PASS requires BOTH signals: a read-only-shaped message/code AND
        # the order NOT in an accepted status. Either alone is ambiguous —
        # a substring "read-only" can appear in unrelated messages, and a
        # non-accepted status alone could be a price-reasonability reject.
        if read_only_signal and not accepted_by_gateway:
            log.info(
                "READ-ONLY VERIFIED — error code %s, status=%s, msgs: %s",
                captured_error_codes or "(none)", order_status,
                "; ".join(all_msgs)[:200],
            )
            return 0

        if accepted_by_gateway:
            log.error(
                "ORDER WAS ACCEPTED (status=%s). READ_ONLY_API is NOT enforced. "
                "STOP and fix the gateway config before resuming.", order_status,
            )
            return 1

        # Not accepted, but no clean read-only signal either: could be a
        # network timeout, a permissions error, or a misconfiguration. Don't
        # claim success — the operator should investigate.
        log.error(
            "INCONCLUSIVE — status=%s, codes=%s, msgs: %s",
            order_status, captured_error_codes or "(none)",
            "; ".join(all_msgs) or "(none)",
        )
        return 2
    finally:
        ib.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
