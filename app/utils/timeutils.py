"""時區與交易日切分(spec 三)。

強制規則:
- 所有市場資料以 UTC 儲存;顯示預設 Asia/Taipei。
- 日線/週線一律以紐約 17:00 ET 切分,使用 zoneinfo 的 America/New_York,
  自動處理 EDT/EST 夏令時間,禁止硬編碼 UTC-4 / UTC-5。
- 休市判定:週五 17:00 ET 收盤 → 週日 18:00 ET 開盤;每日 17:00–18:00 ET 維護休市。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
TAIPEI = ZoneInfo("Asia/Taipei")
UTC = timezone.utc

DAILY_CUT_HOUR = 17   # NY 17:00 ET = 日線切分 / 收盤
DAILY_OPEN_HOUR = 18  # 每日(與週日)18:00 ET 重新開盤


def ensure_utc(dt: datetime) -> datetime:
    """補上/轉換為 UTC tzinfo;naive datetime 視為 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_taipei(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(TAIPEI)


def trading_day(dt_utc: datetime) -> date:
    """回傳 UTC 時間所屬的「交易日」(以 NY 17:00 ET 切分)。

    NY 當地時間 >= 17:00 的行情歸屬「次一日曆日」的交易日,
    與 OANDA dailyAlignment=17 / TradingView(OANDA 資料源)一致。
    """
    ny = ensure_utc(dt_utc).astimezone(NY)
    d = ny.date()
    if ny.hour >= DAILY_CUT_HOUR:
        d += timedelta(days=1)
    return d


def trading_day_bounds(day: date) -> tuple[datetime, datetime]:
    """交易日 D 的 (open_utc, close_utc):前一日曆日 17:00 ET → 當日 17:00 ET。"""
    open_ny = datetime.combine(day - timedelta(days=1), time(DAILY_CUT_HOUR), tzinfo=NY)
    close_ny = datetime.combine(day, time(DAILY_CUT_HOUR), tzinfo=NY)
    return open_ny.astimezone(UTC), close_ny.astimezone(UTC)


def trading_week_start(day: date) -> date:
    """交易週以週一交易日為首(週一交易日 = 週日 17:00 ET 起算)。"""
    return day - timedelta(days=day.weekday())


def is_market_open(dt_utc: datetime, holidays: frozenset[date] | set[date] = frozenset()) -> bool:
    """XAUUSD 是否處於交易時段(spec 四:market_calendar 防呆)。

    - 週五 17:00 ET 之後 → 休市,直到週日 18:00 ET。
    - 平日每天 17:00–18:00 ET 為維護休市。
    - holidays:以「NY 日曆日」表示的假日(整日休市);提前收市由假日表處理為整日保守休市。
    """
    ny = ensure_utc(dt_utc).astimezone(NY)
    wd = ny.weekday()  # Mon=0 .. Sun=6
    if ny.date() in holidays:
        return False
    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday:18:00 ET 開盤
        return ny.hour >= DAILY_OPEN_HOUR
    if wd == 4:  # Friday:17:00 ET 收盤
        return ny.hour < DAILY_CUT_HOUR
    # Mon–Thu:每日 17:00–18:00 維護休市
    return not (DAILY_CUT_HOUR <= ny.hour < DAILY_OPEN_HOUR)


TIMEFRAME_MINUTES: dict[str, int] = {
    "5M": 5, "15M": 15, "30M": 30, "1H": 60, "4H": 240,
    # 1D / 1W 不以固定分鐘處理(依 NY 17:00 切分),此表僅供盤中週期使用
}


def expected_candle_open_times(start_utc: datetime, end_utc: datetime, timeframe: str,
                               holidays: frozenset[date] | set[date] = frozenset()) -> list[datetime]:
    """列出 [start, end) 之間、交易時段內應存在的 K 棒 open_time(UTC)。

    用於缺漏 K 棒檢查;休市時段(含週末、假日、每日維護)不計入,
    避免把休市誤報為資料缺漏(spec 四)。
    """
    minutes = TIMEFRAME_MINUTES[timeframe]
    start = ensure_utc(start_utc)
    end = ensure_utc(end_utc)
    # 對齊到該週期的整數邊界(以 UTC 對齊;盤中週期 OANDA 亦以 UTC 對齊)
    aligned = start - timedelta(minutes=start.minute % minutes,
                                seconds=start.second, microseconds=start.microsecond)
    if minutes >= 60:
        aligned = aligned.replace(minute=0)
        offset_h = aligned.hour % (minutes // 60)
        aligned -= timedelta(hours=offset_h)
    out: list[datetime] = []
    t = aligned
    while t < end:
        if t >= start and is_market_open(t, holidays):
            out.append(t)
        t += timedelta(minutes=minutes)
    return out
