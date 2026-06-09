# IBKR Auth Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make portfolio-hub follow IBKR's intended weekly manual authentication model with daily automatic restarts, while giving the operator reliable retry/restart controls and Telegram alerts when attention is needed.

**Architecture:** Keep IB Gateway/IBC responsible for IBKR login and 2FA. portfolio-hub should not try to automate phone approval; it should configure/read the gateway correctly, reconnect the API client quickly, optionally invoke a guarded host restart command, and notify the operator when the gateway appears to need manual action.

**Tech Stack:** FastAPI, Jinja/HTMX, asyncio, Docker Compose, IBC config template, pytest, httpx for Telegram Bot API calls.

---

## Source Facts

- IBKR daily requirement is a platform restart, not necessarily a daily phone reauthentication.
- With Auto Restart, TWS/IB Gateway can usually restart daily without user auth Monday-Saturday; manual authentication is expected after the Saturday/Sunday reset or when IBKR invalidates the token.
- portfolio-hub's read-only dashboard flow should keep `READ_ONLY_LOGIN=yes` and `READ_ONLY_API=yes`.
- The dashboard retry button is currently an API reconnect action only; it does not restart IB Gateway and cannot force another IBKR Mobile push.
- Gateway restart from the dashboard must be opt-in and admin-protected. Do not mount the Docker socket into the dashboard container.

## File Structure

- Modify `.env.example`: document weekly auth, daily restart, Telegram env vars, restart-command opt-in.
- Modify `docker-compose.yml`: pass through `AUTO_RESTART_TIME`, `TWOFA_EXIT_INTERVAL`, and `TWS_COLD_RESTART` to the gateway container.
- Modify `PLAN.md`: correct stale daily-2FA language.
- Create `docs/runbooks/ibkr-auth.md`: operator-facing auth/restart runbook.
- Modify `app/adapters/ibkr.py`: add an optional `retry_now()` capability that wakes the reconnect loop immediately.
- Modify `app/core/composite_broker.py`: forward `retry_now()` to reconnecting children and keep `retry_disconnected()` for fully disconnected children.
- Modify `app/main.py`: expose safer retry/restart endpoints and start a notification monitor during lifespan.
- Create `app/core/gateway_control.py`: execute an allow-listed restart command configured by env.
- Create `app/core/notifications.py`: Telegram notifier with disabled-by-default behavior.
- Create `app/jobs/connection_monitor.py`: state-transition and long-down alert loop.
- Modify `app/templates/partials/status_badge.html`: show stable retry/restart affordances.
- Modify `app/static/app.css`: style the status actions without disrupting the existing header.
- Add tests:
  - `tests/test_ibkr_retry_now.py`
  - `tests/test_gateway_control.py`
  - `tests/test_healthz_retry.py`
  - `tests/test_connection_monitor_notifications.py`
  - `tests/test_composite_broker.py`

---

### Task 1: Correct Gateway Auth Configuration And Docs

**Files:**
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Modify: `PLAN.md`
- Create: `docs/runbooks/ibkr-auth.md`

- [ ] **Step 1: Update `.env.example` with the intended auth model**

Replace the IBKR auth comments near the top with:

```dotenv
# Login mode for portfolio-hub's read-only dashboard.
# Keep READ_ONLY_LOGIN=yes so IB Gateway can log in read-only without a daily
# IBKR Mobile approval. This is separate from READ_ONLY_API=yes below.
READ_ONLY_LOGIN=yes

# Daily restart / weekly auth model.
# IB Gateway/TWS must restart daily. With Auto Restart, IBKR normally preserves
# the login token Monday-Saturday and asks for manual authentication after the
# Saturday/Sunday reset, or earlier if IBKR invalidates the token.
#
# Use a local time when the laptop is awake. Format for IBC AutoRestartTime is
# "HH:MM AM/PM" with one space before AM/PM.
AUTO_RESTART_TIME=06:00 AM

# Weekly cold restart time, local timezone, after 01:00 US/Eastern on Sunday.
# IBC applies ColdRestartTime on Sunday only; format is HH:MM.
# 15:30 Singapore time is safely after 01:00 US/Eastern year-round.
# Pick a time when you can approve IBKR Mobile if prompted.
TWS_COLD_RESTART=15:30

# 2FA fallback if READ_ONLY_LOGIN is disabled or IBKR requires manual auth.
TWOFA_DEVICE=mobile
RELOGIN_AFTER_TWOFA_TIMEOUT=yes
TWOFA_EXIT_INTERVAL=60
```

