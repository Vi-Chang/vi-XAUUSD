"""市場行事曆(spec 四)— 假日/提前收市防呆。

內建常見整日休市假日;可經 market_calendar 表擴充。
提前收市日保守處理為整日休市(寧可少分析,不可誤報)。
"""
from __future__ import annotations

from datetime import date, datetime

from app.db.models import MarketCalendarEntry
from app.db.session import db_session
from app.utils.timeutils import is_market_open

# 內建假日(NY 日曆日);每年年底維護一次即可
BUILTIN_HOLIDAYS: dict[date, str] = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 12, 25): "Christmas Day",
    date(2026, 12, 24): "Christmas Eve (early close → 保守整日休市)",
    date(2026, 12, 31): "New Year's Eve (early close → 保守整日休市)",
    date(2027, 1, 1): "New Year's Day",
}


def load_holidays() -> frozenset[date]:
    """內建假日 + DB market_calendar 表。DB 不可用時退回內建清單。"""
    days = set(BUILTIN_HOLIDAYS)
    try:
        with db_session() as db:
            for row in db.query(MarketCalendarEntry).all():
                days.add(row.calendar_date)
    except Exception:  # noqa: BLE001 — 行事曆讀取失敗不應讓分析崩潰,改用內建
        pass
    return frozenset(days)


def market_is_open(now_utc: datetime | None = None) -> bool:
    from datetime import timezone
    now = now_utc or datetime.now(timezone.utc)
    return is_market_open(now, load_holidays())
