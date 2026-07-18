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

# 高影響事件中文名稱(spec 十二之優先監控清單);比對採不分大小寫子字串,長鍵優先
EVENT_NAME_ZH: dict[str, str] = {
    "average hourly earnings": "平均時薪",
    "initial jobless claims": "初領失業救濟金人數",
    "consumer confidence": "消費者信心指數",
    "fomc rate decision": "聯準會利率決議",
    "fomc minutes": "聯準會會議紀要",
    "fed chair speech": "聯準會主席談話",
    "nonfarm payrolls": "非農就業人數",
    "unemployment rate": "失業率",
    "ism manufacturing": "ISM 製造業指數",
    "ism services": "ISM 服務業指數",
    "retail sales": "零售銷售",
    "core cpi": "核心消費者物價指數",
    "core pce": "核心個人消費支出物價指數",
    "cpi": "消費者物價指數",
    "ppi": "生產者物價指數",
    "pce": "個人消費支出物價指數",
    "gdp": "國內生產毛額",
}


def translate_event_name(name: str) -> str:
    """事件名稱英翻中;無對應時保留原文。"""
    low = (name or "").lower()
    for key in sorted(EVENT_NAME_ZH, key=len, reverse=True):
        if key in low:
            return EVENT_NAME_ZH[key]
    return name or ""


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
    zh = translate_event_name(ev.get("name", ""))
    state.next_event = f"{zh}({ev.get('country')})"
    state.minutes_remaining = minutes
    impact = str(ev.get("impact", "")).upper()
    if impact == "HIGH" and minutes <= s.event_lockout_minutes:
        state.level = "HIGH"
        state.event_lockout = True
        state.reason = (f"高影響事件「{zh}」距離公布僅 {minutes} 分鐘,已進入事件鎖定:"
                        f"禁止建立新倉;公布後需等待至少一根 15 分鐘 K 棒收線、"
                        f"點差與波動恢復正常,才恢復劇本評估")
    elif impact == "HIGH" and minutes <= 240:
        state.level = "MEDIUM"
        state.reason = (f"高影響事件「{zh}」約 {minutes // 60} 小時 {minutes % 60} 分鐘後公布;"
                        f"事件前 {s.event_lockout_minutes} 分鐘將自動鎖定新倉,"
                        f"接近公布時段請避免持有過大部位")
    else:
        state.level = "LOW"
        state.reason = ("距離下一個已知高影響事件仍有充足緩衝,事件風險低;"
                        "系統將於事件前自動進入鎖定")
    return state
