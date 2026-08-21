"""Closed-candle market-regime state machine.

The previous state is audit context only.  Every evaluation derives the current
state again from the newest closed candles, so an old weakness label cannot
become a veto that sticks to later analysis.
"""
from __future__ import annotations

import hashlib
from typing import Any


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _assessment(normalized: dict, timeframe: str) -> dict:
    return next((item for item in normalized.get("timeframeAssessments") or []
                 if str(item.get("timeframe")) == timeframe), {})


def _level(normalized: dict, kind: str) -> float | None:
    item: dict = next((item for item in normalized.get("confirmationLevels") or []
                       if item.get("timeframe") == "15M" and item.get("kind") == kind), {})
    return _number(item.get("price"))


def evaluate_regime_state(
    data: dict,
    *,
    indicators: dict | None = None,
    previous: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Recalculate HTF/LTF state from the latest confirmed candle.

    Live price can report a test of the reclaim level, but it can never promote
    RECOVERING to BULLISH_RESTORED.  That promotion only uses lastClosedCandlePrice.
    """
    normalized = data.get("normalized_analysis") or {}
    indicators = indicators or {}
    previous = previous or {}
    h4 = _assessment(normalized, "4H")
    h1 = _assessment(normalized, "1H")
    m15 = _assessment(normalized, "15M")
    trend_bias = str(normalized.get("trendBias") or "neutral")
    htf_trend = str(h4.get("trend") or trend_bias).upper()
    mid_structure = str(h1.get("trend") or trend_bias).upper()
    m15_trend = str(m15.get("trend") or "neutral").upper()
    raw_momentum = str(normalized.get("shortTermMomentum") or
                       m15.get("momentum") or "stable")
    support_state = str(normalized.get("supportState") or "none")
    closed_price = _number(normalized.get("lastClosedCandlePrice"))
    live_price = _number(normalized.get("currentPrice"))
    reclaim = _level(normalized, "resistance")
    source_candle = str(normalized.get("lastClosedCandleTimestamp") or "")
    evaluated_at = str(data.get("timestamp_utc") or normalized.get("generatedAt") or "")

    m15_ind = indicators.get("15M") or {}
    rsi = _number(m15_ind.get("rsi14") or m15_ind.get("rsi"))
    rsi_prev = _number(m15_ind.get("rsi14_prev") or m15_ind.get("rsi_prev"))
    hist = _number(m15_ind.get("macd_hist"))
    hist_prev = _number(m15_ind.get("macd_hist_prev"))
    rsi_recovering = rsi is not None and rsi_prev is not None and rsi > rsi_prev
    macd_recovering = hist is not None and hist_prev is not None and hist > hist_prev
    near_reclaim = (closed_price is not None and reclaim is not None
                    and closed_price >= reclaim - max(abs(reclaim) * .0005, .5))
    confirmed_reclaim = (closed_price is not None and reclaim is not None
                         and closed_price > reclaim)
    live_testing = (live_price is not None and reclaim is not None
                    and live_price > reclaim and not confirmed_reclaim)
    htf_bullish = htf_trend == "BULLISH" and mid_structure == "BULLISH"
    htf_bearish = htf_trend == "BEARISH" and mid_structure == "BEARISH"
    structure_broken = support_state in {"confirmed_breakdown", "retest_rejected"}

    reasons: list[str] = [f"4H {htf_trend}", f"1H {mid_structure}"]
    if mid_structure == "BEARISH" and structure_broken:
        ltf_momentum = "BEARISH_CONFIRMED"
        composite = "BEARISH_CONFIRMED"
        reasons.append("15M 與 1H 已收盤結構同步轉空")
    elif htf_bullish and confirmed_reclaim:
        ltf_momentum = "BULLISH_RESTORED"
        composite = "HTF_BULLISH_LTF_BULLISH_RESTORED"
        reasons.append(f"15M 收盤站上重新轉強價 {reclaim:.2f}")
    elif htf_bullish and (near_reclaim or rsi_recovering or macd_recovering):
        ltf_momentum = "RECOVERING"
        composite = "HTF_BULLISH_LTF_RECOVERING"
        evidence = []
        if rsi_recovering:
            evidence.append("RSI 回升")
        if macd_recovering:
            evidence.append("MACD 柱改善")
        if near_reclaim:
            evidence.append("價格接近重新轉強價")
        reasons.append("、".join(evidence) or "15M 正在恢復")
    elif htf_bullish and (raw_momentum in {"weakening", "pullback", "reversal_risk"}
                          or m15_trend == "BEARISH" or structure_broken):
        ltf_momentum = "WEAKENING"
        composite = "HTF_BULLISH_LTF_WEAKENING"
        reasons.append("15M 轉弱，但 1H／4H 尚未翻空")
    elif htf_bullish:
        ltf_momentum = "BULLISH"
        composite = "HTF_BULLISH_LTF_BULLISH"
        reasons.append("多週期多方結構維持")
    elif htf_bearish:
        ltf_momentum = "BEARISH"
        composite = "HTF_BEARISH_LTF_BEARISH"
        reasons.append("多週期空方結構維持")
    else:
        ltf_momentum = "NEUTRAL"
        composite = "MIXED_RANGE"
        reasons.append("多週期方向不一致")

    confirmed_state = ltf_momentum
    live_state = "LIVE_TESTING_RECLAIM" if live_testing else "LIVE_ALIGNED"
    old_state = str(previous.get("confirmedCandleState") or "NONE")
    old_candle = str(previous.get("sourceCandleCloseTime") or "")
    changed = old_state != confirmed_state
    new_candle = bool(source_candle and source_candle != old_candle)
    version = int(previous.get("stateVersion") or 0) + (1 if changed or new_candle else 0)
    transition_reason = reasons[-1]
    transition_log = list(previous.get("transitionLog") or [])[-49:]
    events: list[dict] = []
    if changed:
        transition = {
            "previousState": old_state,
            "newState": confirmed_state,
            "reason": transition_reason,
            "sourceCandleCloseTime": source_candle,
            "reclaimLevel": reclaim,
            "evaluatedAt": evaluated_at,
            "stateVersion": version,
        }
        transition_log.append(transition)
        if confirmed_state in {"BULLISH_RESTORED", "BEARISH_CONFIRMED"}:
            event_type = ("BULLISH_RESTORED" if confirmed_state == "BULLISH_RESTORED"
                          else "BEARISH_CONFIRMED")
            symbol = str(data.get("symbol") or "XAUUSD")
            seed = f"{symbol}|REGIME|{event_type}|{source_candle}|{reclaim}"
            events.append({
                "eventId": hashlib.sha256(seed.encode()).hexdigest()[:32],
                "event_type": event_type,
                "previousState": old_state,
                "currentState": confirmed_state,
                "transitionReason": transition_reason,
                "candleCloseTime": source_candle,
                "triggerLevel": reclaim,
                "currentPrice": live_price or closed_price or 0,
                "marketState": composite,
                "finalDecision": confirmed_state,
                "calculatedAt": evaluated_at,
                "dataVersion": int(data.get("version") or version),
                "direction": "LONG" if confirmed_state == "BULLISH_RESTORED" else "SHORT",
                "setupId": f"REGIME-{source_candle}",
                "notificationEligible": True,
            })

    return {
        "htfTrend": htf_trend,
        "midTfStructure": mid_structure,
        "ltfMomentum": ltf_momentum,
        "compositeRegime": composite,
        "livePriceState": live_state,
        "confirmedCandleState": confirmed_state,
        "reclaimLevel": reclaim,
        "evaluatedAt": evaluated_at,
        "sourceCandleCloseTime": source_candle,
        "stateVersion": version,
        "transitionReason": transition_reason,
        "reasons": reasons,
        "transitionLog": transition_log,
    }, events
