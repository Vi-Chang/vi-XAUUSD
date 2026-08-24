"""Closed-candle price-behavior classifier, independent from market bias."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from app.config import get_settings
from app.engines.indicators import atr, macd, stochastic

BEHAVIORS = {
    "STRONG_RISE", "SLOW_RISE", "RANGE", "PULLBACK",
    "SLOW_BEARISH_DRIFT", "STRONG_DECLINE", "REBOUND",
    "REVERSAL_WARNING", "REVERSAL_CONFIRMED",
}


def _safe(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if np.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _metrics(frame: pd.DataFrame | None) -> dict:
    if frame is None or len(frame) < 4:
        return {"valid": False}
    sample = frame.tail(8).copy()
    closes = sample["close"].astype(float)
    highs = sample["high"].astype(float)
    lows = sample["low"].astype(float)
    opens = sample["open"].astype(float)
    unit = max(_safe(atr(sample).iloc[-1]), _safe((highs - lows).median()), .01)
    slope = _safe(np.polyfit(np.arange(len(closes)), closes, 1)[0]) / unit
    higher_highs = int((highs.diff() > 0).sum())
    lower_highs = int((highs.diff() < 0).sum())
    higher_closes = int((closes.diff() > 0).sum())
    lower_closes = int((closes.diff() < 0).sum())
    span = (highs - lows).replace(0, np.nan)
    bull_body = _safe(((closes - opens).clip(lower=0) / span).tail(3).mean())
    bear_body = _safe(((opens - closes).clip(lower=0) / span).tail(3).mean())
    volumes = sample.get("volume", pd.Series(0.0, index=sample.index)).astype(float)
    median_volume = max(_safe(volumes.iloc[:-1].median()), 1.0)
    volume_ratio = _safe(volumes.iloc[-1]) / median_volume
    macd_hist = macd(closes)["macd_hist"]
    stoch_k = stochastic(sample)["stoch_k"]
    return {
        "valid": True, "bars": len(sample), "atr": round(unit, 5),
        "slopeAtrPerBar": round(slope, 4),
        "moveAtr": round((closes.iloc[-1] - closes.iloc[0]) / unit, 4),
        "higherHighs": higher_highs, "lowerHighs": lower_highs,
        "higherCloses": higher_closes, "lowerCloses": lower_closes,
        "bullBodyRatio": round(bull_body, 4), "bearBodyRatio": round(bear_body, 4),
        "volumeRatio": round(volume_ratio, 4),
        "macdDirection": "UP" if _safe(macd_hist.diff().tail(3).mean()) > 0 else "DOWN",
        "stochasticDirection": "UP" if _safe(stoch_k.diff().tail(3).mean()) > 0 else "DOWN",
        "brokeRecentHigh": bool(closes.iloc[-1] > highs.iloc[:-1].tail(5).max()),
        "brokeRecentLow": bool(closes.iloc[-1] < lows.iloc[:-1].tail(5).min()),
        "lastClose": round(_safe(closes.iloc[-1]), 5),
        "candleTime": str(sample.index[-1]),
    }


def _base_scores(m: dict) -> dict[str, int]:
    if not m.get("valid"):
        return {name: 0 for name in BEHAVIORS} | {"RANGE": 100}
    pairs = max(int(m["bars"]) - 1, 1)
    up_structure = (m["higherHighs"] + m["higherCloses"]) / (2 * pairs)
    down_structure = (m["lowerHighs"] + m["lowerCloses"]) / (2 * pairs)
    up_momentum = int(m["macdDirection"] == "UP") + int(m["stochasticDirection"] == "UP")
    down_momentum = 2 - up_momentum
    rise_strength = min(abs(max(m["moveAtr"], 0)) / 1.5, 1)
    fall_strength = min(abs(min(m["moveAtr"], 0)) / 1.5, 1)
    strong_rise = (25 * rise_strength + 20 * min(max(m["slopeAtrPerBar"], 0) / .35, 1)
                   + 20 * m["bullBodyRatio"] + 15 * min(m["volumeRatio"] / 1.5, 1)
                   + 20 * int(m["brokeRecentHigh"]))
    strong_decline = (25 * fall_strength + 20 * min(abs(min(m["slopeAtrPerBar"], 0)) / .35, 1)
                      + 20 * m["bearBodyRatio"] + 15 * min(m["volumeRatio"] / 1.5, 1)
                      + 20 * int(m["brokeRecentLow"]))
    slow_rise = 45 * up_structure + 30 * min(max(m["slopeAtrPerBar"], 0) / .20, 1) + 12.5 * up_momentum
    slow_decline = 45 * down_structure + 30 * min(abs(min(m["slopeAtrPerBar"], 0)) / .20, 1) + 12.5 * down_momentum
    range_score = 100 - min(abs(m["slopeAtrPerBar"]) * 220, 45) - min(abs(m["moveAtr"]) * 20, 45)
    return {
        "STRONG_RISE": round(strong_rise), "SLOW_RISE": round(slow_rise),
        "RANGE": round(max(0, range_score)), "PULLBACK": 0,
        "SLOW_BEARISH_DRIFT": round(slow_decline),
        "STRONG_DECLINE": round(strong_decline), "REBOUND": 0,
        "REVERSAL_WARNING": 0, "REVERSAL_CONFIRMED": 0,
    }


def _classify(frame: pd.DataFrame | None, *, bias: str, normalized: dict) -> dict:
    m = _metrics(frame)
    scores = _base_scores(m)
    support_state = str(normalized.get("supportState") or "none")
    structure_broken = support_state in {"confirmed_breakdown", "retest_rejected"}
    reversal_risk = str(normalized.get("shortTermMomentum")) == "reversal_risk"
    if bias == "BULLISH" and not structure_broken:
        support_test = support_state in {"testing_support", "failed_breakdown"}
        if support_test and scores["SLOW_BEARISH_DRIFT"] >= 45:
            scores["PULLBACK"] = min(100, scores["SLOW_BEARISH_DRIFT"] + 12)
        if reversal_risk and scores["SLOW_BEARISH_DRIFT"] >= 50:
            scores["REVERSAL_WARNING"] = min(100, scores["SLOW_BEARISH_DRIFT"] + 10)
    if bias == "BEARISH" and not structure_broken and scores["SLOW_RISE"] >= 50:
        scores["REBOUND"] = min(100, scores["SLOW_RISE"] + 10)
    htf_confirmed = bool(normalized.get("higherTimeframeReversalConfirmed"))
    forced_behavior = None
    if structure_broken and bias == "BULLISH":
        forced_behavior = "REVERSAL_CONFIRMED" if htf_confirmed else "REVERSAL_WARNING"
        scores[forced_behavior] = 90 if htf_confirmed else 75
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    behavior, confidence = ((forced_behavior, scores[forced_behavior])
                            if forced_behavior else ordered[0])
    minimum = get_settings().min_behavior_confidence
    if confidence < minimum:
        behavior, confidence = "RANGE", scores["RANGE"]
    secondary = ordered[1][0] if behavior == "PULLBACK" and ordered[1][1] >= minimum else None
    return {"behavior": behavior, "confidence": int(max(0, min(100, confidence))),
            "secondaryBehavior": secondary, "scores": scores, "metrics": m}


def evaluate_market_behavior(*, m15: pd.DataFrame | None, h1: pd.DataFrame | None,
                             h4: pd.DataFrame | None, data: dict,
                             previous: dict | None = None) -> tuple[dict, list[dict]]:
    previous = previous or {}
    normalized = data.get("normalized_analysis") or {}
    break_state = data.get("break_lifecycle_engine") or {}
    normalized = dict(normalized)
    if break_state.get("state") in {"FAILED_BREAKDOWN", "LIQUIDITY_SWEEP_CANDIDATE"}:
        normalized["supportState"] = "failed_breakdown"
        normalized["higherTimeframeReversalConfirmed"] = False
    elif break_state.get("state") == "BREAK_CONFIRMATION_PENDING":
        normalized["supportState"] = "testing_support"
    bias = str(normalized.get("trendBias") or "neutral").upper()
    results = {"15M": _classify(m15, bias=bias, normalized=normalized),
               "1H": _classify(h1, bias=bias, normalized={}),
               "4H": _classify(h4, bias=bias, normalized={})}
    proposed = results["15M"]["behavior"]
    score = results["15M"]["confidence"]
    rejection = data.get("wick_rejection_engine") or {}
    override = rejection.get("behavior_override")
    if override:
        proposed = str(override)
        score = max(50, min(85, score - int(rejection.get("wick_rejection_penalty") or 0)))
    old = str(previous.get("market_behavior") or "RANGE")
    if (old != "RANGE" and proposed == "RANGE"
            and int(results["15M"]["scores"].get(old) or 0)
            >= get_settings().behavior_exit_threshold):
        proposed = old
        score = int(results["15M"]["scores"].get(old) or score)
    candle_time = str(results["15M"]["metrics"].get("candleTime") or
                      normalized.get("lastClosedCandleTimestamp") or "")
    same_candle = candle_time == str(previous.get("last_evaluated_candle") or "")
    pending = str(previous.get("pending_behavior") or "")
    count = int(previous.get("pending_count") or 0)
    if proposed == old:
        behavior, pending, count = old, "", 0
    elif score >= get_settings().behavior_immediate_threshold:
        behavior, pending, count = proposed, "", 0
    else:
        count = count if same_candle and pending == proposed else count + 1 if pending == proposed else 1
        pending = proposed
        behavior = proposed if count >= get_settings().behavior_confirmation_bars else old
        if behavior == proposed:
            pending, count = "", 0
    if behavior != proposed:
        score = int(results["15M"]["scores"].get(behavior)
                    or previous.get("behavior_confidence") or 0)
    now = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    changed = behavior != old
    since = now if changed else str(previous.get("behavior_since") or now)
    metrics = results["15M"]["metrics"]
    reasons = [
        f"15M slope={metrics.get('slopeAtrPerBar', 0):.2f} ATR/bar",
        f"lower highs={metrics.get('lowerHighs', 0)}, lower closes={metrics.get('lowerCloses', 0)}",
        f"MACD {metrics.get('macdDirection', 'UNKNOWN')}, KD {metrics.get('stochasticDirection', 'UNKNOWN')}",
    ]
    output = {
        "schemaVersion": "market-behavior-v1", "market_bias": bias,
        "market_behavior": behavior, "behavior_confidence": score,
        "secondary_behavior": results["15M"].get("secondaryBehavior"),
        "behavior_since": since, "behavior_reason": reasons,
        "behavior_invalidated_by": "下一個已收盤K棒使結構與分數退出目前狀態",
        "behavior_15m": behavior, "behavior_1h": results["1H"]["behavior"],
        "behavior_4h": results["4H"]["behavior"],
        "structure_status": "BULLISH_INTACT" if bias == "BULLISH" and
        str(normalized.get("supportState")) not in {"confirmed_breakdown", "retest_rejected"}
        else "BEARISH_INTACT" if bias == "BEARISH" else "STRUCTURE_AT_RISK",
        "momentum_status": ("SHORT_TERM_BEARISH" if behavior in {
            "SLOW_BEARISH_DRIFT", "STRONG_DECLINE", "REVERSAL_WARNING"}
            else "SHORT_TERM_BULLISH" if behavior in {"SLOW_RISE", "STRONG_RISE", "REBOUND"}
            else "NEUTRAL"),
        "scores": results["15M"]["scores"], "metrics": metrics,
        "wick_rejection": rejection,
        "pending_behavior": pending, "pending_count": count,
        "last_evaluated_candle": candle_time,
        "market_regime": break_state.get("market_regime") or "NORMAL",
        "break_lifecycle": break_state,
        "position_size_multiplier": break_state.get("position_size_multiplier", 1.0),
    }
    events = []
    if changed:
        event_id = hashlib.sha256(f"XAUUSD|BEHAVIOR|{old}|{behavior}|{candle_time}".encode()).hexdigest()[:32]
        events.append({"eventId": event_id, "event_type": "MARKET_BEHAVIOR_CHANGED",
                       "previousBehavior": old, "marketBehavior": behavior,
                       "behaviorConfidence": score, "marketBias": bias,
                       "candleCloseTime": candle_time, "notificationEligible": True,
                       "transitionReason": "；".join(reasons)})
    return output, events
