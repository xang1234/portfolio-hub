import asyncio
import errno
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient


def _client():
    from app.core.broker import ConnectionState
    from app.main import create_app

    class FakeAdapter:
        name = "IBKR"

        async def connect(self): pass
        async def disconnect(self): pass
        async def is_connected(self): return True
        async def get_connection_state(self): return ConnectionState.CONNECTED
        async def get_positions(self): return []
        async def get_account_summary(self): return []

    return TestClient(create_app(broker=FakeAdapter()))


def test_restart_gateway_disabled_without_command(monkeypatch):
    from app.core.gateway_control import GatewayRestartDisabled, restart_gateway

    monkeypatch.delenv("IBKR_GATEWAY_RESTART_COMMAND", raising=False)

    with pytest.raises(GatewayRestartDisabled):
        asyncio.run(restart_gateway())


def test_restart_gateway_runs_configured_command(tmp_path, monkeypatch):
    from app.core.gateway_control import restart_gateway

    marker = tmp_path / "gateway-restarted"
    command = (
        f"{sys.executable} -c "
        f"\"from pathlib import Path; Path({str(marker)!r}).write_text('ok')\""
    )
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", command)

    result = asyncio.run(restart_gateway())

    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert marker.read_text() == "ok"


def test_restart_gateway_timeout_raises_failed(monkeypatch):
    from app.core.gateway_control import GatewayRestartFailed, restart_gateway

    command = f"{sys.executable} -c \"import time; time.sleep(5)\""
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", command)
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_TIMEOUT_S", "0.01")

    with pytest.raises(GatewayRestartFailed, match="timed out"):
        asyncio.run(restart_gateway())


def test_restart_gateway_invalid_timeout_fails_before_start(tmp_path, monkeypatch):
    from app.core.gateway_control import GatewayRestartFailed, restart_gateway

    marker = tmp_path / "should-not-exist"
    command = (
        f"{sys.executable} -c "
        f"\"from pathlib import Path; Path({str(marker)!r}).write_text('started')\""
    )
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", command)
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_TIMEOUT_S", "not-a-number")

    with pytest.raises(GatewayRestartFailed, match="invalid gateway restart timeout"):
        asyncio.run(restart_gateway())
    assert not marker.exists()


def test_restart_gateway_nonpositive_timeout_fails_before_start(tmp_path, monkeypatch):
    from app.core.gateway_control import GatewayRestartFailed, restart_gateway

    marker = tmp_path / "should-not-exist"
    command = (
        f"{sys.executable} -c "
        f"\"from pathlib import Path; Path({str(marker)!r}).write_text('started')\""
    )
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", command)
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_TIMEOUT_S", "0")

    with pytest.raises(GatewayRestartFailed, match="gateway restart timeout must be positive"):
        asyncio.run(restart_gateway())
    assert not marker.exists()


def test_restart_gateway_malformed_command_fails_before_start(monkeypatch):
    from app.core.gateway_control import GatewayRestartFailed, restart_gateway

    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", f"{sys.executable} -c \"unterminated")

    with pytest.raises(GatewayRestartFailed, match="invalid gateway restart command"):
        asyncio.run(restart_gateway())


def test_restart_gateway_cancellation_kills_child(tmp_path, monkeypatch):
    from app.core.gateway_control import restart_gateway

    pid_file = tmp_path / "child.pid"
    command = (
        f"{sys.executable} -c "
        f"\"from pathlib import Path; import time; "
        f"Path({str(pid_file)!r}).write_text(str(__import__('os').getpid())); "
        f"time.sleep(30)\""
    )
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", command)
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_TIMEOUT_S", "30")

    async def run_and_cancel():
        task = asyncio.create_task(restart_gateway())
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    pid = asyncio.run(run_and_cancel())

    assert _pid_exited(pid)


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-only")
def test_restart_gateway_timeout_kills_descendant_process_group(tmp_path, monkeypatch):
    from app.core.gateway_control import GatewayRestartFailed, restart_gateway

    pid_file = tmp_path / "grandchild.pid"
    command = (
        f"{sys.executable} -c "
        f"\"import subprocess, sys, time; from pathlib import Path; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"Path({str(pid_file)!r}).write_text(str(p.pid)); "
        f"time.sleep(30)\""
    )
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", command)
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_TIMEOUT_S", "0.2")

    with pytest.raises(GatewayRestartFailed, match="timed out"):
        asyncio.run(restart_gateway())

    assert pid_file.exists()
    assert _pid_exited(int(pid_file.read_text()))


def _pid_exited(pid: int) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return True
            raise
        time.sleep(0.05)
    return False


def test_admin_gateway_restart_endpoint_requires_auth(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", f"{sys.executable} -c \"pass\"")

    response = _client().post("/admin/ibkr-gateway/restart")

    assert response.status_code == 401


def test_admin_gateway_restart_endpoint_fails_closed_without_admin_config(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_ALLOW_NO_AUTH", raising=False)
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", f"{sys.executable} -c \"pass\"")

    response = _client().post("/admin/ibkr-gateway/restart")

    assert response.status_code == 503
    assert "auth" in response.json().get("detail", "").lower()


def test_admin_gateway_restart_endpoint_returns_503_when_disabled(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("IBKR_GATEWAY_RESTART_COMMAND", raising=False)

    response = _client().post("/admin/ibkr-gateway/restart")

    assert response.status_code == 503
    assert "disabled" in response.json().get("detail", "").lower()


def test_admin_gateway_restart_endpoint_hides_stderr_on_failure(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv(
        "IBKR_GATEWAY_RESTART_COMMAND",
        f"{sys.executable} -c \"import sys; print('secret-token-123', file=sys.stderr); sys.exit(9)\"",
    )

    response = _client().post("/admin/ibkr-gateway/restart")

    assert response.status_code == 502
    assert response.json() == {"detail": "gateway restart command failed"}
    assert "secret-token-123" not in response.text


def test_admin_gateway_restart_endpoint_success(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", f"{sys.executable} -c \"pass\"")

    response = _client().post("/admin/ibkr-gateway/restart")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "exit_code": 0}
