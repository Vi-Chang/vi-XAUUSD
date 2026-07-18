"""Capital.com Adapter 純函式測試(不打真實 API)。"""
from datetime import datetime, timezone

from app.providers.capital_com import candle_from_price_row, parse_snapshot_time

SAMPLE_ROW = {
    "snapshotTime": "2026-07-20T09:15:00",
    "snapshotTimeUTC": "2026-07-20T08:15:00",
    "openPrice": {"bid": 4650.0, "ask": 4650.6},
    "closePrice": {"bid": 4655.2, "ask": 4655.8},
    "highPrice": {"bid": 4657.0, "ask": 4657.6},
    "lowPrice": {"bid": 4648.4, "ask": 4649.0},
    "lastTradedVolume": 1234,
}


def test_parse_snapshot_time_is_utc():
    t = parse_snapshot_time("2026-07-20T08:15:00")
    assert t == datetime(2026, 7, 20, 8, 15, tzinfo=timezone.utc)


def test_candle_from_price_row_mid_and_spread():
    now = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    c = candle_from_price_row(SAMPLE_ROW, "XAUUSD", "15M", now)
    assert c.open_time == datetime(2026, 7, 20, 8, 15, tzinfo=timezone.utc)
    assert c.close_time == datetime(2026, 7, 20, 8, 30, tzinfo=timezone.utc)
    assert c.open == (4650.0 + 4650.6) / 2
    assert c.close == (4655.2 + 4655.8) / 2
    assert c.bid_close == 4655.2 and c.ask_close == 4655.8
    assert abs(c.spread - 0.6) < 1e-9
    assert c.volume == 1234
    assert c.is_closed  # 08:30 <= 09:00
    assert c.data_provider == "capital_com"


def test_unclosed_candle_flagged():
    now = datetime(2026, 7, 20, 8, 20, tzinfo=timezone.utc)
    c = candle_from_price_row(SAMPLE_ROW, "XAUUSD", "15M", now)
    assert not c.is_closed
