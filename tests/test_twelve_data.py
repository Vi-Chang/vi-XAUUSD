"""Twelve Data 主力模式:K 棒邊界快取與共享配額(不打真實 API)。"""
from datetime import datetime, timezone

import pytest

from app.providers.base import QuotaExceededError
from app.providers.twelve_data import LONG_H1_KEY, QuotaTracker, bar_floor, needs_refetch


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestNeedsRefetch:
    def test_first_fetch_always(self):
        assert needs_refetch("15M", None, utc(2026, 7, 20, 10, 0))

    def test_same_bar_uses_cache(self):
        # 10:31 抓過,10:44 仍在同一根 15M(10:30–10:45)→ 用快取
        assert not needs_refetch("15M", utc(2026, 7, 20, 10, 31), utc(2026, 7, 20, 10, 44))

    def test_new_bar_refetches(self):
        assert needs_refetch("15M", utc(2026, 7, 20, 10, 31), utc(2026, 7, 20, 10, 46))

    def test_hourly_bar(self):
        assert not needs_refetch("1H", utc(2026, 7, 20, 10, 5), utc(2026, 7, 20, 10, 55))
        assert needs_refetch("1H", utc(2026, 7, 20, 10, 5), utc(2026, 7, 20, 11, 0))

    def test_long_h1_refreshes_every_6h(self):
        assert not needs_refetch(LONG_H1_KEY, utc(2026, 7, 20, 4, 0), utc(2026, 7, 20, 9, 59))
        assert needs_refetch(LONG_H1_KEY, utc(2026, 7, 20, 4, 0), utc(2026, 7, 20, 10, 0))

    def test_bar_floor(self):
        assert bar_floor(utc(2026, 7, 20, 10, 44, 30), 15) == utc(2026, 7, 20, 10, 30)
        assert bar_floor(utc(2026, 7, 20, 10, 44), 240) == utc(2026, 7, 20, 8, 0)


class TestMarketHoursFilter:
    def test_weekend_zombie_bars_removed(self):
        from datetime import timedelta

        from app.providers.base import Candle
        from app.services.candle_service import filter_market_hours

        def bar(t):
            return Candle(symbol="XAUUSD", timeframe="15M", open_time=t,
                          close_time=t + timedelta(minutes=15), open=4010.0,
                          high=4010.6, low=4010.4, close=4010.5, is_closed=True,
                          data_provider="twelve_data")

        candles = [
            bar(utc(2026, 7, 17, 20, 30)),   # 週五 16:30 ET → 開盤中,保留
            bar(utc(2026, 7, 17, 21, 0)),    # 週五 17:00 ET → 已收盤,剔除
            bar(utc(2026, 7, 18, 11, 15)),   # 週六 → 剔除
            bar(utc(2026, 7, 19, 22, 30)),   # 週日 18:30 ET → 已開盤,保留
        ]
        kept = filter_market_hours(candles)
        assert [c.open_time for c in kept] == [utc(2026, 7, 17, 20, 30),
                                               utc(2026, 7, 19, 22, 30)]


class TestQuota:
    def test_minute_limit(self):
        q = QuotaTracker(daily_limit=100, minute_limit=3)
        for _ in range(3):
            q.check_and_count()
        with pytest.raises(QuotaExceededError):
            q.check_and_count()

    def test_daily_limit(self):
        q = QuotaTracker(daily_limit=2, minute_limit=100)
        q.check_and_count()
        q.check_and_count()
        with pytest.raises(QuotaExceededError):
            q.check_and_count()
        assert q.remaining_today == 0