- [ ] **Step 2: Pass the restart env vars to `ib-gateway`**

In `docker-compose.yml`, add these under `services.ib-gateway.environment`:

```yaml
      AUTO_RESTART_TIME: ${AUTO_RESTART_TIME:-06:00 AM}
      AUTO_LOGOFF_TIME: ${AUTO_LOGOFF_TIME:-}
      TWS_COLD_RESTART: ${TWS_COLD_RESTART:-15:30}
      TWOFA_EXIT_INTERVAL: ${TWOFA_EXIT_INTERVAL:-60}
```

Keep:

```yaml
      READ_ONLY_LOGIN: ${READ_ONLY_LOGIN:-yes}
      READ_ONLY_API: ${READ_ONLY_API:-yes}
```

- [ ] **Step 3: Correct `PLAN.md` language**

Replace the current "one tap per day" row with:

```markdown
| **IBKR auth** | `READ_ONLY_LOGIN=yes` + `READ_ONLY_API=yes` for the read-only dashboard. IB Gateway/TWS still restarts daily, but Auto Restart should avoid daily phone reauthentication; manual auth is expected weekly after the Saturday/Sunday reset or when IBKR invalidates the token. |
| **IBKR daily restart** | `AUTO_RESTART_TIME` configures the daily Gateway restart. `IbkrAdapter` reconnects with backoff and can be manually retried from the UI. |
```

- [ ] **Step 4: Add the runbook**

Create `docs/runbooks/ibkr-auth.md`:

```markdown
# IBKR Auth Runbook

## Intended Model

- Daily: IB Gateway restarts automatically.
- Weekly: Manual IBKR Mobile authentication may be required after the Saturday/Sunday reset.
- Unexpected: Manual auth may be required if IBKR invalidates the token, the laptop slept, network/update work interrupted Gateway, or another login reused the same IBKR username.

## Normal Morning Check

1. Open portfolio-hub.
2. If IBKR is connected, do nothing.
3. If IBKR is reconnecting for less than 2 minutes, wait.
4. If it remains reconnecting or disconnected, tap `Retry now`.
5. If retry does not recover, tap `Restart Gateway` when available.
6. If Telegram says manual auth is likely needed, approve the IBKR Mobile prompt or open IB Gateway/TWS on the host.

## Configuration

Keep:

```dotenv
READ_ONLY_LOGIN=yes
READ_ONLY_API=yes
AUTO_RESTART_TIME=06:00 AM
TWS_COLD_RESTART=15:30
RELOGIN_AFTER_TWOFA_TIMEOUT=yes
```

`AUTO_RESTART_TIME` should be a daily time when the host laptop is awake.
`TWS_COLD_RESTART` is Sunday-only in IBC and should be a `HH:MM` local time when you can approve IBKR Mobile.

## What The Dashboard Buttons Do

- `Retry now`: wakes portfolio-hub's API reconnect loop. It does not send a new IBKR Mobile push.
- `Restart Gateway`: runs the configured host restart command. It may cause IBKR Mobile auth if the gateway token is invalid or the weekly reset happened.

## Telegram Alerts

Telegram alerts should fire when:

- IBKR enters reconnecting.
- IBKR remains unavailable for more than 2 minutes.
- IBKR reaches disconnected/backoff-exhausted.
- IBKR recovers.
```

- [ ] **Step 5: Verify compose renders**

Run:

```bash
docker compose config >/tmp/portfolio-hub-compose.yml
rg "AUTO_RESTART_TIME|TWS_COLD_RESTART|READ_ONLY_LOGIN|TWOFA_EXIT_INTERVAL" /tmp/portfolio-hub-compose.yml
```

Expected: all four env names appear in the rendered gateway service.

