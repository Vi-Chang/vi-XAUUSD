"""Failed-breakout, support-role and anti-anchoring evidence engine.

This engine evaluates current 15M evidence from scratch.  ``previous`` is used
only for lifecycle transitions (support role/reclaim and notification dedupe),
never as a reason to preserve yesterday's directional opinion.
"""
from __future__ import annotations

import math
from typing import Any

BIAS_STATES = {
    "STRONG_BULLISH", "BULLISH", "BULLISH_WITH_RESISTANCE",
    "BULLISH_WEAKENING", "BULLISH_RECLAIM", "NEUTRAL_BULLISH", "NEUTRAL",
    "NEUTRAL_BEARISH", "BEARISH_RECLAIM", "BEARISH_WEAKENING",
    "BEARISH_WITH_SUPPORT", "BEARISH", "STRONG_BEARISH",
}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _last(candles: list[dict] | None) -> dict:
    return dict(candles[-1]) if candles else {}


def _momentum_decay(momentum: dict | None, follow_through: dict | None,
                    volume: dict | None) -> tuple[int, list[str]]:
    momentum, follow_through, volume = momentum or {}, follow_through or {}, volume or {}
    score, reasons = 0, []
    if momentum.get("macd_histogram_shrinking"):
        score += 10
        reasons.append("MACD 柱體縮短")
    if momentum.get("kd_rollover"):
        score += 8
        reasons.append("KD 高檔轉弱")
    if momentum.get("rsi_divergence"):
        score += 12
        reasons.append("RSI 背離")
    if volume.get("decreasing_on_attempts"):
        score += 8
        reasons.append("突破量能下降")
    if follow_through.get("distance_decreasing"):
        score += 10
        reasons.append("突破延續距離縮短")
    return min(score, 35), reasons


def _coarse_bias(state: str) -> str:
    if state in {"STRONG_BULLISH", "BULLISH", "BULLISH_WITH_RESISTANCE",
                 "BULLISH_WEAKENING", "BULLISH_RECLAIM"}:
        return "BULLISH"
    if state in {"STRONG_BEARISH", "BEARISH", "BEARISH_WITH_SUPPORT",
                 "BEARISH_WEAKENING", "BEARISH_RECLAIM"}:
        return "BEARISH"
    return "NEUTRAL"


def _position_risk(*, position_side: str, side: str, state: str,
                   support_state: str, momentum_decay_score: int) -> tuple[str, str]:
    if position_side != side:
        return "POSITION_HEALTHY", "HOLD"
    if support_state == "BROKEN_CONFIRMED":
        return "POSITION_INVALIDATED", "EXIT"
    if support_state in {"BROKEN_PENDING_CLOSE", "BROKEN"}:
        return "POSITION_DEFENSIVE", "REDUCE"
    if state in {"REPEATED_REJECTION", "FAILED_BREAKOUT",
                 "CONFIRMED_REVERSAL_RISK"} or momentum_decay_score >= 18:
        return "POSITION_WARNING", "HOLD"
    return "POSITION_HEALTHY", "HOLD"


