"""經濟事件風險：官方 BLS 行事曆快取優先，手動清單作為後備。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from app.config import get_settings

logger = logging.getLogger(__name__)

EVENT_NAME_ZH: dict[str, str] = {
    "consumer price index": "消費者物價指數",
    "employment situation": "就業報告／非農",
    "producer price index": "生產者物價指數(PPI)",
    "employment cost index": "就業成本指數",
    "fomc rate decision": "聯準會利率決議",
    "fomc minutes": "聯準會會議紀要",
    "nonfarm payrolls": "非農就業人數",
    "core cpi": "核心消費者物價指數",
    "core pce": "核心PCE",
    "personal income and outlays": "個人所得與支出／PCE",
    "cpi": "消費者物價指數",
    "ppi": "生產者物價指數(PPI)",
    "pce": "個人消費支出(PCE)",
    "gdp": "國內生產毛額(GDP)",
}

_HIGH_IMPACT_TERMS = tuple(EVENT_NAME_ZH)


def translate_event_name(name: str) -> str:
    low = (name or "").lower()
    for key in sorted(EVENT_NAME_ZH, key=len, reverse=True):
        if key in low:
            return EVENT_NAME_ZH[key]
    return name or ""


@dataclass
class EventRiskState:
    event_impact: str = "UNKNOWN"
    time_risk: str = "UNKNOWN"
    level: str = "UNKNOWN"
    event_lockout: bool = False
    next_event: str = ""
    minutes_remaining: int | None = None
    source: str = "none"
    reason: str = ""
    manual_file_stale: bool = False
    data_stale: bool = True
    data_updated_at: str = ""
    event_phase: str = "unknown"
    post_event_wait: bool = False
    actual: float | None = None
    forecast: float | None = None
    previous: float | None = None
    outcome_status: str = "not_available"
    outcome_source: str = ""


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _read_catalog(path: Path) -> tuple[list[dict], datetime]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("events", []), _parse_time(data.get("updated_at", "1970-01-01T00:00:00Z"))


def load_manual_events() -> tuple[list[dict], bool]:
    s = get_settings()
    path = Path(s.manual_events_path)
    if not path.exists():
        return [], True
    events, updated = _read_catalog(path)
    return events, (datetime.now(timezone.utc) - updated).days > s.manual_events_stale_days


def _unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_bls_ics(text: str) -> list[dict]:
    """只保留會顯著影響黃金的 BLS 高影響發布，避免把低相關數據混入。"""
    events: list[dict] = []
    item: dict[str, str] | None = None
    for line in _unfold_ics(text):
        if line == "BEGIN:VEVENT":
            item = {}
        elif line == "END:VEVENT" and item:
            summary = item.get("SUMMARY", "")
            if any(term in summary.lower() for term in _HIGH_IMPACT_TERMS):
                stamp = item.get("DTSTART", "")
                if stamp.endswith("Z") and len(stamp) >= 16:
                    dt = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                    events.append({"name": summary, "country": "US",
                                   "time_utc": dt.isoformat().replace("+00:00", "Z"),
                                   "impact": "HIGH", "source": "BLS"})
            item = None
        elif item is not None and ":" in line:
            key, value = line.split(":", 1)
            item[key.split(";", 1)[0]] = value.strip()
    return events


_MONTHS = {name: number for number, name in enumerate((
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December"), start=1)}


def parse_fomc_calendar(html: str, year: int) -> list[dict]:
    """從 Fed 官方 FOMC 年度日程擷取會議最後一天的決議時間。

    Fed 的利率決議通常於會議最後日美東 14:00 發布；使用 America/New_York
    換算 UTC，避免夏令時間把事件錯排一小時。只採用該年度段落，避免歷史日程混入。
    """
    plain = re.sub(r"<[^>]+>", " ", html)
    plain = re.sub(r"\s+", " ", plain)
    marker = f"{year} FOMC Meetings"
    start = plain.find(marker)
    if start < 0:
        return []
    end = plain.find(f"{year - 1} FOMC Meetings", start + len(marker))
    section = plain[start:end if end >= 0 else None]
    pattern = re.compile(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?", re.IGNORECASE)
    eastern = ZoneInfo("America/New_York")
    events: list[dict] = []
    for match in pattern.finditer(section):
        month = _MONTHS[match.group(1).title()]
        day = int(match.group(3) or match.group(2))
        local = datetime(year, month, day, 14, 0, tzinfo=eastern)
        events.append({"name": "FOMC Rate Decision", "country": "US", "impact": "HIGH",
                       "time_utc": local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                       "source": "Federal Reserve"})
    return events


def parse_bea_schedule(html: str, year: int) -> list[dict]:
    """擷取 BEA 官方發布表的 GDP 與 Personal Income and Outlays（含 PCE）。"""
    plain = re.sub(r"<[^>]+>", " ", html)
    plain = re.sub(r"\s+", " ", plain)
    eastern = ZoneInfo("America/New_York")
    date_prefix = (
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2})\s+(\d{1,2}):(\d{2})\s*(AM|PM)"
    )
    matches = list(re.finditer(date_prefix, plain, re.IGNORECASE))
    events: list[dict] = []
    for index, match in enumerate(matches):
        title = plain[match.end(): matches[index + 1].start() if index + 1 < len(matches) else None]
        title = re.sub(r"^(?:\s*\|?\s*[NDA]\s*\|?\s*)+", "", title).strip(" |")
        is_pce = "personal income and outlays" in title.lower()
        is_gdp = bool(re.search(r"(?:^|\s)GDP\s*\(|Gross Domestic Product,", title, re.IGNORECASE))
        if not (is_pce or is_gdp):
            continue
        hour = int(match.group(3)) % 12 + (12 if match.group(5).upper() == "PM" else 0)
        local = datetime(year, _MONTHS[match.group(1).title()], int(match.group(2)), hour,
                         int(match.group(4)), tzinfo=eastern)
        name = "Personal Income and Outlays (PCE)" if is_pce else "GDP"
        events.append({"name": name, "country": "US", "impact": "HIGH",
                       "time_utc": local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                       "source": "BEA"})
    return events


def _write_catalog(path: Path, events: list[dict], now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": now.isoformat().replace("+00:00", "Z"), "source": "BLS",
               "events": events}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_official_events(now: datetime | None = None, *, fetcher=None) -> tuple[list[dict], bool, str]:
    """讀取快取；只有快取過期才請求官方 BLS iCalendar，失敗時絕不覆蓋舊快取。"""
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    path = Path(s.official_events_cache_path)
    events: list[dict] = []
    updated: datetime | None = None
    try:
        events, updated = _read_catalog(path)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    fresh = bool(updated and now - updated <= timedelta(hours=s.official_events_cache_hours))
    if fresh or not s.official_event_sync_enabled or s.app_env == "test":
        return events, not fresh, updated.isoformat() if updated else ""
    def fetch(url: str) -> str:
        if fetcher is not None:
            return fetcher(url)
        request = Request(url, headers={"User-Agent": "XAUUSD-event-risk/1.0"})
        with urlopen(request, timeout=s.official_events_timeout_seconds) as response:  # nosec B310
            return response.read().decode("utf-8")

    # 每個官方來源獨立容錯：Fed／BEA 暫時失效時，仍保留 BLS 等可用來源，
    # 不讓單一網頁格式變動導致整個事件風險面板退回過期手動資料。
    parsed: list[dict] = []
    failed_sources: list[str] = []
    source_jobs = (
        ("BLS", s.official_events_url, parse_bls_ics),
        ("Federal Reserve", s.official_fomc_events_url,
         lambda raw: parse_fomc_calendar(raw, now.year)),
        ("BEA", s.official_bea_events_url, lambda raw: parse_bea_schedule(raw, now.year)),
    )
    for source_name, url, parser in source_jobs:
        try:
            source_events = parser(fetch(url))
            if not source_events:
                raise ValueError("no supported high-impact events")
            parsed.extend(source_events)
        except Exception as exc:  # noqa: BLE001
            failed_sources.append(source_name)
            logger.warning("official %s calendar refresh failed: %s", source_name, exc)

    parsed = list({(item["name"], item["time_utc"]): item for item in parsed}.values())
    if not parsed:
        return events, True, updated.isoformat() if updated else ""
    if failed_sources:
        logger.warning("official event sources partially unavailable: %s", ", ".join(failed_sources))
    _write_catalog(path, parsed, now)
    return parsed, False, now.isoformat()


def _event_time(event: dict) -> datetime | None:
    try:
        return _parse_time(str(event["time_utc"]))
    except (KeyError, TypeError, ValueError):
        return None


def _number(value: object) -> float | None:
    """Parse a provider value without treating missing data as zero."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


