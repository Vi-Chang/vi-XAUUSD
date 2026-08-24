"""Closed-candle wick rejection and breakout confirmation engine.

This module is the sole calculator for wick rejection. Consumers must use its
canonical result rather than reinterpreting candle shadows independently.
"""
from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pandas as pd

from app.config import get_settings
from app.engines.indicators import atr, macd, stochastic


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _location_quality(zone_low: float, zone_high: float, levels: list[dict],
                      tolerance: float) -> tuple[str, list[str]]:
    reasons: list[str] = []
    for item in levels:
        price = _finite(item.get("price"), float("nan"))
        if not np.isfinite(price) or price < zone_low - tolerance or price > zone_high + tolerance:
            continue
        kind = str(item.get("kind") or "").lower()
        timeframe = str(item.get("timeframe") or item.get("source") or "")
        reasons.append(f"{timeframe or '結構'} {kind or 'level'}")
    if any(any(tf in reason.upper() for tf in ("1H", "4H", "1D", "HTF")) for reason in reasons):
        return "HIGH", reasons
    if reasons:
        return "MEDIUM", reasons
    return "LOW", reasons


def _cluster(prices: list[tuple[float, int]], tolerance: float) -> list[list[tuple[float, int]]]:
    clusters: list[list[tuple[float, int]]] = []
    for price, index in sorted(prices):
        if not clusters or price - max(value for value, _ in clusters[-1]) > tolerance:
            clusters.append([(price, index)])
        else:
            clusters[-1].append((price, index))
    return clusters