def evaluate_failed_breakout(
    *, side: str, resistance_zone: dict | None = None,
    support_zone: dict | None = None, attempt_count: int = 0,
    closed_candles: list[dict] | None = None, wick_rejection: dict | None = None,
    close_position: str | None = None, volume: dict | None = None,
    momentum: dict | None = None, follow_through: dict | None = None,
    current_price: float | None = None, previous: dict | None = None,
    position_side: str | None = None, confirmation_buffer: float = 0.0,
    setup_age_bars: int | None = None,
    base_bias_state: str | None = None,
) -> tuple[dict, list[dict]]:
    """Evaluate failed breakout and support/reclaim evidence symmetrically."""
    side = str(side or "LONG").upper()
    previous = previous or {}
    latest = _last(closed_candles)
    close = _number(latest.get("close"))
    high = _number(latest.get("high"))
    low = _number(latest.get("low"))
    candle_time = str(latest.get("time") or latest.get("closeTime") or "")
    current = _number(current_price)
    resistance_zone, support_zone = resistance_zone or {}, support_zone or {}
    resistance_edge = _number(resistance_zone.get("high") if side == "LONG"
                              else resistance_zone.get("low"))
    support_edge = _number(support_zone.get("low") if side == "LONG"
                           else support_zone.get("high"))
    wick = wick_rejection or {}
    direction = "UPPER" if side == "LONG" else "LOWER"
    wick_state = str(wick.get("wick_rejection_state") or "")
    wick_score = int(_number(wick.get("wick_rejection_score")) or 0)
    matching_wick = direction in wick_state
    attempted = bool(
        resistance_edge is not None and
        ((side == "LONG" and high is not None and high >= resistance_edge) or
         (side == "SHORT" and low is not None and low <= resistance_edge)))
    failed_close = bool(
        resistance_edge is not None and close is not None and attempted and
        ((side == "LONG" and close <= resistance_edge) or
         (side == "SHORT" and close >= resistance_edge)))
    confirmed_breakout = bool(
        resistance_edge is not None and close is not None and
        ((side == "LONG" and close > resistance_edge + confirmation_buffer) or
         (side == "SHORT" and close < resistance_edge - confirmation_buffer)))

    decay_score, decay_reasons = _momentum_decay(momentum, follow_through, volume)
    rejection_score = 0
    rejection_reasons: list[str] = []
    if failed_close or close_position == "REJECTED":
        rejection_score += 25
        rejection_reasons.append("15M 收盤未能站穩突破區")
    if matching_wick:
        rejection_score += min(20, round(wick_score * .2))
        rejection_reasons.append("壓力區出現拒絕影線")
    if attempt_count:
        rejection_score += min(25, 8 + max(0, int(attempt_count) - 1) * 7)
        rejection_reasons.append(f"同區已測試 {int(attempt_count)} 次")
    previous_candle = str(previous.get("closedCandleTime") or "")
    inferred_age = int(previous.get("setupAgeBars") or 0)
    if attempted and candle_time and candle_time != previous_candle:
        inferred_age += 1
    age_bars = int(setup_age_bars if setup_age_bars is not None else inferred_age)
    if age_bars >= 6 and attempt_count:
        rejection_score += min(15, (age_bars - 4) * 2)
        rejection_reasons.append(f"突破劇本已等待 {age_bars} 根 15M")
    rejection_score += decay_score
    rejection_reasons.extend(decay_reasons)
    rejection_score = min(100, rejection_score)
    if confirmed_breakout:
        rejection_state = "NONE"
        rejection_score = 0
    elif rejection_score >= 80:
        rejection_state = "CONFIRMED_REVERSAL_RISK"
    elif rejection_score >= 60:
        rejection_state = "FAILED_BREAKOUT"
    elif rejection_score >= 38:
        rejection_state = "REPEATED_REJECTION"
    elif rejection_score >= 18:
        rejection_state = "FIRST_REJECTION"
    else:
        rejection_state = "NONE"
    rejection_strength = ("HIGH" if rejection_score >= 60 else
                          "MEDIUM" if rejection_score >= 38 else
                          "LOW" if rejection_score else "NONE")

    previous_support = str(previous.get("supportState") or "SAFE")
    support_state, support_role = "SAFE", "SUPPORT" if side == "LONG" else "RESISTANCE"
    if support_edge is not None:
        intrabar_crossed = bool(
            (side == "LONG" and current is not None and current < support_edge) or
            (side == "SHORT" and current is not None and current > support_edge))
        close_broken = bool(
            close is not None and
            ((side == "LONG" and close < support_edge - confirmation_buffer) or
             (side == "SHORT" and close > support_edge + confirmation_buffer)))
        close_reclaimed = bool(
            close is not None and
            ((side == "LONG" and close > support_edge + confirmation_buffer) or
             (side == "SHORT" and close < support_edge - confirmation_buffer)))
        if close_broken:
            support_state, support_role = "BROKEN_CONFIRMED", "RESISTANCE_CANDIDATE"
        elif previous_support in {"BROKEN_PENDING_CLOSE", "BROKEN_CONFIRMED"} and close_reclaimed:
            if bool((follow_through or {}).get("confirmed")):
                support_state, support_role = "HELD", "SUPPORT" if side == "LONG" else "RESISTANCE"
            else:
                support_state, support_role = "RECLAIMED", "RECLAIM_CANDIDATE"
        elif previous_support == "BROKEN_CONFIRMED":
            support_state, support_role = "BROKEN_CONFIRMED", "RESISTANCE_CANDIDATE"
        elif intrabar_crossed:
            support_state, support_role = "BROKEN_PENDING_CLOSE", "TESTING_ROLE_FLIP"

    if side == "LONG":
        if support_state == "BROKEN_CONFIRMED":
            bias_state = "NEUTRAL_BEARISH" if rejection_score >= 75 else "NEUTRAL"
        elif support_state == "BROKEN_PENDING_CLOSE":
            bias_state = "BULLISH_WEAKENING"
        elif support_state == "RECLAIMED":
            bias_state = "BULLISH_RECLAIM"
        elif rejection_state == "FIRST_REJECTION":
            bias_state = "BULLISH_WITH_RESISTANCE"
        elif rejection_state == "REPEATED_REJECTION":
            bias_state = "NEUTRAL_BULLISH"
        elif rejection_state in {"FAILED_BREAKOUT", "CONFIRMED_REVERSAL_RISK"}:
            bias_state = "NEUTRAL"
        else:
            bias_state = (str(base_bias_state or "BULLISH").upper()
                          if "BULL" in str(base_bias_state or "BULLISH").upper()
                          else "NEUTRAL")
    else:
        if support_state == "BROKEN_CONFIRMED":
            bias_state = "NEUTRAL_BULLISH" if rejection_score >= 75 else "NEUTRAL"
        elif support_state == "BROKEN_PENDING_CLOSE":
            bias_state = "BEARISH_WEAKENING"
        elif support_state == "RECLAIMED":
            bias_state = "BEARISH_RECLAIM"
        elif rejection_state == "FIRST_REJECTION":
            bias_state = "BEARISH_WITH_SUPPORT"
        elif rejection_state == "REPEATED_REJECTION":
            bias_state = "NEUTRAL_BEARISH"
        elif rejection_state in {"FAILED_BREAKOUT", "CONFIRMED_REVERSAL_RISK"}:
            bias_state = "NEUTRAL"
        else:
            bias_state = (str(base_bias_state or "BEARISH").upper()
                          if "BEAR" in str(base_bias_state or "BEARISH").upper()
                          else "NEUTRAL")
    confidence = max(20, min(95, 82 - round(rejection_score * .45) -
                             (18 if support_state == "BROKEN_CONFIRMED" else
                              8 if support_state == "BROKEN_PENDING_CLOSE" else 0)))
    setup_quality = ("POOR" if rejection_state in {"REPEATED_REJECTION", "FAILED_BREAKOUT",
                                                    "CONFIRMED_REVERSAL_RISK"}
                     or support_state in {"BROKEN_PENDING_CLOSE", "BROKEN_CONFIRMED"}
                     else "ACCEPTABLE")
    entry_eligibility = "NO" if setup_quality == "POOR" else "REASSESS"
    position_risk, position_action = _position_risk(
        position_side=str(position_side or "").upper(), side=side,
        state=rejection_state, support_state=support_state,
        momentum_decay_score=decay_score)
    result = {
        "schemaVersion": "failed-breakout-rejection-v1", "side": side,
        "state": rejection_state, "rejectionScore": rejection_score,
        "rejectionStrength": rejection_strength, "rejectionReasons": rejection_reasons,
        "momentumDecayScore": decay_score, "momentumDecayReasons": decay_reasons,
        "attemptCount": int(attempt_count), "confirmedBreakout": confirmed_breakout,
        "setupAgeBars": age_bars,
        "setupExpired": bool(age_bars >= 12 and rejection_state in {
            "FAILED_BREAKOUT", "CONFIRMED_REVERSAL_RISK"}),
        "biasState": bias_state, "marketBias": _coarse_bias(bias_state),
        "biasConfidence": confidence, "supportState": support_state,
        "supportRole": support_role, "supportLevel": support_edge,
        "resistanceZone": resistance_zone or None, "setupQuality": setup_quality,
        "entryEligibility": entry_eligibility, "positionRiskState": position_risk,
        "positionAction": position_action, "positionSide": str(position_side or "").upper(),
        "closedCandleTime": candle_time,
    }
    event_map = {
        "FIRST_REJECTION": "FIRST_REJECTION", "REPEATED_REJECTION": "REPEATED_REJECTION",
        "FAILED_BREAKOUT": "FAILED_BREAKOUT", "CONFIRMED_REVERSAL_RISK": "FAILED_BREAKOUT",
    }
    events: list[dict] = []
    old_state, old_support = str(previous.get("state") or "NONE"), previous_support
    old_position = str(previous.get("positionRiskState") or "POSITION_HEALTHY")
    if rejection_state != old_state and rejection_state in event_map:
        events.append({"event_type": event_map[rejection_state], "currentState": rejection_state,
                       "previousState": old_state, "marketBias": result["marketBias"],
                       "marketBiasState": bias_state, "rejectionScore": rejection_score,
                       "rejectionReasons": rejection_reasons, "notificationEligible": True})
    if support_state != old_support and support_state in {"BROKEN_CONFIRMED", "RECLAIMED", "HELD"}:
        events.append({"event_type": "SUPPORT_BROKEN" if support_state == "BROKEN_CONFIRMED"
                       else "SUPPORT_RECLAIMED", "currentState": support_state,
                       "previousState": old_support, "supportLevel": support_edge,
                       "supportRole": support_role, "marketBiasState": bias_state,
                       "notificationEligible": True})
    if position_risk != old_position and position_risk != "POSITION_HEALTHY":
        events.append({"event_type": position_risk, "currentState": position_risk,
                       "previousState": old_position, "positionAction": position_action,
                       "marketBiasState": bias_state, "notificationEligible": True})
    for event in events:
        event.update({"side": side, "closedBarTimestamp": candle_time,
                      "semanticState": f"{bias_state}|{rejection_state}|{support_state}|{position_risk}"})
    return result, events


