"""通知 Adapter(spec 二十三)— 分級 + 去重 + 冷卻;禁止每次跳動都發通知。"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from app.config import get_settings
from app.db.models import Alert
from app.db.session import db_session

logger = logging.getLogger(__name__)

LEVELS = ("INFO", "WATCH", "TRIGGER", "RISK", "MANAGE", "EXIT")


class NotificationChannel(ABC):
    name: str = "base"

    @abstractmethod
    async def send(self, text: str) -> bool: ...


class NotificationManager:
    """去重(同 topic)+ 冷卻(config.notify_cooldown_seconds)。"""

    def __init__(self, channels: list[NotificationChannel]) -> None:
        self.channels = channels
        self._last_sent: dict[str, datetime] = {}

    def _cooldown_ok(self, topic: str, now: datetime) -> bool:
        cooldown = get_settings().notify_cooldown_seconds
        last = self._last_sent.get(topic)
        return last is None or (now - last).total_seconds() >= cooldown

    async def notify(self, level: str, topic: str, message: str, *,
                     bypass_cooldown: bool = False) -> bool:
        """發送分級通知。RISK/EXIT 預設繞過冷卻(安全訊息不可壓抑)。"""
        assert level in LEVELS, f"unknown level {level}"
        now = datetime.now(timezone.utc)
        if level in ("RISK", "EXIT"):
            bypass_cooldown = True
        key = f"{level}:{topic}"
        if not bypass_cooldown and not self._cooldown_ok(key, now):
            logger.debug("notification suppressed by cooldown: %s", key)
            return False

        text = f"[{level}] {message}"
        delivered = False
        for ch in self.channels:
            try:
                delivered = await ch.send(text) or delivered
            except Exception as exc:  # noqa: BLE001
                logger.error("notify via %s failed: %s", ch.name, exc)
        self._last_sent[key] = now
        try:
            with db_session() as db:
                db.add(Alert(level=level, topic=topic, message=message,
                             channel=",".join(c.name for c in self.channels) or "log",
                             sent_at=now, delivered=delivered))
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert log failed: %s", exc)
        return delivered


class LogChannel(NotificationChannel):
    """無 Telegram 設定時的 fallback:寫入 log,確保通知不會無聲消失。"""
    name = "log"

    async def send(self, text: str) -> bool:
        logger.info("NOTIFY %s", text)
        return True
