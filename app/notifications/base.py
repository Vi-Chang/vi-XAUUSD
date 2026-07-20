"""通知 Adapter(spec 二十三)— 分級 + 去重 + 冷卻;禁止每次跳動都發通知。

分級通知(三層設計):
- 每則通知有「類別」(level:INFO/WATCH/TRIGGER/RISK/MANAGE/EXIT)與
  「嚴重度」(severity:DEBUG<INFO<WARN<ERROR)。
- NOTIFY_LEVEL 門檻:嚴重度低於門檻只寫 log(不推 Telegram);達到才推播。
  預設 WARN → 一切正常時手機不響,只有資料延遲/異常才響(靜默 heartbeat)。
- ERROR 會加醒目前綴(可選 TELEGRAM_MENTION 標記 @you)。
- log channel 永遠寫入(is_push=False);Telegram 為推播 channel(受門檻控管)。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from app.config import get_settings
from app.db.models import Alert
from app.db.session import db_session

logger = logging.getLogger(__name__)

LEVELS = ("INFO", "WATCH", "TRIGGER", "RISK", "MANAGE", "EXIT")

SEVERITY_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
# 類別 → 預設嚴重度(呼叫端可用 severity 覆寫)
LEVEL_SEVERITY = {
    "INFO": "INFO", "WATCH": "INFO",
    "TRIGGER": "WARN", "MANAGE": "WARN",
    "RISK": "ERROR", "EXIT": "ERROR",
}


class NotificationChannel(ABC):
    name: str = "base"
    is_push: bool = True   # True=受 NOTIFY_LEVEL 門檻控管(如 Telegram);False=永遠寫入(log)

    @abstractmethod
    async def send(self, text: str) -> bool: ...


class NotificationManager:
    """分級門檻 + 去重(同 topic)+ 冷卻(config.notify_cooldown_seconds)。"""

    def __init__(self, channels: list[NotificationChannel]) -> None:
        self.channels = channels
        self._last_sent: dict[str, datetime] = {}

    def _cooldown_ok(self, topic: str, now: datetime) -> bool:
        cooldown = get_settings().notify_cooldown_seconds
        last = self._last_sent.get(topic)
        return last is None or (now - last).total_seconds() >= cooldown

    async def notify(self, level: str, topic: str, message: str, *,
                     severity: str | None = None, bypass_cooldown: bool = False,
                     force_push: bool = False, mention: bool | None = None) -> bool:
        """發送分級通知。

        severity:覆寫類別預設嚴重度(如 data_lag 用 RISK 類別但只算 WARN)。
        force_push:忽略 NOTIFY_LEVEL 門檻強制推播(每日摘要用)。
        回傳是否有推播到任一 push channel。
        """
        assert level in LEVELS, f"unknown level {level}"
        s = get_settings()
        sev = (severity or LEVEL_SEVERITY.get(level, "INFO")).upper()
        now = datetime.now(timezone.utc)
        if level in ("RISK", "EXIT"):
            bypass_cooldown = True
        key = f"{level}:{topic}"
        if not bypass_cooldown and not self._cooldown_ok(key, now):
            logger.debug("notification suppressed by cooldown: %s", key)
            return False

        threshold = SEVERITY_ORDER.get(s.notify_level.upper(), 2)
        allow_push = force_push or SEVERITY_ORDER.get(sev, 1) >= threshold
        if mention is None:
            mention = sev == "ERROR"

        prefix = "🔴 " if sev == "ERROR" else ("🟠 " if sev == "WARN" else "")
        text = f"{prefix}[{sev}] {message}"
        if mention and s.telegram_mention:
            text = f"{s.telegram_mention} {text}"

        # log channel 永遠寫;push channel(Telegram)僅在達門檻時發
        delivered = False
        for ch in self.channels:
            if getattr(ch, "is_push", True) and not allow_push:
                continue
            try:
                delivered = await ch.send(text) or delivered
            except Exception as exc:  # noqa: BLE001
                logger.error("notify via %s failed: %s", ch.name, exc)

        logger.info("NOTIFY sev=%s level=%s topic=%s pushed=%s", sev, level, topic, allow_push)
        self._last_sent[key] = now
        try:
            with db_session() as db:
                db.add(Alert(level=level, topic=topic, message=f"[{sev}] {message}",
                             channel=",".join(c.name for c in self.channels) or "log",
                             sent_at=now, delivered=delivered))
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert log failed: %s", exc)
        return delivered


class LogChannel(NotificationChannel):
    """永遠寫入 log(is_push=False),確保通知不會無聲消失。"""
    name = "log"
    is_push = False

    async def send(self, text: str) -> bool:
        logger.info("NOTIFY %s", text)
        return True
