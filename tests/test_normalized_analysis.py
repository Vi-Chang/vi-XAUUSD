from datetime import datetime, timezone

import pandas as pd

from app.engines.market_structure import StructureEvent, StructureReport
from app.engines.normalized_analysis import (
    build_normalized_state,
    validate_consistency,
)

T0 = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)


def report(trend="UP", events=None, high=2400.0, low=2380.0):
    return StructureReport("15M", [], events or [], trend, high, low, high, low)


def event(kind, price=2400.0, *, valid=True, provisional=False):
    return StructureEvent(kind, T0, price, "15M", [T0], None, valid, provisional)


def frame(close, *, closed=True):
    return pd.DataFrame({"open": [close], "high": [close + 1], "low": [close - 1],
                         "close": [close], "is_closed": [closed]},
                        index=pd.DatetimeIndex([T0]))


def build(*, m15=None, all_close=2402, all_closed=True, closed_close=2402,
          h1="UP", bull=None, bear=None, chase=None, event_source="manual",
          event_stale=False, quality="GOOD", state="STRONG_BULL_TREND"):
    return build_normalized_state(
        generated_at="2026-08-13T01:01:00+00:00",
        market_timestamp="2026-08-13T01:00:00+00:00", current_price=2401.0,
        market_state=state, market_quality=quality, event_source=event_source,
        event_stale=event_stale,
        structures={"15M": m15 or report(h1), "1H": report(h1)},
        m15_all=frame(all_close, closed=all_closed), m15_closed=frame(closed_close),
        bull_evidence=bull or [], bear_evidence=bear or [], chase_flags=chase or [])


def test_breakout_confirmed_only_after_closed_candle_holds_level():
    s = build(m15=report(events=[event("BOS_UP")]),
              bull=["STRUCT:15分K收盤站上前高 2400.00,順勢突破"])
    assert s.breakoutState == "confirmed"
    assert s.longEvidence[0].sourceEvent == "BOS_UP"
    assert s.entryTiming == "favorable"


def test_intrabar_breakout_is_testing_not_confirmed():
    s = build(m15=report(events=[]), all_close=2402, all_closed=False,
              closed_close=2399, bull=["HTF:1小時趨勢向上"])
    assert s.breakoutState == "testing"
    assert s.entryTiming == "wait"


def test_failed_breakout_invalidates_bull_evidence_and_counts_failure_short():
    failed = event("FAILED_BREAKOUT")
    s = build(m15=report(events=[failed]), closed_close=2398,
              state="FAILED_BREAKOUT",
              bull=["STRUCT:15分K收盤站上前高 2400.00,順勢突破", "HTF:1小時趨勢向上"])
    assert s.breakoutState == "failed"
    assert all("順勢突破" not in x.label for x in s.longEvidence)
    assert len(s.invalidatedEvidence) == 1
    assert s.invalidatedEvidence[0].level == 2400
    assert any("假突破" in x.label for x in s.shortEvidence)
    assert s.bullPct != 100


def test_bullish_trend_can_be_chase_long_timing():
    s = build(bull=["HTF:1小時趨勢向上"], chase=["CHASE_LONG_RISK:位置過高"])
    assert (s.trendBias, s.entryTiming, s.riskLabel) == ("bullish", "chase", "追多風險")
    assert "追多" in s.riskMessage


def test_bearish_trend_can_be_chase_short_timing():
    s = build(h1="DOWN", state="STRONG_BEAR_TREND",
              bear=["HTF:1小時趨勢向下"], chase=["CHASE_SHORT_RISK:位置過低"])
    assert (s.trendBias, s.entryTiming, s.riskLabel) == ("bearish", "chase", "追空風險")
    assert "追空" in s.riskMessage


def test_all_event_sources_failed_is_separate_from_good_market_data():
    s = build(event_source="none", quality="GOOD")
    assert s.marketDataStatus == "GOOD"
    assert s.eventDataStatus == "FAILED"


def test_mixed_component_timestamps_force_wait():
    s = build().model_copy(update={"sourceTimestamps": {
        "marketState": "2026-08-13T01:00:00+00:00",
        "risk": "2026-08-13T00:45:00+00:00"}})
    checked = validate_consistency(s)
    assert checked.entryTiming == "wait"
    assert "各區塊使用不同 timestamp" in checked.consistencyErrors


def test_evidence_ratio_mismatch_force_wait():
    s = build(bull=["HTF:1小時趨勢向上"], bear=["MOMO:動能偏空"])
    checked = validate_consistency(s.model_copy(update={"bullPct": 100, "bearPct": 0}))
    assert checked.entryTiming == "wait"
    assert "技術傾向與去相關化評分不一致" in checked.consistencyErrors


def test_risk_label_and_message_direction_mismatch_force_wait():
    s = build(chase=["CHASE_LONG_RISK:位置過高"])
    checked = validate_consistency(s.model_copy(update={"riskMessage": "目前有追空風險"}))
    assert checked.entryTiming == "wait"
    assert checked.riskLabel == "等待確認"
    assert "risk label 與風險內文方向不一致" in checked.consistencyErrors
