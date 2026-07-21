"""BUGFIX:「假跌破」在真跌破後仍被顯示。

三重把關:引擎層假突破再推翻 → 只認最新事件 → 現價驗證 + 時效窗。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.engines.market_state import classify
from app.engines.market_structure import analyze_structure
from tests.helpers import make_df, zigzag_path


@pytest.fixture(autouse=True)
def _cfg():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _recent_df(closes):
    """K 棒時間貼近現在(結尾=現在),讓事件落在時效窗內。"""
    start = datetime.now(timezone.utc) - timedelta(minutes=15 * len(closes))
    return make_df(closes, start_time=start, minutes=15)


BASE = zigzag_path([(15, 1.5), (10, -1.5), (15, 1.5)])          # 前低 ~4007.5
BREAK = [4020, 4010, 4000, 3995]                                 # 跌破前低
RECOVER_HOLD = [4012, 4013, 4012, 4013, 4012, 4013, 4012, 4013]  # 收回並守住


def _structures(df):
    return {"15M": analyze_structure(df, "15M"),
            "1H": analyze_structure(df, "1H"),
            "4H": analyze_structure(df, "4H")}


class TestEngineRefutation:
    def test_failed_breakdown_stays_valid_when_recovery_holds(self):
        df = _recent_df(BASE + BREAK + RECOVER_HOLD)
        rep = analyze_structure(df, "15M")
        failed = [e for e in rep.events if e.event_type == "FAILED_BREAKDOWN"]
        assert failed and failed[-1].still_valid   # 收回守住 → 假跌破成立

    def test_failed_breakdown_refuted_when_price_breaks_again(self):
        """使用者回報情境:假跌破後價格又真的跌下去 → 事件必須失效。"""
        closes = BASE + BREAK + RECOVER_HOLD + [4005, 4000, 3998, 3996, 3994]
        rep = analyze_structure(_recent_df(closes), "15M")
        failed = [e for e in rep.events if e.event_type == "FAILED_BREAKDOWN"]
        assert failed
        assert not failed[-1].still_valid, "再度跌破後假跌破敘事必須被推翻"

    def test_failed_breakout_refuted_symmetric(self):
        up_base = zigzag_path([(15, -1.5), (10, 1.5), (15, -1.5)], start=4100.0)
        closes = (up_base + [4080, 4090, 4100, 4105]          # 突破前高
                  + [4088, 4087, 4088, 4087, 4088, 4087]      # 跌回 → 假突破
                  + [4095, 4100, 4106, 4110])                 # 又真的突破 → 推翻
        rep = analyze_structure(_recent_df(closes), "15M")
        failed = [e for e in rep.events if e.event_type == "FAILED_BREAKOUT"]
        assert failed
        assert not failed[-1].still_valid


class TestClassification:
    def test_valid_failed_breakdown_classified(self):
        """正控:收回守住、現價在價位上、事件新鮮 → 正確報假跌破。"""
        df = _recent_df(BASE + BREAK + RECOVER_HOLD)
        state = classify(structures=_structures(df), indicators_h1={},
                         indicators_m15={}, m15_df=df, price=4013.0)
        assert state == "FAILED_BREAKDOWN"

    def test_refuted_event_not_classified(self):
        """使用者情境端對端:又跌下去 → 不得再報「假跌破,跌不下去又漲回來」。"""
        closes = BASE + BREAK + RECOVER_HOLD + [4005, 4000, 3998, 3996, 3994]
        df = _recent_df(closes)
        state = classify(structures=_structures(df), indicators_h1={},
                         indicators_m15={}, m15_df=df, price=3994.0)
        assert state != "FAILED_BREAKDOWN"

    def test_price_guard_blocks_contradicted_narrative(self):
        """現價驗證:即使事件仍有效,現價已跌回價位下 → 不報假跌破。"""
        df = _recent_df(BASE + BREAK + RECOVER_HOLD)
        state = classify(structures=_structures(df), indicators_h1={},
                         indicators_m15={}, m15_df=df, price=4004.0)  # 低於 4007.5
        assert state != "FAILED_BREAKDOWN"

    def test_age_window_expires_failed_state(self):
        """時效窗:事件太舊(>180 分)不再定義當前狀態。"""
        old_start = datetime.now(timezone.utc) - timedelta(hours=30)
        df = make_df(BASE + BREAK + RECOVER_HOLD, start_time=old_start, minutes=15)
        state = classify(structures=_structures(df), indicators_h1={},
                         indicators_m15={}, m15_df=df, price=4013.0)
        assert state != "FAILED_BREAKDOWN"

    def test_newer_event_overrides_older_failed(self):
        """只認最新事件:假跌破之後出現新的向上突破 → 狀態不再是假跌破。"""
        closes = (BASE + BREAK + RECOVER_HOLD
                  + [4020, 4028, 4034, 4040, 4046, 4050])   # 收復並突破前高 → 新事件
        df = _recent_df(closes)
        rep15 = analyze_structure(df, "15M")
        ups_after = [e for e in rep15.events
                     if e.still_valid and not e.provisional and e.event_type.endswith("_UP")]
        if ups_after:   # 有更新的向上事件時,分類不得停留在假跌破
            state = classify(structures=_structures(df), indicators_h1={},
                             indicators_m15={}, m15_df=df,
                             price=float(df["close"].iloc[-1]))
            assert state != "FAILED_BREAKDOWN"