- [ ] **Step 6: Commit**

```bash
git add .env.example docker-compose.yml PLAN.md docs/runbooks/ibkr-auth.md
git commit -m "docs: clarify ibkr weekly authentication model"
```

---

### Task 2: Add Immediate Retry Capability To IBKR Adapter

**Files:**
- Modify: `app/adapters/ibkr.py`
- Add: `tests/test_ibkr_retry_now.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_ibkr_retry_now.py`:

```python
import asyncio

from app.core.broker import ConnectionState


class _Event:
    def __init__(self):
        self._callbacks = []

    def __iadd__(self, cb):
        self._callbacks.append(cb)
        return self

    def __isub__(self, cb):
        if cb in self._callbacks:
            self._callbacks.remove(cb)
        return self

    def emit(self):
        for cb in list(self._callbacks):
            cb()


class FakeIB:
    def __init__(self):
        self._connected = False
        self.connect_attempts = 0
        self.disconnectedEvent = _Event()
        self.next_failures = 0

    async def connectAsync(self, host, port, clientId):
        self.connect_attempts += 1
        if self.next_failures > 0:
            self.next_failures -= 1
            raise ConnectionRefusedError("simulated")
        self._connected = True

    def disconnect(self):
        self._connected = False

    def isConnected(self):
        return self._connected

    def reqMarketDataType(self, _type):
        pass

    def simulate_disconnect(self):
        self._connected = False
        self.disconnectedEvent.emit()


def _adapter(fake_ib, delays):
    from app.adapters.ibkr import IbkrAdapter

    return IbkrAdapter(
        host="ib-gateway",
        port=4003,
        client_id=1,
        ib_factory=lambda: fake_ib,
        reconnect_delays=delays,
    )


async def test_retry_now_wakes_reconnecting_loop_without_waiting_full_backoff():
    fake_ib = FakeIB()
    adapter = _adapter(fake_ib, delays=[60.0])
    await adapter.connect()
    initial_attempts = fake_ib.connect_attempts

    fake_ib.simulate_disconnect()
    await asyncio.sleep(0.01)
    assert await adapter.get_connection_state() is ConnectionState.RECONNECTING

    await adapter.retry_now()
    await asyncio.sleep(0.05)

    assert fake_ib.connect_attempts > initial_attempts
    assert await adapter.get_connection_state() is ConnectionState.CONNECTED


async def test_retry_now_on_disconnected_starts_reconnect_flow():
    fake_ib = FakeIB()
    adapter = _adapter(fake_ib, delays=[0.01])

    await adapter.retry_now()
    await asyncio.sleep(0.05)

    assert fake_ib.connect_attempts >= 1
    assert await adapter.get_connection_state() is ConnectionState.CONNECTED


async def test_retry_now_on_connected_is_noop():
    fake_ib = FakeIB()
    adapter = _adapter(fake_ib, delays=[0.01])
    await adapter.connect()
    attempts = fake_ib.connect_attempts

    await adapter.retry_now()

    assert fake_ib.connect_attempts == attempts
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_ibkr_retry_now.py -v
```

Expected: fails because `IbkrAdapter.retry_now` does not exist.

- [ ] **Step 3: Implement wakeable reconnect loop**

In `IbkrAdapter.__init__`, add:

```python
self._retry_now_event = asyncio.Event()
```

Add method:

```python
async def retry_now(self) -> None:
    """Operator-triggered immediate reconnect attempt.

    CONNECTED: no-op.
    RECONNECTING: wake the current backoff sleep.
    DISCONNECTED: start the same reconnect path used after initial failure.
    """
    if self._connection_state is ConnectionState.CONNECTED:
        return
    if self._connection_state is ConnectionState.RECONNECTING:
        self._retry_now_event.set()
        return
    self._handle_disconnect()
    self._retry_now_event.set()
```

Replace the reconnect sleep in `_reconnect_loop()`:

```python
self._current_backoff_delay = delay
self._retry_now_event.clear()
try:
    await asyncio.wait_for(self._retry_now_event.wait(), timeout=delay)
except asyncio.TimeoutError:
    pass
finally:
    self._retry_now_event.clear()
```

