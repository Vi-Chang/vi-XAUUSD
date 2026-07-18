"""Telegram 通知(MVP 必做,spec 二十三)。"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.notifications.base import LogChannel, NotificationChannel, NotificationManager

logger = logging.getLogger(__name__)


class TelegramChannel(NotificationChannel):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, text: str) -> bool:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(self._url, json={
                "chat_id": self._chat_id, "text": text[:4000],
                "disable_web_page_preview": True,
            })
            if r.status_code != 200:
                logger.error("telegram send failed: %s %s", r.status_code, r.text[:200])
                return False
            return True


def build_notification_manager() -> NotificationManager:
    """有 Telegram 設定 → Telegram;否則 fallback 到 log channel。"""
    s = get_settings()
    channels: list[NotificationChannel] = []
    if s.telegram_bot_token and s.telegram_chat_id:
        channels.append(TelegramChannel(s.telegram_bot_token, s.telegram_chat_id))
    else:
        channels.append(LogChannel())
    return NotificationManager(channels)
