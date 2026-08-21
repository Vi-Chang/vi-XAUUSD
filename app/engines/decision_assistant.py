"""Action-first market decision layer shared by dashboard, history and alerts."""
from __future__ import annotations

import hashlib
from typing import Any

ENGINE_VERSION = "decision-assistant-v3"
READY = {"LONG_READY", "SHORT_READY", "BREAKOUT_ENTRY_READY", "PULLBACK_ENTRY_READY"}


def _num(value: Any, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _grade(score: int) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "不通知"


def classify_regime(data: dict) -> tuple[str, list[str]]:
    """4H sets direction, 1H structure, 15M timing; no single TF can flip regime."""
    n = data.get("normalized_analysis") or {}
    trend = str(n.get("trendBias") or "neutral")
    momentum = str(n.get("shortTermMomentum") or "stable")
    assessments = {str(x.get("timeframe")): x for x in n.get("timeframeAssessments") or []}
    h4 = str((assessments.get("4H") or {}).get("trend") or trend)
    h1 = str((assessments.get("1H") or {}).get("trend") or trend)
    m15 = str((assessments.get("15M") or {}).get("trend") or "neutral")
    score = int(n.get("trendScore") or 50)
    rsi = _num(((data.get("timeframes") or {}).get("m15") or {}).get("rsi"), 50)
    support = str(n.get("supportState") or "none")
    breakout = str(n.get("breakoutState") or "none")
    reasons = [f"4H {h4}", f"1H {h1}", f"15M {m15}／{momentum}"]
    if n.get("marketDataStatus") != "GOOD":
        return "NO_EDGE", ["行情資料不完整或過期"]
    if trend == "bullish" and momentum in {"weakening", "pullback", "reversal_risk"}:
        return "SHORT_WEAK_HTF_BULLISH", reasons
    if trend == "bearish" and momentum in {"accelerating", "stable"}:
        return "SHORT_STRONG_HTF_BEARISH", reasons
    if trend == "bullish" and (rsi >= 80 or score >= 90) and n.get("entryTiming") == "chase":
        return "OVERHEATED_BULLISH", reasons + [f"15M RSI {rsi:.1f}，位置延伸"]
    if trend == "bearish" and rsi <= 20:
        return "OVERSOLD_BEARISH", reasons + [f"15M RSI {rsi:.1f}"]
    if breakout == "testing":
        return "BREAKOUT_SETUP", reasons
    if support in {"testing_support", "failed_breakdown"}:
        return "PULLBACK_SETUP", reasons
    if momentum == "reversal_risk" or breakout == "failed":
        return "REVERSAL_RISK", reasons
    if str(n.get("marketRegime")) == "range":
        return "RANGE", reasons
    if trend == "bullish" and h4 != "bearish" and h1 != "bearish":
        return "TREND_BULLISH", reasons
    if trend == "bearish" and h4 != "bullish" and h1 != "bullish":
        return "TREND_BEARISH", reasons
    return "NO_EDGE", reasons + ["多週期沒有一致優勢"]


def breakout_quality(candle: dict, trigger: float, atr: float,
                     direction: str, rejection_count: int = 0) -> dict:
    """Judge closed-candle breakout strength instead of close-only confirmation."""
    opened, high, low, close = (_num(candle.get(k)) for k in ("open", "high", "low", "close"))
    span = max(high - low, 0.01)
    body_ratio = abs(close - opened) / span
    wick_ratio = ((high - max(opened, close)) / span if direction == "LONG"
                  else (min(opened, close) - low) / span)
    distance = ((close - trigger) if direction == "LONG" else (trigger - close))
    strength = distance / max(atr, 0.01)
    score = round(max(0, min(100, 35 + strength * 120 + body_ratio * 35
                             - wick_ratio * 30 - rejection_count * 8)))
    weak = distance > 0 and (strength < .10 or body_ratio < .45 or wick_ratio > .40)
    return {"state": "WEAK_BREAKOUT" if weak else "STRONG_BREAKOUT" if distance > 0 else "NOT_CONFIRMED",
            "score": score, "closeDistanceAtr": round(strength, 3),
            "bodyRatio": round(body_ratio, 3), "wickRatio": round(wick_ratio, 3),
            "priorRejectionCount": rejection_count}


def pullback_depth(distance: float, atr: float, structure_broken: bool) -> str:
    if structure_broken:
        return "STRUCTURE_BREAK"
    ratio = distance / max(atr, .01)
    if ratio < .35:
        return "SHALLOW"
    if ratio < .70:
        return "NORMAL"
    if ratio <= 1.20:
        return "DEEP"
    return "STRUCTURE_BREAK"


def evaluate_decision_assistant(data: dict, *, latest_candle: dict | None = None,
                                previous: dict | None = None) -> tuple[dict, list[dict]]:
    from app.config import get_settings

    settings = get_settings()
    previous = previous or {}
    n = data.get("normalized_analysis") or {}
    regime, regime_reasons = classify_regime(data)
    continuation = data.get("trend_continuation_engine") or {}
    ledger = data.get("breakout_setup_manager") or {}
    candidates = list(continuation.get("candidates") or [])
    active = ledger.get("activeSetup") or {}
    if active:
        candidates.append(active)
    selected = next((x for x in candidates if str(x.get("status")) in READY
                     or str(x.get("status", "")).startswith("ENTRY_READY_")), None)
    selected = selected or (active if active else next(iter(candidates), {}))
    direction = str(selected.get("direction") or ("LONG" if "BULL" in regime else
                                                    "SHORT" if "BEAR" in regime else "NONE"))
    current = _num(n.get("currentPrice"))
    atr = max(_num(selected.get("atrValue") or selected.get("atr15") or n.get("atr15")), .01)
    low, high = _num(selected.get("entryZoneLow")), _num(selected.get("entryZoneHigh"))
    optimal = (low + high) / 2 if low and high else current
    distance = abs(current - optimal)
    distance_atr = distance / atr
    chase_penalty = round(min(35, max(0, distance_atr - .10) * 30))
    rr = _num(selected.get("riskReward"))
    if not rr and low and high and isinstance(selected.get("stopPrice"), (int, float)) and isinstance(selected.get("tp1"), (int, float)):
        entry_edge = high if direction == "LONG" else low
        risk = abs(entry_edge - float(selected["stopPrice"]))
        rr = abs(float(selected["tp1"]) - entry_edge) / risk if risk else 0
    base = int(selected.get("signalScore") or n.get("entryQualityScore") or n.get("trendScore") or 50)
    breakdown = {
        "higherTimeframe": 20 if regime in {"TREND_BULLISH", "TREND_BEARISH", "BREAKOUT_SETUP", "PULLBACK_SETUP"} else 10,
        "structure": 15 if n.get("consistencyValid", True) else 0,
        "confluence": 15 if selected.get("pullbackEntryZoneLow") or selected.get("passedReasons") else 7,
        "momentum": 10 if n.get("shortTermMomentum") in {"accelerating", "stable"} else 5,
        "volatility": 10 if regime not in {"RANGE", "REVERSAL_RISK", "NO_EDGE"} else 4,
        "location": max(0, 10 - round(chase_penalty / 3.5)),
        "riskReward": 10 if rr >= settings.decision_assistant_min_rr else 0,
        "breakout": 5,
        "pullback": 5 if selected.get("pullbackEntryZoneLow") else 2,
    }
    score = max(0, min(100, round(sum(breakdown.values()) * .7 + base * .3 - chase_penalty)))
    quality_grade = _grade(score)
    rr_passed = rr >= settings.decision_assistant_min_rr
    status = str(selected.get("status") or "NO_SETUP")
    can_enter = (status in READY or status.startswith("ENTRY_READY_")) and rr_passed and score >= 50
    no_trade_reasons = []
    if regime in {"NO_EDGE", "RANGE", "SHORT_WEAK_HTF_BULLISH",
                  "OVERHEATED_BULLISH", "OVERSOLD_BEARISH", "REVERSAL_RISK"}:
        no_trade_reasons.append("目前市場型態不適合直接追價")
        can_enter = False
    if not rr_passed:
        no_trade_reasons.append(f"賺賠比 {rr:.2f}，低於門檻 {settings.decision_assistant_min_rr:.2f}")
    if score < 50:
        no_trade_reasons.append(f"進場品質 {score} 分，未達通知門檻 50 分")
    if distance_atr >= settings.decision_assistant_missed_entry_atr:
        can_enter = False
        action = "不要追價"
        trade_state = "MISSED_ENTRY" if status in READY else "NO_TRADE"
    elif no_trade_reasons:
        action, trade_state = "沒有好機會", "NO_TRADE"
    elif distance_atr <= settings.decision_assistant_approaching_atr and not can_enter:
        action, trade_state = "現在先等", "ENTRY_APPROACHING"
    elif can_enter:
        action, trade_state = "現在可以進", "ENTRY_READY"
    elif "PULLBACK" in status or "RETEST" in status:
        action, trade_state = "等回踩", "WAIT_PULLBACK"
    else:
        action, trade_state = "等突破", "WAIT_BREAKOUT"
    candle = latest_candle or {}
    trigger = _num(selected.get("breakoutTrigger"))
    bq = breakout_quality(candle, trigger, atr, direction) if trigger and candle else {}
    if bq.get("state") == "WEAK_BREAKOUT" and can_enter:
        can_enter, action, trade_state = False, "現在先等", "WEAK_BREAKOUT"
        no_trade_reasons.append("雖然收盤突破，但力道不足，先看下一根能不能守住")
    structure_broken = str(n.get("supportState")) in {"confirmed_breakdown", "retest_rejected"}
    depth = pullback_depth(abs(current - trigger), atr, structure_broken) if trigger else "NONE"
    if depth == "STRUCTURE_BREAK" and "PULLBACK" in status:
        can_enter, action, trade_state = False, "多方失效" if direction == "LONG" else "空方失效", "SCENARIO_INVALIDATED"
    scenario_id = str(selected.get("setupId") or "")
    scenario_version = int(selected.get("scenarioVersion") or selected.get("setupVersionNumber") or 1)
    event_type = {
        "ENTRY_READY": "entry_ready", "ENTRY_APPROACHING": "approach_entry",
        "MISSED_ENTRY": "missed_entry", "WEAK_BREAKOUT": "breakout_confirmed",
        "SCENARIO_INVALIDATED": "scenario_invalidated", "NO_TRADE": "no_trade",
    }.get(trade_state, "")
    changed = (previous.get("tradeState") != trade_state or previous.get("regime") != regime
               or previous.get("scenarioId") != scenario_id)
    should_notify = bool(event_type and changed and trade_state != "NO_TRADE")
    output = {
        "schemaVersion": ENGINE_VERSION, "regime": regime, "regimeReasons": regime_reasons,
        "scenarioId": scenario_id, "scenarioVersion": scenario_version,
        "scenarioType": selected.get("type") or ("BREAKOUT" if trigger else "NONE"),
        "direction": direction, "tradeState": trade_state, "actionSummary": action,
        "canEnter": can_enter, "entryQualityScore": score, "entryQualityGrade": quality_grade,
        "qualityExplanation": "代表多個週期、結構、位置與風控條件的綜合品質，不是勝率。",
        "qualityBreakdown": breakdown, "expectedEntry": optimal,
        "entryZone": {"low": low or None, "high": high or None},
        "invalidation": selected.get("stopPrice"),
        "targets": [x for x in (selected.get("tp1"), selected.get("tp2"), selected.get("tp3")) if isinstance(x, (int, float))],
        "rewardRiskRatio": round(rr, 2), "rrPassed": rr_passed,
        "distanceFromOptimalEntry": round(distance, 2), "distanceInAtr": round(distance_atr, 3),
        "chasePenalty": chase_penalty, "breakoutQuality": bq, "pullbackDepth": depth,
        "noTradeReasons": no_trade_reasons, "eventType": event_type,
        "shouldNotify": should_notify,
        "nextTrigger": (f"15M 收盤{'站上' if direction == 'LONG' else '跌破'} {trigger:.2f}"
                        if trigger else "等待新的市場結構"),
        "why": {"4H": regime_reasons[0] if regime_reasons else "",
                "1H": regime_reasons[1] if len(regime_reasons) > 1 else "",
                "15M": regime_reasons[2] if len(regime_reasons) > 2 else "",
                "technical": selected.get("passedReasons") or [],
                "blocked": no_trade_reasons or selected.get("missingConditions") or []},
    }
    events = []
    if should_notify:
        candle_time = str(n.get("lastClosedCandleTimestamp") or "")
        calculated = str(data.get("timestamp_utc") or "")
        seed = f"{data.get('symbol', 'XAUUSD')}|{scenario_id}|{event_type}|{trade_state}|{candle_time}"
        events.append({"eventId": hashlib.sha256(seed.encode()).hexdigest()[:32],
                       "event_type": event_type.upper(), "currentState": trade_state,
                       "previousState": previous.get("tradeState") or "NONE",
                       "direction": direction, "setupId": scenario_id,
                       "currentPrice": current, "entryZone": output["entryZone"],
                       "stopLoss": output["invalidation"], "targets": output["targets"],
                       "triggerLevel": trigger or None, "decisionAssistant": output,
                       "transitionReason": action, "triggerReason": action,
                       "marketState": regime, "finalDecision": trade_state,
                       "candleCloseTime": candle_time, "calculatedAt": calculated,
                       "notificationEligible": True})
    return output, events
