import asyncio
import traceback

import pytest

from app.core.broker import ConnectionState


class _FakeBroker:
    def __init__(self, state: ConnectionState):
        self.state = state

    async def get_connection_state(self) -> ConnectionState:
        return self.state


class _CollectingNotifier:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


class _FailOnceNotifier:
    def __init__(self):
        self.messages: list[str] = []
        self.fail_next = True

    async def send(self, text: str) -> None:
        self.messages.append(text)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("accepted then timed out")


class _CompositeLikeBroker:
    name = "Composite"

    def __init__(self, adapters):
        self.adapters = tuple(adapters)


async def test_reconnecting_transition_sends_one_message():
    from app.jobs.connection_monitor import poll_connection_once

    broker = _FakeBroker(ConnectionState.CONNECTED)
    notifier = _CollectingNotifier()
    state: dict = {}

    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=0.0,
        attention_after_s=120.0,
    )

    broker.state = ConnectionState.RECONNECTING
    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=5.0,
        attention_after_s=120.0,
    )

    assert len(notifier.messages) == 1
    assert "IBKR reconnecting" in notifier.messages[0]


async def test_transition_state_advances_when_notifier_fails_once():
    from app.jobs.connection_monitor import poll_connection_once

    broker = _FakeBroker(ConnectionState.RECONNECTING)
    notifier = _FailOnceNotifier()
    state: dict = {}

    with pytest.raises(RuntimeError, match="accepted then timed out"):
        await poll_connection_once(
            broker,
            notifier=notifier,
            state=state,
            now_monotonic=0.0,
            attention_after_s=120.0,
        )

    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=10.0,
        attention_after_s=120.0,
    )

    assert notifier.messages == ["IBKR reconnecting..."]
    assert state["last_state"] is ConnectionState.RECONNECTING


async def test_long_unavailable_threshold_sends_one_attention_message():
    from app.jobs.connection_monitor import poll_connection_once

    broker = _FakeBroker(ConnectionState.RECONNECTING)
    notifier = _CollectingNotifier()
    state: dict = {}

    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=0.0,
        attention_after_s=120.0,
    )
    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=121.0,
        attention_after_s=120.0,
    )
    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=240.0,
        attention_after_s=120.0,
    )

    attention_messages = [
        msg for msg in notifier.messages if "manual auth may be needed" in msg
    ]
    assert len(attention_messages) == 1
    assert "dashboard retry/restart" in attention_messages[0]


async def test_attention_state_advances_when_notifier_fails_once():
    from app.jobs.connection_monitor import poll_connection_once

    broker = _FakeBroker(ConnectionState.RECONNECTING)
    notifier = _FailOnceNotifier()
    state = {
        "last_state": ConnectionState.RECONNECTING,
        "down_since": 0.0,
        "attention_sent": False,
    }

    with pytest.raises(RuntimeError, match="accepted then timed out"):
        await poll_connection_once(
            broker,
            notifier=notifier,
            state=state,
            now_monotonic=130.0,
            attention_after_s=120.0,
        )

    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=240.0,
        attention_after_s=120.0,
    )

    assert len(notifier.messages) == 1
    assert "manual auth may be needed" in notifier.messages[0]
    assert state["attention_sent"] is True


async def test_recovery_sends_one_message_and_resets_down_state():
    from app.jobs.connection_monitor import poll_connection_once

    broker = _FakeBroker(ConnectionState.DISCONNECTED)
    notifier = _CollectingNotifier()
    state: dict = {}

    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=0.0,
        attention_after_s=120.0,
    )

    broker.state = ConnectionState.CONNECTED
    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=10.0,
        attention_after_s=120.0,
    )
    await poll_connection_once(
        broker,
        notifier=notifier,
        state=state,
        now_monotonic=20.0,
        attention_after_s=120.0,
    )

    recovery_messages = [msg for msg in notifier.messages if "IBKR recovered" in msg]
    assert len(recovery_messages) == 1
    assert state.get("down_since") is None
    assert state.get("attention_sent") is False


async def test_repeated_same_state_poll_does_not_duplicate_transition_message():
    from app.jobs.connection_monitor import poll_connection_once

    broker = _FakeBroker(ConnectionState.RECONNECTING)
    notifier = _CollectingNotifier()
    state: dict = {}

    for now in (0.0, 10.0, 20.0):
        await poll_connection_once(
            broker,
            notifier=notifier,
            state=state,
            now_monotonic=now,
            attention_after_s=120.0,
        )

    assert len(notifier.messages) == 1
    assert "IBKR reconnecting" in notifier.messages[0]


def test_build_notifier_returns_null_notifier_when_env_missing(monkeypatch):
    from app.core.notifications import NullNotifier, build_notifier

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert isinstance(build_notifier(), NullNotifier)


def test_build_notifier_returns_telegram_notifier_when_env_present(monkeypatch):
    from app.core.notifications import TelegramNotifier, build_notifier

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  token-123  ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "  chat-456  ")

    notifier = build_notifier()

    assert isinstance(notifier, TelegramNotifier)
    assert notifier.bot_token == "token-123"
    assert notifier.chat_id == "chat-456"


async def test_telegram_error_is_safe_to_log(monkeypatch):
    import httpx

    from app.core.notifications import NotificationSendError, TelegramNotifier

    class _FailingClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, json):
            request = httpx.Request("POST", url)
            return httpx.Response(400, request=request)

    monkeypatch.setattr("app.core.notifications.httpx.AsyncClient", _FailingClient)

    notifier = TelegramNotifier(bot_token="secret-token", chat_id="chat-123")
    with pytest.raises(NotificationSendError) as excinfo:
        await notifier.send("hello")

    formatted = "".join(
        traceback.format_exception(
            excinfo.type,
            excinfo.value,
            excinfo.value.__traceback__,
        )
    )

    assert "secret-token" not in str(excinfo.value)
    assert "secret-token" not in formatted
    assert str(excinfo.value) == "telegram notification failed"


def test_env_float_rejects_non_finite_values(monkeypatch):
    from app.main import _env_float

    monkeypatch.setenv("CONNECTION_MONITOR_INTERVAL_S", "nan")
    assert _env_float("CONNECTION_MONITOR_INTERVAL_S", 30.0) == 30.0

    monkeypatch.setenv("CONNECTION_MONITOR_INTERVAL_S", "inf")
    assert _env_float("CONNECTION_MONITOR_INTERVAL_S", 30.0) == 30.0


def test_ibkr_monitor_broker_selects_ibkr_child_from_composite():
    from app.main import _ibkr_monitor_broker

    ibkr = _FakeBroker(ConnectionState.CONNECTED)
    ibkr.name = "IBKR"
    futu = _FakeBroker(ConnectionState.DISCONNECTED)
    futu.name = "Futu"
    composite = _CompositeLikeBroker([futu, ibkr])

    assert _ibkr_monitor_broker(composite) is ibkr


def test_ibkr_monitor_broker_skips_composite_without_ibkr():
    from app.main import _ibkr_monitor_broker

    futu = _FakeBroker(ConnectionState.DISCONNECTED)
    futu.name = "Futu"

    assert _ibkr_monitor_broker(_CompositeLikeBroker([futu])) is None


async def test_monitor_loop_cancellation_reraises_cleanly():
    from app.jobs.connection_monitor import monitor_connection

    broker = _FakeBroker(ConnectionState.CONNECTED)
    notifier = _CollectingNotifier()

    task = asyncio.create_task(
        monitor_connection(broker, notifier=notifier, interval_s=60.0)
    )
    await asyncio.sleep(0)
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("monitor_connection should re-raise cancellation")
