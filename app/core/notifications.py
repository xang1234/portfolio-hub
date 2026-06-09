"""Notification sinks for operational alerts."""

import os
from typing import Protocol

import httpx


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class NullNotifier:
    """Notifier used when alerting is not configured."""

    async def send(self, text: str) -> None:
        return None


class NotificationSendError(RuntimeError):
    """Safe-to-log notification delivery failure."""


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError:
            raise NotificationSendError("telegram notification failed") from None


def build_notifier() -> Notifier:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        return NullNotifier()
    return TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