def evaluate_intrabar_support_pressure(previous: dict, *,
                                       current_price: float) -> tuple[dict, list[dict]]:
    """Apply only a live support-cross warning; never invent a close confirmation."""
    result = dict(previous or {})
    side = str(result.get("side") or "LONG")
    level = _number(result.get("supportLevel"))
    current = _number(current_price)
    crossed = bool(level is not None and current is not None and (
        (side == "LONG" and current < level) or
        (side == "SHORT" and current > level)))
    if not crossed or str(result.get("supportState")) == "BROKEN_CONFIRMED":
        return result, []
    old_support = str(result.get("supportState") or "SAFE")
    old_position = str(result.get("positionRiskState") or "POSITION_HEALTHY")
    if old_support == "BROKEN_PENDING_CLOSE":
        return result, []
    result.update({
        "supportState": "BROKEN_PENDING_CLOSE", "supportRole": "TESTING_ROLE_FLIP",
        "biasState": "BULLISH_WEAKENING" if side == "LONG" else "BEARISH_WEAKENING",
        "marketBias": "BULLISH" if side == "LONG" else "BEARISH",
        "setupQuality": "POOR", "entryEligibility": "NO",
        "positionRiskState": ("POSITION_DEFENSIVE"
                              if str(result.get("positionSide") or "") == side
                              else str(result.get("positionRiskState") or
                                       "POSITION_HEALTHY")),
        "positionAction": ("REDUCE" if str(result.get("positionSide") or "") == side
                           else str(result.get("positionAction") or "HOLD")),
    })
    events = [{
        "event_type": "SUPPORT_BREAK_PENDING_CLOSE",
        "previousState": old_support, "currentState": "BROKEN_PENDING_CLOSE",
        "supportLevel": level, "side": side, "marketBiasState": result["biasState"],
        "notificationEligible": True,
    }]
    if result["positionRiskState"] != old_position:
        events.append({
            "event_type": "POSITION_DEFENSIVE", "previousState": old_position,
            "currentState": "POSITION_DEFENSIVE", "positionAction": "REDUCE",
            "supportLevel": level, "side": side,
            "marketBiasState": result["biasState"], "notificationEligible": True,
        })
    return result, events
