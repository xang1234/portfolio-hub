"""Broker connection monitoring and auth-attention notifications."""

import asyncio
import logging
import time

from app.core.broker import ConnectionState
from app.core.notifications import Notifier


_LOG = logging.getLogger(__name__)
_DOWN_STATES = {ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED}


def _state_label(state: ConnectionState) -> str:
    return {
        ConnectionState.RECONNECTING: "reconnecting",
        ConnectionState.DISCONNECTED: "disconnected",
    }[state]


async def poll_connection_once(
    broker,
    *,
    notifier: Notifier,
    state: dict,
    now_monotonic: float,
    attention_after_s: float,
) -> None:
    """Poll broker state once and emit transition/attention notifications.

    `state` is caller-owned so tests can drive time deterministically and the
    production loop can retain state across polls without a monitor class.
    """
    current = await broker.get_connection_state()
    previous = state.get("last_state")

    messages: list[str] = []

    if current is ConnectionState.CONNECTED:
        if previous in _DOWN_STATES or state.get("down_since") is not None:
            messages.append("IBKR recovered.")
        state["last_state"] = current
        state["down_since"] = None
        state["attention_sent"] = False
        await _send_notifications(notifier, messages)
        return

    if current not in _DOWN_STATES:
        state["last_state"] = current
        return

    if state.get("down_since") is None:
        state["down_since"] = now_monotonic

    down_since = state.get("down_since")
    if previous != current:
        messages.append(f"IBKR {_state_label(current)}...")

    if (
        down_since is not None
        and not state.get("attention_sent", False)
        and now_monotonic - down_since >= attention_after_s
    ):
        messages.append(
            "IBKR manual auth may be needed. Use the dashboard retry/restart "
            "controls to recover the Gateway."
        )
        state["attention_sent"] = True

    state["last_state"] = current
    await _send_notifications(notifier, messages)


async def _send_notifications(notifier: Notifier, messages: list[str]) -> None:
    first_error: Exception | None = None
    for message in messages:
        try:
            await notifier.send(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


async def monitor_connection(
    broker,
    *,
    notifier: Notifier,
    interval_s: float = 30.0,
    attention_after_s: float = 120.0,
) -> None:
    state: dict = {}
    while True:
        try:
            await poll_connection_once(
                broker,
                notifier=notifier,
                state=state,
                now_monotonic=time.monotonic(),
                attention_after_s=attention_after_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.warning("connection monitor poll failed: %s", exc)
        await asyncio.sleep(interval_s)
