"""Protected operator hooks for IB Gateway control."""

from dataclasses import dataclass

import asyncio
import os
import shlex
import signal


class GatewayRestartDisabled(RuntimeError):
    """Raised when no gateway restart command is configured."""


class GatewayRestartFailed(RuntimeError):
    """Raised when the configured gateway restart command fails."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ):
        super().__init__(message)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class GatewayRestartResult:
    exit_code: int
    stdout: str
    stderr: str


async def restart_gateway() -> GatewayRestartResult:
    command = os.environ.get("IBKR_GATEWAY_RESTART_COMMAND", "").strip()
    if not command:
        raise GatewayRestartDisabled("gateway restart command is disabled")

    try:
        timeout = float(os.environ.get("IBKR_GATEWAY_RESTART_TIMEOUT_S", "30"))
    except ValueError as exc:
        raise GatewayRestartFailed("invalid gateway restart timeout") from exc
    if timeout <= 0:
        raise GatewayRestartFailed("gateway restart timeout must be positive")

    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise GatewayRestartFailed("invalid gateway restart command") from exc
    if not args:
        raise GatewayRestartFailed("invalid gateway restart command")

    subprocess_kwargs = {}
    if os.name == "posix":
        # Wrapper commands may spawn docker-compose or helper descendants.
        # A fresh session lets timeout/cancellation kill the whole group.
        subprocess_kwargs["start_new_session"] = True
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_kwargs,
        )
    except OSError as exc:
        raise GatewayRestartFailed("gateway restart command failed to start") from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
    except TimeoutError as exc:
        await _kill_and_drain(process)
        raise GatewayRestartFailed("gateway restart command timed out") from exc
    except asyncio.CancelledError:
        await _kill_and_drain(process)
        raise

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    exit_code = process.returncode if process.returncode is not None else -1
    if exit_code != 0:
        raise GatewayRestartFailed(
            f"gateway restart command exited with code {exit_code}",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    return GatewayRestartResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )


async def _kill_and_drain(process: asyncio.subprocess.Process) -> None:
    _kill_process_group_or_child(process)
    await process.communicate()


def _kill_process_group_or_child(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass

    try:
        process.kill()
    except ProcessLookupError:
        pass
