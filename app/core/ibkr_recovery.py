"""IBKR connection recovery policy and operator controls."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping

from app.core.broker import Broker, ConnectionState
from app.core.gateway_control import GatewayRestartResult, restart_gateway


_LOG = logging.getLogger(__name__)

_STATE_STRINGS = {
    ConnectionState.CONNECTED: "connected",
    ConnectionState.RECONNECTING: "reconnecting",
    ConnectionState.DISCONNECTED: "disconnected",
}


def state_to_string(state: ConnectionState) -> str:
    return _STATE_STRINGS[state]


async def broker_status_map(broker_ref: Broker) -> dict[str, str]:
    """Return connection states keyed by broker name for health/status views."""
    states_getter = getattr(broker_ref, "get_connection_states", None)
    if callable(states_getter):
        states = await states_getter()
        return {
            name.lower(): state_to_string(state)
            for name, state in states.items()
        }
    return {
        getattr(broker_ref, "name", "broker").lower(): state_to_string(
            await broker_ref.get_connection_state()
        )
    }


async def retry_broker_now(broker_ref: Broker) -> None:
    """Wake a reconnecting broker, or start a disconnected broker."""
    retry_now = getattr(broker_ref, "retry_now", None)
    if callable(retry_now):
        await retry_now()
        return

    retry_disconnected = getattr(broker_ref, "retry_disconnected", None)
    if callable(retry_disconnected):
        await retry_disconnected()
        return

    if await broker_ref.get_connection_state() is ConnectionState.DISCONNECTED:
        start = getattr(broker_ref, "start", None)
        if callable(start):
            await start()


def select_ibkr_broker(broker: Broker) -> Broker | None:
    """Return the IBKR adapter from a single or composite broker."""
    if broker.name.lower() == "ibkr":
        return broker
    for adapter in getattr(broker, "adapters", ()):
        if adapter.name.lower() == "ibkr":
            return adapter
    return None


def positive_env_float(
    name: str,
    default: float,
    *,
    env: Mapping[str, str] | None = None,
) -> float:
    source = os.environ if env is None else env
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning("Invalid %s=%r; using default %.1f", name, raw, default)
        return default
    if not math.isfinite(value) or value <= 0:
        _LOG.warning("Invalid %s=%r; using default %.1f", name, raw, default)
        return default
    return value


def gateway_restart_visible_for_statuses(
    statuses: Mapping[str, str],
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if env is None else env
    return (
        bool(source.get("IBKR_GATEWAY_RESTART_COMMAND", "").strip())
        and _env_bool("ADMIN_ALLOW_NO_AUTH", False, env=source)
        and source.get("ADMIN_TOKEN", "") == ""
        and statuses.get("ibkr") in {"reconnecting", "disconnected"}
    )


async def restart_gateway_then_retry_ibkr(broker: Broker) -> GatewayRestartResult:
    """Restart Gateway, then wake the IBKR adapter reconnect path."""
    result = await restart_gateway()
    ibkr_broker = select_ibkr_broker(broker)
    if ibkr_broker is not None:
        try:
            await retry_broker_now(ibkr_broker)
        except Exception as exc:
            _LOG.warning("gateway restart reconnect wake failed: %s", exc)
    return result


def _env_bool(
    name: str,
    default: bool = False,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
