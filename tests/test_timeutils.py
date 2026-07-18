"""日線 NY 17:00 ET 切分與休市判定(spec 三、四)— 含夏令時間切換測試。"""
from datetime import date, datetime, timezone

from app.utils.timeutils import is_market_open, trading_day, trading_day_bounds


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestTradingDayCut:
    def test_summer_edt_before_cut(self):
        # 2026-07-15 20:59 UTC = 16:59 EDT → 屬 7/15 交易日
        assert trading_day(utc(2026, 7, 15, 20, 59)) == date(2026, 7, 15)

    def test_summer_edt_after_cut(self):
        # 2026-07-15 21:00 UTC = 17:00 EDT → 屬 7/16 交易日
        assert trading_day(utc(2026, 7, 15, 21, 0)) == date(2026, 7, 16)

    def test_winter_est_before_cut(self):
        # 2026-01-15 21:30 UTC = 16:30 EST → 屬 1/15(冬令 UTC-5,不能硬編碼 -4)
        assert trading_day(utc(2026, 1, 15, 21, 30)) == date(2026, 1, 15)

    def test_winter_est_after_cut(self):
        # 2026-01-15 22:00 UTC = 17:00 EST → 屬 1/16
        assert trading_day(utc(2026, 1, 15, 22, 0)) == date(2026, 1, 16)

    def test_bounds_are_24h_and_dst_aware(self):
        # 夏令:7/16 交易日 = 7/15 21:00 UTC → 7/16 21:00 UTC
        o, c = trading_day_bounds(date(2026, 7, 16))
        assert o == utc(2026, 7, 15, 21, 0)
        assert c == utc(2026, 7, 16, 21, 0)
        # 冬令:1/16 交易日 = 1/15 22:00 UTC → 1/16 22:00 UTC
        o, c = trading_day_bounds(date(2026, 1, 16))
        assert o == utc(2026, 1, 15, 22, 0)
        assert c == utc(2026, 1, 16, 22, 0)

    def test_dst_transition_week_consistency(self):
        # 2026 美國夏令時間 3/8 開始:3/6(五)仍為 EST、3/9(一)已是 EDT
        assert trading_day(utc(2026, 3, 6, 21, 30)) == date(2026, 3, 6)   # 16:30 EST
        assert trading_day(utc(2026, 3, 9, 21, 30)) == date(2026, 3, 10)  # 17:30 EDT


class TestMarketOpen:
    def test_saturday_closed(self):
        assert not is_market_open(utc(2026, 7, 18, 12, 0))  # Saturday

    def test_sunday_open_after_18_et(self):
        assert not is_market_open(utc(2026, 7, 19, 21, 30))  # 17:30 ET Sun → 未開盤
        assert is_market_open(utc(2026, 7, 19, 22, 30))      # 18:30 ET Sun → 開盤

    def test_friday_close_at_17_et(self):
        assert is_market_open(utc(2026, 7, 17, 20, 30))      # 16:30 ET Fri
        assert not is_market_open(utc(2026, 7, 17, 21, 30))  # 17:30 ET Fri

    def test_daily_maintenance_break(self):
        # 週三 17:30 EDT = 21:30 UTC → 休市;18:30 EDT 恢復
        assert not is_market_open(utc(2026, 7, 15, 21, 30))
        assert is_market_open(utc(2026, 7, 15, 22, 30))

    def test_holiday_closed(self):
        holidays = {date(2026, 12, 25)}
        assert not is_market_open(utc(2026, 12, 25, 15, 0), holidays)
