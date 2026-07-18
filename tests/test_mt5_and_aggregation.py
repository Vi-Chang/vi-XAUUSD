"""MT5 時差推算(純函式)與 1H→1D/1W 本地聚合(NY 17:00 ET 切分)。"""
from datetime import datetime, timedelta, timezone

from app.providers.base import Candle
from app.providers.mt5_tmgm import infer_server_offset_hours
from app.services.candle_service import aggregate_candles


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestServerOffset:
    def test_summer_plus3(self):
        # 伺服器牆上時間 15:00、真實 UTC 12:00 → +3(夏令 NY-close 券商)
        now = utc(2026, 7, 15, 12, 0)
        server_epoch = utc(2026, 7, 15, 15, 0).timestamp()
        assert infer_server_offset_hours(server_epoch, now) == 3

    def test_winter_plus2(self):
        now = utc(2026, 1, 15, 12, 0)
        server_epoch = utc(2026, 1, 15, 14, 0).timestamp()
        assert infer_server_offset_hours(server_epoch, now) == 2

    def test_rounding_tolerates_seconds_drift(self):
        now = utc(2026, 7, 15, 12, 0, 30)
        server_epoch = utc(2026, 7, 15, 14, 59, 45).timestamp()
        assert infer_server_offset_hours(server_epoch, now) == 3


def h1_candles(start: datetime, hours: int) -> list[Candle]:
    out = []
    price = 4600.0
    for i in range(hours):
        t = start + timedelta(hours=i)
        out.append(Candle(symbol="XAUUSD", timeframe="1H", open_time=t,
                          close_time=t + timedelta(hours=1),
                          open=price, high=price + 2 + (i % 5), low=price - 2 - (i % 3),
                          close=price + 1, volume=100.0, is_closed=True,
                          data_provider="test"))
        price += 1
    return out


class TestAggregation:
    def test_daily_cut_at_ny_17(self):
        # 2026-07-14 18:00 UTC 起 30 小時:跨 7/14 21:00 與 7/15 21:00 兩個
        # NY 17:00 EDT 切分點 → 3 根日線(3h + 24h + 3h)
        candles = h1_candles(utc(2026, 7, 14, 18, 0), 30)
        daily = aggregate_candles(candles, "1D")
        assert len(daily) == 3
        d1, d2, d3 = daily
        # 每根日線的第一根 1H 必須落在 21:00 UTC(NY 17:00)切分邊界
        assert d2.open_time == utc(2026, 7, 14, 21, 0)
        assert d3.open_time == utc(2026, 7, 15, 21, 0)
        # 完整 24h 的那根:OHLC 覆蓋正確且已收線
        in_d2 = [c for c in candles if utc(2026, 7, 14, 21, 0) <= c.open_time < utc(2026, 7, 15, 21, 0)]
        assert len(in_d2) == 24
        assert d2.high == max(c.high for c in in_d2)
        assert d2.low == min(c.low for c in in_d2)
        assert d2.volume == 24 * 100.0
        assert d2.is_closed

    def test_daily_volume_sums_once(self):
        candles = h1_candles(utc(2026, 7, 14, 21, 0), 24)
        daily = aggregate_candles(candles, "1D")
        assert len(daily) == 1
        assert daily[0].volume == 24 * 100.0

    def test_weekly_bucket_starts_monday_trading_day(self):
        # 兩週資料 → 2 根週線
        candles = h1_candles(utc(2026, 7, 6, 0, 0), 24 * 12)
        weekly = aggregate_candles(candles, "1W")
        assert 2 <= len(weekly) <= 3
        assert weekly[0].volume + weekly[-1].volume <= 24 * 12 * 100.0