- [ ] **Step 4: Run retry tests**

Run:

```bash
pytest tests/test_ibkr_retry_now.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Run reconnect regression tests**

Run:

```bash
pytest tests/test_reconnect.py tests/test_reconnect_edges.py tests/test_reconnect_badge_countdown.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/adapters/ibkr.py tests/test_ibkr_retry_now.py
git commit -m "feat: allow immediate ibkr reconnect retry"
```

---

### Task 3: Forward Retry In Composite Broker And HTTP Route

**Files:**
- Modify: `app/core/composite_broker.py`
- Modify: `app/main.py`
- Modify: `tests/test_composite_broker.py`
- Modify: `tests/test_healthz_retry.py`

- [ ] **Step 1: Add composite retry test**

Append to `tests/test_composite_broker.py`:

```python
def test_healthz_retry_wakes_reconnecting_child_in_composite_broker():
    from fastapi.testclient import TestClient

    from app.core.composite_broker import CompositeBroker
    from app.main import create_app

    class RetryAdapter(_Adapter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.retry_now_calls = 0

        async def retry_now(self):
            self.retry_now_calls += 1
            self._state = ConnectionState.CONNECTED

    ibkr = RetryAdapter("IBKR", state=ConnectionState.RECONNECTING)
    futu = _Adapter("Futu", state=ConnectionState.CONNECTED)
    app = create_app(broker=CompositeBroker([ibkr, futu]))

    response = TestClient(app).post("/healthz/retry")

    assert response.status_code == 200
    assert ibkr.retry_now_calls == 1
    assert "connected" in response.text
```

- [ ] **Step 2: Add route-level reconnecting retry test**

Append to `tests/test_healthz_retry.py`:

```python
def test_post_healthz_retry_calls_retry_now_when_reconnecting():
    class RetryAdapter(FakeAdapter):
        def __init__(self, *, state):
            super().__init__(state=state)
            self.retry_now_calls = 0

        async def retry_now(self):
            self.retry_now_calls += 1
            self._state = ConnectionState.CONNECTED

    from app.main import create_app

    adapter = RetryAdapter(state=ConnectionState.RECONNECTING)
    client = TestClient(create_app(broker=adapter))

    response = client.post("/healthz/retry")

    assert response.status_code == 200
    assert adapter.retry_now_calls == 1
    assert "connected" in response.text
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
pytest tests/test_composite_broker.py::test_healthz_retry_wakes_reconnecting_child_in_composite_broker tests/test_healthz_retry.py::test_post_healthz_retry_calls_retry_now_when_reconnecting -v
```

Expected: tests fail because reconnecting retry is currently a no-op.

- [ ] **Step 4: Implement forwarding**

Add to `CompositeBroker`:

```python
async def retry_now(self) -> None:
    """Wake reconnecting children and start disconnected children."""
    states = await self.get_connection_states()
    tasks = []
    for adapter in self._adapters:
        state = states.get(adapter.name)
        if state is ConnectionState.RECONNECTING:
            retry_now = getattr(adapter, "retry_now", None)
            if callable(retry_now):
                tasks.append(retry_now())
        elif state is ConnectionState.DISCONNECTED:
            tasks.append(self._start_one(adapter))
    if tasks:
        await asyncio.gather(*tasks)
```

In `app/main.py`, update `/healthz/retry` before the existing `retry_disconnected` branch:

```python
retry_now = getattr(broker, "retry_now", None)
if callable(retry_now):
    await retry_now()
    conn_state = await broker.get_connection_state()
else:
    retry_disconnected = getattr(broker, "retry_disconnected", None)
    if callable(retry_disconnected):
        await retry_disconnected()
        conn_state = await broker.get_connection_state()
    elif conn_state == ConnectionState.DISCONNECTED:
        start = getattr(broker, "start", None)
        if callable(start):
            await start()
        conn_state = await broker.get_connection_state()
```

- [ ] **Step 5: Run route/composite tests**

Run:

```bash
pytest tests/test_composite_broker.py tests/test_healthz_retry.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/core/composite_broker.py app/main.py tests/test_composite_broker.py tests/test_healthz_retry.py
git commit -m "feat: retry reconnecting brokers on demand"
```

---

### Task 4: Add Protected Gateway Restart Command

**Files:**
- Create: `app/core/gateway_control.py`
- Modify: `app/main.py`
- Add: `tests/test_gateway_control.py`
- Modify: `.env.example`
- Modify: `docs/runbooks/ibkr-auth.md`

- [ ] **Step 1: Write gateway control tests**

Create `tests/test_gateway_control.py`:

```python
import os

import pytest


async def test_restart_gateway_disabled_without_command(monkeypatch):
    from app.core.gateway_control import GatewayRestartDisabled, restart_gateway

    monkeypatch.delenv("IBKR_GATEWAY_RESTART_COMMAND", raising=False)

    with pytest.raises(GatewayRestartDisabled):
        await restart_gateway()


async def test_restart_gateway_runs_configured_command(monkeypatch, tmp_path):
    from app.core.gateway_control import restart_gateway

    marker = tmp_path / "ran"
    monkeypatch.setenv(
        "IBKR_GATEWAY_RESTART_COMMAND",
        f"/bin/sh -c 'printf ok > {marker}'",
    )

    result = await restart_gateway()

    assert result.exit_code == 0
    assert marker.read_text() == "ok"


async def test_restart_gateway_times_out(monkeypatch):
    from app.core.gateway_control import GatewayRestartFailed, restart_gateway

    monkeypatch.setenv("IBKR_GATEWAY_RESTART_COMMAND", "/bin/sh -c 'sleep 5'")
    monkeypatch.setenv("IBKR_GATEWAY_RESTART_TIMEOUT_S", "0.01")

    with pytest.raises(GatewayRestartFailed):
        await restart_gateway()
```

- [ ] **Step 2: Implement gateway control module**

Create `app/core/gateway_control.py`:

```python
import asyncio
import os
import shlex
from dataclasses import dataclass


class GatewayRestartDisabled(RuntimeError):
    pass


class GatewayRestartFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayRestartResult:
    exit_code: int
    stdout: str
    stderr: str


async def restart_gateway() -> GatewayRestartResult:
    raw = os.environ.get("IBKR_GATEWAY_RESTART_COMMAND", "").strip()
    if not raw:
        raise GatewayRestartDisabled("IBKR_GATEWAY_RESTART_COMMAND is not configured")

    timeout = float(os.environ.get("IBKR_GATEWAY_RESTART_TIMEOUT_S", "30"))
    argv = shlex.split(raw)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise GatewayRestartFailed("gateway restart command timed out") from exc

    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    if proc.returncode != 0:
        raise GatewayRestartFailed(
            f"gateway restart command exited {proc.returncode}: {stderr.strip()}"
        )
    return GatewayRestartResult(proc.returncode, stdout, stderr)
```

- [ ] **Step 3: Add admin endpoint tests**

Add tests in `tests/test_gateway_control.py`:

```python
def test_admin_restart_gateway_requires_admin_auth(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    client = TestClient(create_app())

    response = client.post("/admin/ibkr-gateway/restart")

    assert response.status_code == 401


def test_admin_restart_gateway_returns_disabled_when_not_configured(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("ADMIN_ALLOW_NO_AUTH", "1")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("IBKR_GATEWAY_RESTART_COMMAND", raising=False)

    response = TestClient(create_app()).post("/admin/ibkr-gateway/restart")

    assert response.status_code == 503
```

- [ ] **Step 4: Add endpoint**

In `app/main.py`, add:

```python
@app.post("/admin/ibkr-gateway/restart")
async def admin_restart_ibkr_gateway(request: Request):
    _require_admin_token(request)
    from app.core.gateway_control import (
        GatewayRestartDisabled,
        GatewayRestartFailed,
        restart_gateway,
    )

    try:
        result = await restart_gateway()
    except GatewayRestartDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GatewayRestartFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse({"status": "ok", "exit_code": result.exit_code})
```

- [ ] **Step 5: Document env vars**

Add to `.env.example`:

```dotenv
# Optional protected Gateway restart button.
# Leave blank to hide/disable restart. Prefer a host wrapper script over mounting
# the Docker socket into the dashboard container.
IBKR_GATEWAY_RESTART_COMMAND=
IBKR_GATEWAY_RESTART_TIMEOUT_S=30
```

For a local Docker Compose deployment, the command can be:

```dotenv
IBKR_GATEWAY_RESTART_COMMAND=/bin/sh -lc 'docker compose restart ib-gateway'
```

- [ ] **Step 6: Run tests**

Run:

```bash
pytest tests/test_gateway_control.py tests/test_admin_reconcile_endpoint.py -v
```

Expected: all tests pass and existing admin auth behavior remains intact.

- [ ] **Step 7: Commit**

```bash
git add app/core/gateway_control.py app/main.py tests/test_gateway_control.py .env.example docs/runbooks/ibkr-auth.md
git commit -m "feat: add protected ibkr gateway restart hook"
```

---

### Task 5: Improve Status UI Actions

**Files:**
- Modify: `app/templates/partials/status_badge.html`
- Modify: `app/static/app.css`
- Modify: `app/main.py`
- Modify: `tests/test_healthz_retry.py`

- [ ] **Step 1: Update template tests**

Add expectations to `tests/test_healthz_retry.py`:

```python
def test_reconnecting_badge_has_retry_action():
    client, _ = make_client(ConnectionState.RECONNECTING)
    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/healthz/retry" in response.text
    assert "Retry now" in response.text


def test_restart_action_is_hidden_when_not_configured(monkeypatch):
    monkeypatch.delenv("IBKR_GATEWAY_RESTART_COMMAND", raising=False)
    client, _ = make_client(ConnectionState.DISCONNECTED)

    response = client.get("/healthz", headers={"HX-Request": "true"})

    assert "/admin/ibkr-gateway/restart" not in response.text
```

- [ ] **Step 2: Pass restart availability into template**

In `app/main.py`, add helper:

```python
def _gateway_restart_enabled() -> bool:
    return bool(os.environ.get("IBKR_GATEWAY_RESTART_COMMAND", "").strip())
```

Include this context in `index`, `/healthz`, and `/healthz/retry` template responses:

```python
"gateway_restart_enabled": _gateway_restart_enabled(),
```

- [ ] **Step 3: Update status template**

For reconnecting and disconnected branches, render actions:

```html
<span class="status-actions">
  <button type="button"
          class="status-action"
          hx-post="/healthz/retry"
          hx-trigger="click"
          hx-swap="outerHTML"
          hx-target="#ibkr-status">Retry now</button>
  {% if gateway_restart_enabled %}
  <button type="button"
          class="status-action status-action--danger"
          hx-post="/admin/ibkr-gateway/restart"
          hx-trigger="click"
          hx-confirm="Restart IB Gateway?"
          hx-swap="none">Restart Gateway</button>
  {% endif %}
</span>
```

Keep the root `id="ibkr-status"` wrapper so HTMX swaps remain stable.

- [ ] **Step 4: Add compact styles**

Add to `app/static/app.css`:

```css
.status-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.status-action {
  padding: 0.16rem 0.5rem;
  border-radius: var(--ph-pill);
  border: 1px solid var(--ph-border);
  background: var(--ph-surface-2);
  color: var(--ph-text-2);
  font: inherit;
  font-size: 12px;
  line-height: 1.3;
  cursor: pointer;
}
.status-action--danger {
  border-color: color-mix(in srgb, var(--ph-neg) 35%, transparent);
  color: var(--ph-neg);
}
```

- [ ] **Step 5: Run UI-related tests**

Run:

```bash
pytest tests/test_healthz_retry.py tests/test_healthz_three_states.py tests/test_status_badge_styles.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/templates/partials/status_badge.html app/static/app.css app/main.py tests/test_healthz_retry.py
git commit -m "feat: show reliable ibkr retry controls"
```

---

### Task 6: Add Telegram Notifications

**Files:**
- Create: `app/core/notifications.py`
- Create: `app/jobs/connection_monitor.py`
- Modify: `app/main.py`
- Modify: `.env.example`
- Add: `tests/test_connection_monitor_notifications.py`

- [ ] **Step 1: Write notifier tests**

Create `tests/test_connection_monitor_notifications.py`:

```python
import asyncio

from app.core.broker import ConnectionState


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def send(self, text):
        self.messages.append(text)


class FakeBroker:
    name = "IBKR"

    def __init__(self, states):
        self._states = list(states)

    async def get_connection_state(self):
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]


async def test_monitor_notifies_on_reconnecting_transition():
    from app.jobs.connection_monitor import poll_connection_once

    notifier = FakeNotifier()
    state = {}
    broker = FakeBroker([ConnectionState.RECONNECTING])

    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=10.0,
        attention_after_s=120.0,
    )

    assert len(notifier.messages) == 1
    assert "IBKR reconnecting" in notifier.messages[0]


async def test_monitor_notifies_when_reconnect_exceeds_attention_threshold():
    from app.jobs.connection_monitor import poll_connection_once

    notifier = FakeNotifier()
    state = {
        "last_state": ConnectionState.RECONNECTING,
        "down_since": 0.0,
        "attention_sent": False,
    }
    broker = FakeBroker([ConnectionState.RECONNECTING])

    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=130.0,
        attention_after_s=120.0,
    )

    assert len(notifier.messages) == 1
    assert "manual auth may be needed" in notifier.messages[0].lower()