def _apply_outcome(state: EventRiskState, event: dict) -> None:
    """Attach only provider-supplied event values; price action never fills them."""
    state.actual = _number(event.get("actual"))
    state.forecast = _number(event.get("forecast"))
    state.previous = _number(event.get("previous"))
    state.outcome_source = str(event.get("outcome_source") or "")
    if state.actual is not None and state.forecast is not None:
        state.outcome_status = "available"
    elif state.forecast is not None or state.previous is not None:
        state.outcome_status = "pending"


def evaluate_event_risk(now: datetime | None = None) -> EventRiskState:
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    official, official_stale, official_updated = load_official_events(now)
    try:
        manual, manual_stale = load_manual_events()
    except Exception as exc:  # noqa: BLE001
        logger.warning("manual event calendar read failed: %s", exc)
        manual, manual_stale = [], True

    events = official or manual
    source = "official" if official else "manual" if manual else "none"
    source_stale = official_stale if official else manual_stale
    updated_at = official_updated
    if not official and manual:
        try:
            _, updated = _read_catalog(Path(s.manual_events_path))
            updated_at = updated.isoformat()
        except (OSError, ValueError, json.JSONDecodeError):
            updated_at = ""

    state = EventRiskState(source=source, manual_file_stale=manual_stale,
                           data_stale=source_stale, data_updated_at=updated_at)
    valid = [(t, event) for event in events if (t := _event_time(event))]
    upcoming = sorted((pair for pair in valid if pair[0] >= now), key=lambda pair: pair[0])
    recent = sorted((pair for pair in valid if pair[0] < now), key=lambda pair: pair[0], reverse=True)
    post_window = timedelta(minutes=s.event_post_lockout_minutes)
    post = next(((t, event) for t, event in recent
                 if str(event.get("impact", "")).upper() == "HIGH" and now - t < post_window), None)

    if post:
        t, event = post
        elapsed = max(0, int((now - t).total_seconds() // 60))
        name = translate_event_name(str(event.get("name", "")))
        state.event_impact = "HIGH"
        state.time_risk = state.level = "HIGH"
        state.event_lockout = state.post_event_wait = True
        state.event_phase = "post_release"
        state.next_event = f"{name}({event.get('country', 'US')})"
        state.minutes_remaining = max(0, s.event_post_lockout_minutes - elapsed)
        state.reason = (f"{state.next_event} 已公布 {elapsed} 分鐘；等待至少 "
                        f"{s.event_post_lockout_minutes} 分鐘及一根已收盤 15 分K確認，暫停新倉與追價。")
        _apply_outcome(state, event)
        return state

    if not upcoming:
        state.time_risk = state.level = "LOW" if not source_stale else "UNKNOWN"
        state.reason = ("事件資料缺失或過期，目前分析僅依技術面；不輸出低事件風險結論。"
                        if source_stale else "官方事件行事曆正常，暫無近期高影響美國數據。")
        return state

    t, event = upcoming[0]
    minutes = max(0, int((t - now).total_seconds() // 60))
    state.next_event = f"{translate_event_name(str(event.get('name', '')))}({event.get('country', 'US')})"
    _apply_outcome(state, event)
    state.minutes_remaining = minutes
    state.event_phase = "upcoming"
    state.event_impact = str(event.get("impact", "UNKNOWN")).upper()
    state.time_risk = "HIGH" if minutes <= s.event_lockout_minutes else "MEDIUM" if minutes <= 240 else "LOW"
    state.level = state.time_risk
    state.event_lockout = state.event_impact == "HIGH" and minutes <= s.event_lockout_minutes
    if source_stale:
        state.reason = "官方事件資料已過期；僅供風險提示，不可視為完整事件覆蓋。"
    elif state.event_lockout:
        state.reason = (f"{state.next_event} 將於 {minutes} 分鐘後公布；進入事件凍結區，"
                        "暫停新倉與追價，等待發布後 15 分K收盤確認。")
    elif state.event_impact != "HIGH":
        state.reason = (f"下一項低影響事件：{state.next_event}，約 {minutes} 分鐘後；"
                        "不觸發鎖定，但仍保留時間風險提示。")
    elif state.time_risk == "LOW":
        state.reason = (f"下一項高影響事件：{state.next_event}，約 {minutes} 分鐘後；"
                        f"目前緩衝充足，公布前 {s.event_lockout_minutes} 分鐘將自動凍結新倉。")
    else:
        state.reason = (f"下一項 {state.event_impact} 影響事件：{state.next_event}，"
                        f"約 {minutes} 分鐘後；公布前 {s.event_lockout_minutes} 分鐘將自動凍結新倉。")
    return state
