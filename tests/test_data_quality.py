"""Data Quality Engine(spec 四):缺漏、過期、Bid/Ask、休市防呆、SOURCE_MISMATCH。"""
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.engines import data_quality as dq
from app.providers.base import PriceTick
from tests.helpers import make_df, zigzag_path


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def m15_df(start: datetime, bars: int) -> pd.DataFrame:
    return make_df(zigzag_path([(bars - 1, 0.5)]), start_time=start, minutes=15)


def test_missing_candle_detected_during_open_hours():
    # 2026-07-14(二)10:00–14:00 UTC 全程開盤;拿掉一根 → 缺漏
    df = m15_df(utc(2026, 7, 14, 10, 0), 16)
    df = df.drop(df.index[5])
    missing, _ = dq.check_candles(df, "15M")
    assert len(missing) == 1
    assert "15M" in missing[0]


def test_no_false_missing_over_maintenance_break(monkeypatch=None):
    # 跨每日維護休市(週三 21:00–22:00 UTC = 17–18 ET 夏令)不得誤報缺漏
    part1 = m15_df(utc(2026, 7, 15, 19, 0), 8)    # 19:00–21:00
    part2 = m15_df(utc(2026, 7, 15, 22, 0), 8)    # 22:00–24:00
    df = pd.concat([part1, part2])
    missing, _ = dq.check_candles(df, "15M")
    assert missing == []


def test_stale_price_only_when_market_open():
    old_tick = PriceTick("XAUUSD", 4000.0, 4000.4,
                         quote_time=utc(2026, 7, 15, 11, 0), provider="test")
    # 開盤中(週三 12:00 UTC),報價 1 小時前 → STALE
    warns, stale = dq.check_live_price(old_tick, now=utc(2026, 7, 15, 12, 0))
    assert stale and any("STALE" in w for w in warns)
    # 週六(休市)→ 不得誤報 STALE(行事曆防呆)
    warns, stale = dq.check_live_price(old_tick, now=utc(2026, 7, 18, 12, 0))
    assert not stale


def test_bid_greater_than_ask_flagged():
    bad = PriceTick("XAUUSD", 4001.0, 4000.0, quote_time=utc(2026, 7, 15, 12, 0),
                    provider="test")
    warns, _ = dq.check_live_price(bad, now=utc(2026, 7, 15, 12, 0))
    assert any("bid(" in w for w in warns)


def test_source_mismatch_threshold():
    # 門檻 = max(0.05% × 4000 = 2.0, 0.3 × ATR 3.0 = 0.9) = 2.0
    mismatch, _ = dq.check_source_mismatch(4000.0, 4001.5, atr15=3.0)
    assert not mismatch
    mismatch, msg = dq.check_source_mismatch(4000.0, 4003.0, atr15=3.0)
    assert mismatch and "SOURCE_MISMATCH" in msg
    # 事件時段放寬 ×2 → 3.0 價差不再觸發
    mismatch, _ = dq.check_source_mismatch(4000.0, 4003.0, atr15=3.0, event_window=True)
    assert not mismatch


def test_evaluate_failed_without_data():
    report = dq.evaluate({"15M": pd.DataFrame(columns=["open", "high", "low", "close"])},
                         None, now=utc(2026, 7, 15, 12, 0))
    assert report.status == "FAILED"
    assert not report.tradeable
