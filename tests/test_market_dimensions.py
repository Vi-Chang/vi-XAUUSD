from datetime import datetime, timedelta, timezone

import pandas as pd

from app.engines.market_dimensions import assess_timeframes, dimensions, support_state
from app.engines.market_structure import StructureReport
from app.llm.service import _align_with_normalized
from app.schemas.ai import AiAction, AiConfidence, AiStrategy, AiTradePlan
from app.schemas.analysis import NormalizedAnalysisState

T0 = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)


def rep(tf="15M", trend="UP", low=100.0, high=110.0):
    return StructureReport(tf, [], [], trend, high, low, high, low)


def df(closes, *, closed=True, highs=None, lows=None):
    highs = highs or [x + .4 for x in closes]
    lows = lows or [x - .4 for x in closes]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes,
                         "is_closed": [closed] * len(closes)},
        index=pd.DatetimeIndex([T0 + timedelta(minutes=15 * i) for i in range(len(closes))]))


def bullish_ind(hist=1.0, prev=1.0, rsi=60, rsi_prev=60):
    return {"ema20": 105, "ema50": 103, "ema200": 98, "macd_hist": hist,
            "macd_hist_prev": prev, "rsi14": rsi, "rsi14_prev": rsi_prev,
            "stoch_k": 60, "adx": 28}


def base_inputs(event_status="GOOD", market_status="GOOD", indicators=None, chase=None):
    structures = {tf: rep(tf) for tf in ("1D", "4H", "1H", "15M")}
    return {"structures": structures,
        "indicators": indicators or {tf: bullish_ind() for tf in structures},
        "closed_times": {tf: T0.isoformat() for tf in structures},
        "m15_all": df([101]), "m15_closed": df([101]), "atr15": 2.0, "price": 101.0,
        "market_status": market_status, "event_status": event_status,
        "chase_flags": chase or []}


def test_higher_timeframe_bullish_with_h1_cooling_and_m15_pullback_waits():
    ind = {"1D": bullish_ind(), "4H": bullish_ind(),
           "1H": bullish_ind(hist=.4, prev=1.0, rsi=55, rsi_prev=62),
           "15M": bullish_ind(hist=-.4, prev=.2, rsi=44, rsi_prev=53)}
    out = dimensions(**base_inputs(indicators=ind))
    assert out["marketRegime"] in ("bullish", "strong_bullish")
    assert out["shortTermMomentum"] == "pullback"
    assert out["entryReadiness"] == "wait_confirmation"
    labels = {x.timeframe: x.label for x in out["assessments"]}
    assert labels["1H"] == "多頭動能降溫"
    assert labels["15M"] == "回調／支撐測試"


def test_unclosed_m15_shadow_below_support_is_intrabar_only():
    closed = df([100.5])
    live = df([99.8], closed=False, highs=[100.4], lows=[99.4])
    state, _ = support_state(rep(), live, closed, atr15=2, price=100)
    assert state == "intrabar_breach"


def test_closed_breakdown_then_reclaim_is_failed_breakdown():
    state, _ = support_state(rep(), df([99.4, 100.5]), df([99.4, 100.5]), 2, 100)
    assert state == "failed_breakdown"


def test_closed_breakdown_and_failed_retest_is_rejected():
    candles = df([99.3, 99.2], highs=[99.6, 99.8], lows=[99.0, 98.9])
    state, _ = support_state(rep(), candles, candles, 2, 100)
    assert state == "retest_rejected"


def test_all_timeframes_up_can_be_strong_but_chase_is_separate():
    out = dimensions(**base_inputs(chase=["CHASE_LONG_RISK:位置過高"]))
    assert out["marketRegime"] == "strong_bullish"
    assert out["entryReadiness"] == "avoid_chasing"


def test_event_failure_preserves_direction_but_lowers_confidence():
    out = dimensions(**base_inputs(event_status="FAILED"))
    assert out["marketRegime"] == "strong_bullish"
    assert out["dataConfidence"] == "low"
    assert out["entryReadiness"] != "ready"


def test_correlated_oscillators_form_one_family_not_three_votes():
    assessments = assess_timeframes({"15M": rep()},
        {"15M": {"macd_hist": 1, "rsi14": 70, "stoch_k": 80}}, {"15M": T0.isoformat()})
    m15 = next(x for x in assessments if x.timeframe == "15M")
    assert set(m15.familyScores) == {"structure", "momentum", "oscillator"}
    assert len(m15.familyScores) == 3


def test_stale_market_forces_insufficient_and_no_trade():
    out = dimensions(**base_inputs(market_status="STALE"))
    assert out["dataConfidence"] == "insufficient"
    assert out["entryReadiness"] == "no_trade"


def test_ai_action_is_forced_to_same_wait_and_confidence_cap():
    normalized = NormalizedAnalysisState(
        marketRegime="bullish", shortTermMomentum="pullback",
        entryReadiness="wait_confirmation", dataConfidence="low",
        eventRisk="unknown", tradingScript="大週期偏多，短線回調，等待確認。")
    ai = AiStrategy(available=True, action=AiAction(type="Buy", next_trigger="現在買"),
        trade_plan=AiTradePlan(entry_id="X"), confidence=AiConfidence(score=92))
    aligned = _align_with_normalized(ai, normalized)
    assert aligned.action.type == "Wait"
    assert aligned.trade_plan.entry_id is None
    assert aligned.confidence.score == 50
    assert aligned.gate_note == "事件資料缺失，目前分析僅依技術面"