async def test_monitor_notifies_on_recovery():
    from app.jobs.connection_monitor import poll_connection_once

    notifier = FakeNotifier()
    state = {
        "last_state": ConnectionState.RECONNECTING,
        "down_since": 0.0,
        "attention_sent": True,
    }
    broker = FakeBroker([ConnectionState.CONNECTED])

    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=150.0,
        attention_after_s=120.0,
    )

    assert len(notifier.messages) == 1
    assert "IBKR recovered" in notifier.messages[0]
```

- [ ] **Step 2: Implement Telegram notifier**

Create `app/core/notifications.py`:

```python
import os

import httpx


class NullNotifier:
    async def send(self, text: str) -> None:
        return None


class TelegramNotifier:
    def __init__(self, *, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()


def build_notifier():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return NullNotifier()
    return TelegramNotifier(bot_token=token, chat_id=chat_id)
```

- [ ] **Step 3: Implement connection monitor**

Create `app/jobs/connection_monitor.py`:

```python
import asyncio
import logging
import time

from app.core.broker import ConnectionState


_LOG = logging.getLogger(__name__)


async def poll_connection_once(
    broker,
    *,
    notifier,
    state: dict,
    now_monotonic: float,
    attention_after_s: float,
) -> None:
    current = await broker.get_connection_state()
    last = state.get("last_state")

    if current in (ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED):
        if state.get("down_since") is None:
            state["down_since"] = now_monotonic
        if last != current:
            await notifier.send(f"IBKR {current.value.lower()}. No action yet unless this persists.")
            state["attention_sent"] = False
        down_for = now_monotonic - float(state.get("down_since", now_monotonic))
        if down_for >= attention_after_s and not state.get("attention_sent", False):
            await notifier.send(
                "IBKR has been unavailable for more than "
                f"{int(attention_after_s)}s; manual auth may be needed. "
                "Open portfolio-hub and use Retry now or Restart Gateway."
            )
            state["attention_sent"] = True
    elif current is ConnectionState.CONNECTED:
        if last in (ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED):
            await notifier.send("IBKR recovered; portfolio-hub is connected again.")
        state["down_since"] = None
        state["attention_sent"] = False

    state["last_state"] = current


async def monitor_connection(
    broker,
    *,
    notifier,
    interval_s: float = 30.0,
    attention_after_s: float = 120.0,
) -> None:
    state = {"last_state": None, "down_since": None, "attention_sent": False}
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
            _LOG.warning("connection monitor failed: %s", exc)
        await asyncio.sleep(interval_s)
```

- [ ] **Step 4: Start monitor in app lifespan**

In `app/main.py` lifespan setup, after broker startup task creation, add:

```python
from app.core.notifications import build_notifier
from app.jobs.connection_monitor import monitor_connection

notify_task = asyncio.create_task(
    monitor_connection(
        broker,
        notifier=build_notifier(),
        interval_s=float(os.environ.get("CONNECTION_MONITOR_INTERVAL_S", "30")),
        attention_after_s=float(os.environ.get("IBKR_AUTH_ATTENTION_AFTER_S", "120")),
    )
)
notify_task.add_done_callback(_log_loop_crash("connection monitor"))
app.state.connection_monitor_task = notify_task
```

During lifespan shutdown, cancel and await it like the existing background tasks.

- [ ] **Step 5: Add env vars**

Add to `.env.example`:

```dotenv
# Telegram notifications for IBKR reconnect/auth attention.
# Create a bot with BotFather and put the chat/user/group id here.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
CONNECTION_MONITOR_INTERVAL_S=30
IBKR_AUTH_ATTENTION_AFTER_S=120
```

- [ ] **Step 6: Run tests**

Run:

```bash
pytest tests/test_connection_monitor_notifications.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/core/notifications.py app/jobs/connection_monitor.py app/main.py .env.example tests/test_connection_monitor_notifications.py
git commit -m "feat: notify on ibkr auth attention"
```

---

### Task 7: End-To-End Verification

**Files:**
- No new files unless fixing issues found during verification.

- [ ] **Step 1: Run focused auth/reconnect tests**

Run:

```bash
pytest tests/test_ibkr_retry_now.py tests/test_healthz_retry.py tests/test_gateway_control.py tests/test_connection_monitor_notifications.py tests/test_composite_broker.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Run broader reconnect suite**

Run:

```bash
pytest tests/test_reconnect.py tests/test_reconnect_edges.py tests/test_reconnect_multi_cycle.py tests/test_reconnect_resubscribe.py tests/test_reconnect_stale_flag.py tests/test_reconnect_stale_race.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Verify compose config**

Run:

```bash
docker compose config >/tmp/portfolio-hub-compose.yml
rg "READ_ONLY_LOGIN|READ_ONLY_API|AUTO_RESTART_TIME|TWS_COLD_RESTART|TWOFA_EXIT_INTERVAL" /tmp/portfolio-hub-compose.yml
```

Expected: all variables appear under the `ib-gateway` service.

- [ ] **Step 4: Manual dry run without live IBKR**

Run:

```bash
BROKERS_ENABLED=ibkr,futu .venv/bin/python scripts/serve_mock.py
```

Open:

```text
http://127.0.0.1:8765
```

Expected: page still renders and existing mock behavior is unaffected.

- [ ] **Step 5: Manual live reconnect check**

With the real Docker stack running:

```bash
docker compose restart ib-gateway
```

Expected:
- portfolio-hub shows `IBKR reconnecting`.
- Telegram sends one reconnecting alert.
- `Retry now` is visible during reconnecting.
- After Gateway is available, portfolio-hub recovers and Telegram sends one recovery alert.

- [ ] **Step 6: Manual prolonged-down check**

Stop Gateway:

```bash
docker compose stop ib-gateway
```

Wait longer than `IBKR_AUTH_ATTENTION_AFTER_S`.

Expected:
- Telegram sends the attention-needed message.
- portfolio-hub shows retry/restart controls.

Restart:

```bash
docker compose start ib-gateway
```

Expected: recovery alert fires once.

- [ ] **Step 7: Commit any verification fixes**

Only if fixes were needed:

```bash
git add <changed-files>
git commit -m "fix: stabilize ibkr auth resilience flow"
```

---

## Self-Review

- Spec coverage: weekly auth, daily restart, no daily reauth, reliable retry, protected restart, Telegram alerts, and docs/runbook are all covered.
- Placeholder scan: no open placeholders or undefined future tasks remain.
- Type consistency: `retry_now()`, `restart_gateway()`, `build_notifier()`, and monitor function names are consistent across tasks.
- Security check: gateway restart is disabled unless configured, admin-protected, and does not require Docker socket access inside the dashboard.
