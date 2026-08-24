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
        self._edit_url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
        self._chat_id = chat_id

    async def send(self, text: str) -> bool:
        return bool(await self.send_with_receipt(text))

    async def send_with_receipt(self, text: str) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(self._url, json={
                "chat_id": self._chat_id, "text": text[:4000],
                "disable_web_page_preview": True,
            })
            if r.status_code != 200:
                from app.services.secret_sanitizer import sanitize_text
                logger.error("telegram send failed: %s %s", r.status_code,
                             sanitize_text(r.text[:200]))
                return ""
            payload = r.json()
            return str((payload.get("result") or {}).get("message_id") or "")

    async def edit_with_receipt(self, message_id: str, text: str) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(self._edit_url, json={
                "chat_id": self._chat_id, "message_id": message_id,
                "text": text[:4000], "disable_web_page_preview": True,
            })
            if r.status_code != 200:
                from app.services.secret_sanitizer import sanitize_text
                logger.error("telegram edit failed: %s %s", r.status_code,
                             sanitize_text(r.text[:200]))
                return ""
            return str(((r.json().get("result") or {}).get("message_id")) or message_id)


def build_notification_manager() -> NotificationManager:
    """log channel 永遠在(寫檔);有 Telegram 設定時再加 Telegram 推播 channel。"""
    s = get_settings()
    channels: list[NotificationChannel] = [LogChannel()]   # 永遠寫 log
    if s.telegram_bot_token and s.telegram_chat_id:
        channels.append(TelegramChannel(s.telegram_bot_token, s.telegram_chat_id))
    return NotificationManager(channels)