def evaluate_wick_rejection(frame: pd.DataFrame | None, *, data: dict,
                            previous: dict | None = None) -> tuple[dict, list[dict]]:
    """Evaluate only completed 15M candles; output is long/short symmetric."""
    settings = get_settings()
    if frame is None or len(frame) < 3:
        return ({"schemaVersion": "wick-rejection-v1",
                 "wick_rejection_state": "NO_SIGNIFICANT_REJECTION",
                 "wick_rejection_score": 0, "wick_rejection_zone": None,
                 "wick_rejection_count": 0, "wick_rejection_strength": "NONE",
                 "rejection_location_quality": "LOW", "momentum_price_conflict": "NONE",
                 "wick_rejection_penalty": 0, "breakout_state": "NONE",
                 "failed_breakout_count": 0}, [])
    normalized = data.get("normalized_analysis") or {}
    sample = frame.tail(int(settings.wick_rejection_lookback_bars)).copy()
    ranges = (sample["high"].astype(float) - sample["low"].astype(float)).replace(0, np.nan)
    opens, closes = sample["open"].astype(float), sample["close"].astype(float)
    highs, lows = sample["high"].astype(float), sample["low"].astype(float)
    unit = max(_finite(atr(frame).iloc[-1]), _finite(ranges.median()), .01)
    upper = highs - np.maximum(opens, closes)
    lower = np.minimum(opens, closes) - lows
    body = (closes - opens).abs()
    volumes = sample.get("volume", pd.Series(0.0, index=sample.index)).astype(float)
    median_volume = max(_finite(volumes.replace(0, np.nan).median()), 1.0)
    sig_upper: list[tuple[float, int]] = []
    sig_lower: list[tuple[float, int]] = []
    candle_metrics: list[dict] = []
    for index in range(len(sample)):
        span = max(_finite(ranges.iloc[index]), .01)
        volume_ratio = _finite(volumes.iloc[index]) / median_volume
        upper_ratio, lower_ratio = _finite(upper.iloc[index]) / span, _finite(lower.iloc[index]) / span
        upper_atr, lower_atr = _finite(upper.iloc[index]) / unit, _finite(lower.iloc[index]) / unit
        upper_sig = (upper_ratio >= settings.wick_ratio_threshold and
                     upper_atr >= settings.wick_atr_ratio_threshold and
                     _finite(closes.iloc[index]) <= _finite(highs.iloc[index]) -
                     settings.wick_close_from_extreme_atr * unit)
        lower_sig = (lower_ratio >= settings.wick_ratio_threshold and
                     lower_atr >= settings.wick_atr_ratio_threshold and
                     _finite(closes.iloc[index]) >= _finite(lows.iloc[index]) +
                     settings.wick_close_from_extreme_atr * unit)
        if volume_ratio < settings.wick_min_volume_ratio:
            upper_sig = lower_sig = False
        if upper_sig:
            sig_upper.append((_finite(highs.iloc[index]), index))
        if lower_sig:
            sig_lower.append((_finite(lows.iloc[index]), index))
        candle_metrics.append({"time": str(sample.index[index]), "bodyRatio": round(_finite(body.iloc[index]) / span, 4),
                               "upperWickRatio": round(upper_ratio, 4), "lowerWickRatio": round(lower_ratio, 4),
                               "upperWickAtrRatio": round(upper_atr, 4), "lowerWickAtrRatio": round(lower_atr, 4),
                               "volumeRatio": round(volume_ratio, 4), "significantUpper": upper_sig,
                               "significantLower": lower_sig})
    tolerance = unit * settings.wick_cluster_tolerance_atr
    upper_clusters, lower_clusters = _cluster(sig_upper, tolerance), _cluster(sig_lower, tolerance)
    upper_best = max(upper_clusters, key=len, default=[])
    lower_best = max(lower_clusters, key=len, default=[])
    upper_wins = len(upper_best) >= len(lower_best)
    chosen = upper_best if upper_wins else lower_best
    count = len(chosen)
    repeated = count >= settings.wick_repeated_min_count
    direction = "UPPER" if upper_wins else "LOWER"
    state = (f"REPEATED_{direction}_WICK_REJECTION" if repeated else
             f"SINGLE_{direction}_WICK_REJECTION" if count else "NO_SIGNIFICANT_REJECTION")
    zone = ({"low": round(min(value for value, _ in chosen), 2),
             "high": round(max(value for value, _ in chosen), 2)} if chosen else None)
    levels = list(normalized.get("confirmationLevels") or [])
    quality, location_reasons = _location_quality(zone["low"], zone["high"], levels, tolerance) if zone else ("LOW", [])
    age_weights = [settings.wick_recency_decay ** (len(sample) - 1 - index) for _, index in chosen]
    recency = sum(age_weights) / max(len(age_weights), 1)
    location_mult = {"HIGH": 1.25, "MEDIUM": 1.0, "LOW": .65}[quality]
    score = min(100, round((25 if count else 0) + count * 18 * recency * location_mult))
    strength = "STRONG" if score >= 70 else "MEDIUM" if score >= 45 else "WEAK" if score else "NONE"
    hist = macd(frame["close"].astype(float))["macd_hist"]
    kd = stochastic(frame)["stoch_k"]
    bullish_momentum = _finite(hist.diff().tail(3).mean()) > 0 and _finite(kd.diff().tail(3).mean()) > 0
    bearish_momentum = _finite(hist.diff().tail(3).mean()) < 0 and _finite(kd.diff().tail(3).mean()) < 0
    conflict = ("BULLISH_MOMENTUM_BUT_PRICE_REJECTED" if repeated and direction == "UPPER" and bullish_momentum else
                "BEARISH_MOMENTUM_BUT_PRICE_SUPPORTED" if repeated and direction == "LOWER" and bearish_momentum else "NONE")
    base_penalty = ({"NONE": 0, "WEAK": 5, "MEDIUM": 15, "STRONG": 25}[strength]
                    if repeated else 5 if count else 0)
    penalty = min(settings.wick_rejection_max_penalty, round(base_penalty * location_mult))
    resistance = zone["low"] if zone and direction == "UPPER" else None
    support = zone["high"] if zone and direction == "LOWER" else None
    buffer = unit * settings.wick_breakout_buffer_atr
    last_close = _finite(closes.iloc[-1])
    prev_close = _finite(closes.iloc[-2])
    breakout = "NONE"
    failures = 0
    if resistance is not None:
        failures = sum(1 for high, idx in sig_upper if high >= resistance and _finite(closes.iloc[idx]) <= zone["high"])
        breakout = ("BREAKOUT_CONFIRMED" if last_close > zone["high"] + buffer and prev_close > zone["high"] else
                    "BREAKOUT_FAILED" if failures >= settings.wick_repeated_min_count else
                    "BREAKOUT_ATTEMPT" if _finite(highs.iloc[-1]) > resistance and last_close <= zone["high"] else "NONE")
    elif support is not None:
        failures = sum(1 for low, idx in sig_lower if low <= support and _finite(closes.iloc[idx]) >= zone["low"])
        breakout = ("BREAKOUT_CONFIRMED" if last_close < zone["low"] - buffer and prev_close < zone["low"] else
                    "BREAKOUT_FAILED" if failures >= settings.wick_repeated_min_count else
                    "BREAKOUT_ATTEMPT" if _finite(lows.iloc[-1]) < support and last_close >= zone["low"] else "NONE")
    behavior_override = ("BULLISH_ATTEMPT_WITH_REJECTION" if conflict == "BULLISH_MOMENTUM_BUT_PRICE_REJECTED" else
                         "BEARISH_ATTEMPT_WITH_SUPPORT" if conflict == "BEARISH_MOMENTUM_BUT_PRICE_SUPPORTED" else None)
    output = {"schemaVersion": "wick-rejection-v1", "wick_rejection_state": state,
              "wick_rejection_score": score, "wick_rejection_zone": zone,
              "wick_rejection_count": count, "wick_rejection_strength": strength,
              "rejection_location_quality": quality, "rejection_location_reasons": location_reasons,
              "rejection_recency_weight": round(recency, 4), "momentum_price_conflict": conflict,
              "wick_rejection_penalty": penalty, "breakout_state": breakout,
              "failed_breakout_count": failures, "behavior_override": behavior_override,
              "atr": round(unit, 5), "cluster_tolerance": round(tolerance, 5),
              "zone_touch_count": count, "zone_rejection_count": count,
              "last_closed_candle_time": str(sample.index[-1]), "candle_metrics": candle_metrics}
    old_state = str((previous or {}).get("wick_rejection_state") or "NO_SIGNIFICANT_REJECTION")
    old_breakout = str((previous or {}).get("breakout_state") or "NONE")
    events = []
    if state != old_state or breakout != old_breakout:
        event_type = "WICK_REJECTION_CHANGED" if state != old_state else "REJECTION_BREAKOUT_CHANGED"
        event_key = f"XAUUSD|15M|{event_type}|{state}|{breakout}|{sample.index[-1]}"
        events.append({"eventId": hashlib.sha256(event_key.encode()).hexdigest()[:32],
                       "event_type": event_type,
                       "previousState": old_state, "currentState": state, "breakoutState": breakout,
                       "zone": zone, "candleCloseTime": str(sample.index[-1]),
                       "notificationEligible": True})
    return output, events
