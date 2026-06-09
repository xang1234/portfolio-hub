"""Broker connection monitoring and auth-attention notifications."""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.core.broker import ConnectionState
from app.core.notifications import Notifier


_LOG = logging.getLogger(__name__)
_DOWN_STATES = {ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED}


@dataclass
class ConnectionMonitorState:
    last_state: ConnectionState | None = None
    down_since: float | None = None
    attention_sent: bool = False
    pending_messages: list[str] = field(default_factory=list)


def _state_label(state: ConnectionState) -> str:
    return {
        ConnectionState.RECONNECTING: "reconnecting",
        ConnectionState.DISCONNECTED: "disconnected",
    }[state]


async def poll_connection_once(
    broker,
    *,
    notifier: Notifier,
    state: ConnectionMonitorState,
    now_monotonic: float,
    attention_after_s: float,
) -> None:
    """Poll broker state once and emit transition/attention notifications.

    `state` is caller-owned so tests can drive time deterministically and the
    production loop can retain state across polls. Notification state advances
    only after delivery succeeds, so transient Telegram failures are retried.
    """
    current = await broker.get_connection_state()
    previous = state.last_state

    if current is ConnectionState.CONNECTED:
        if previous in _DOWN_STATES or state.down_since is not None:
            _queue_message(state, "IBKR recovered.")
        await _send_pending_notifications(notifier, state)
        state.last_state = current
        state.down_since = None
        state.attention_sent = False
        return

    if current not in _DOWN_STATES:
        state.last_state = current
        return

    if state.down_since is None:
        state.down_since = now_monotonic

    if previous != current:
        _queue_message(state, f"IBKR {_state_label(current)}...")

    attention_due = False
    if (
        state.down_since is not None
        and not state.attention_sent
        and now_monotonic - state.down_since >= attention_after_s
    ):
        _queue_message(
            state,
            "IBKR manual auth may be needed. Use the dashboard retry/restart "
            "controls to recover the Gateway.",
        )
        attention_due = True

    await _send_pending_notifications(notifier, state)
    state.last_state = current
    if attention_due:
        state.attention_sent = True


def _queue_message(state: ConnectionMonitorState, message: str) -> None:
    if message not in state.pending_messages:
        state.pending_messages.append(message)


async def _send_pending_notifications(
    notifier: Notifier,
    state: ConnectionMonitorState,
) -> None:
    if not state.pending_messages:
        return
    await _send_notifications(notifier, state.pending_messages)
    state.pending_messages.clear()


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
    state = ConnectionMonitorState()
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
