"""經濟事件服務(MVP:僅 manual_events.json fallback;Phase 6 加 Finnhub/FMP)。

- 事件前 EVENT_LOCKOUT_MINUTES 內 → 鎖定新倉(spec 十二)。
- 所有來源失效 → EVENT_RISK_UNKNOWN,降低分析信心。
- manual_events.json 超過 7 天未更新 → 提醒使用者。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class EventRiskState:
    level: str = "UNKNOWN"          # LOW/MEDIUM/HIGH/UNKNOWN
    event_lockout: bool = False
    next_event: str = ""
    minutes_remaining: int | None = None
    source: str = "none"            # finnhub/fmp/manual/none
    reason: str = ""
    manual_file_stale: bool = False


def load_manual_events() -> tuple[list[dict], bool]:
    """讀取 data/manual_events.json;回傳 (events, file_stale)。"""
    s = get_settings()
    path = Path(s.manual_events_path)
    if not path.exists():
        return [], True
    data = json.loads(path.read_text(encoding="utf-8"))
    updated = datetime.fromisoformat(data.get("updated_at", "1970-01-01T00:00:00Z")
                                     .replace("Z", "+00:00"))
    stale = (datetime.now(timezone.utc) - updated).days > s.manual_events_stale_days
    return data.get("events", []), stale


def evaluate_event_risk(now: datetime | None = None) -> EventRiskState:
    """MVP:只用 manual fallback。Phase 6 於此函式前插入 Finnhub → FMP 鏈。"""
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    try:
        events, stale = load_manual_events()
    except Exception as exc:  # noqa: BLE001
        logger.warning("manual_events.json 讀取失敗: %s", exc)
        return EventRiskState(reason="所有經濟事件來源失效 → EVENT_RISK_UNKNOWN,降低信心")

    upcoming = []
    for ev in events:
        try:
            t = datetime.fromisoformat(ev["time_utc"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if t >= now:
            upcoming.append((t, ev))
    upcoming.sort()

    state = EventRiskState(source="manual", manual_file_stale=stale)
    if stale:
        state.reason = f"manual_events.json 超過 {s.manual_events_stale_days} 天未更新,請更新本週事件"
    if not upcoming:
        state.level = "LOW" if not stale else "UNKNOWN"
        return state

    t, ev = upcoming[0]
    minutes = int((t - now).total_seconds() // 60)
    state.next_event = f"{ev.get('name')} ({ev.get('country')})"
    state.minutes_remaining = minutes
    impact = str(ev.get("impact", "")).upper()
    if impact == "HIGH" and minutes <= s.event_lockout_minutes:
        state.level = "HIGH"
        state.event_lockout = True
        state.reason = f"高影響事件 {state.next_event} 距離 {minutes} 分鐘 → EVENT_LOCKOUT"
    elif impact == "HIGH" and minutes <= 240:
        state.level = "MEDIUM"
    else:
        state.level = "LOW"
    return state
