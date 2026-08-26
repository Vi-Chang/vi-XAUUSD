"""Relative tick/quote-volume and closed-candle interpretation.

The engine deliberately treats provider volume as relative activity only.  It
never emits a trading direction by itself and never compares raw 15M volume
with raw 1H volume.
"""
from __future__ import annotations

import math
from datetime import timedelta

import pandas as pd

from app.config import get_settings


def _finite(value, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def market_session(value) -> str:
    """Return a stable UTC liquidity session for XAUUSD activity baselines."""
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    hour = stamp.hour
    if 0 <= hour < 7:
        return "ASIA"
    if 7 <= hour < 13:
        return "EUROPE"
    if 13 <= hour < 16:
        return "EUROPE_US_OVERLAP"
    return "US"


def _volume_state(ratio: float | None) -> str:
    if ratio is None:
        return "UNAVAILABLE"
    settings = get_settings()
    if ratio < settings.volume_very_low_ratio:
        return "VERY_LOW"
    if ratio < settings.volume_low_ratio:
        return "LOW"
    if ratio < settings.volume_high_ratio:
        return "NORMAL"
    if ratio < settings.volume_very_high_ratio:
        return "HIGH"
    if ratio < settings.volume_extreme_ratio:
        return "VERY_HIGH"
    return "EXTREME"


def candle_anatomy(frame: pd.DataFrame, *, atr: float = 0.0) -> dict:
    if frame is None or frame.empty:
        return {"candleType": "UNAVAILABLE"}
    settings = get_settings()
    row = frame.iloc[-1]
    opened, high, low, close = (_finite(row.get(key)) for key in (
        "open", "high", "low", "close"))
    candle_range = max(high-low, 1e-9)
    body = abs(close-opened)
    upper = max(0.0, high-max(opened, close))
    lower = max(0.0, min(opened, close)-low)
    body_ratio = body/candle_range
    upper_ratio, lower_ratio = upper/candle_range, lower/candle_range
    close_location = (close-low)/candle_range
    atr_ratio = candle_range/max(atr, 1e-9) if atr > 0 else None
    if upper_ratio >= settings.candle_rejection_wick_ratio and close_location < .65:
        candle_type = "REJECTION_UP"
    elif lower_ratio >= settings.candle_rejection_wick_ratio and close_location > .35:
        candle_type = "REJECTION_DOWN"
    elif body_ratio <= settings.candle_weak_body_ratio/2:
        candle_type = "DOJI"
    elif (close > opened and body_ratio >= settings.candle_strong_body_ratio and
          close_location >= 1-settings.candle_close_edge_ratio):
        candle_type = "STRONG_BULLISH_CLOSE"
    elif (close < opened and body_ratio >= settings.candle_strong_body_ratio and
          close_location <= settings.candle_close_edge_ratio):
        candle_type = "STRONG_BEARISH_CLOSE"
    elif close > opened:
        candle_type = "WEAK_BULLISH_CLOSE"
    elif close < opened:
        candle_type = "WEAK_BEARISH_CLOSE"
    else:
        candle_type = "DOJI"
    if atr_ratio is not None and atr_ratio <= settings.candle_compression_atr_ratio:
        expansion = "COMPRESSION"
    elif atr_ratio is not None and atr_ratio >= settings.candle_expansion_atr_ratio:
        expansion = "EXPANSION"
    else:
        expansion = "NORMAL_RANGE"
    return {
        "candleType": candle_type, "rangeState": expansion,
        "bodySize": round(body, 5), "bodyRatio": round(body_ratio, 4),
        "upperWick": round(upper, 5), "upperWickRatio": round(upper_ratio, 4),
        "lowerWick": round(lower, 5), "lowerWickRatio": round(lower_ratio, 4),
        "closeLocation": round(close_location, 4), "range": round(candle_range, 5),
        "atrRatio": round(atr_ratio, 4) if atr_ratio is not None else None,
        "open": opened, "high": high, "low": low, "close": close,
    }


def relative_volume_engine(frame: pd.DataFrame, *, timeframe: str,
                           atr: float = 0.0) -> dict:
    if frame is None or frame.empty or "volume" not in frame:
        return {"timeframe": timeframe, "available": False,
                "volumeState": "UNAVAILABLE", "reason": "缺少成交活躍度資料"}
    settings = get_settings()
    sample = frame.tail(max(settings.volume_history_bars+1, 21)).copy()
    values = pd.to_numeric(sample["volume"], errors="coerce").fillna(0.0)
    current = _finite(values.iloc[-1])
    history = values.iloc[:-1]
    positive = history[history > 0]
    if current <= 0 or positive.empty:
        return {"timeframe": timeframe, "available": False,
                "currentVolume": current, "volumeState": "UNAVAILABLE",
                "reason": "資料源未提供可比較的 tick／quote volume"}
    close_offset = timedelta(minutes=15 if timeframe.upper() == "15M" else 60)
    sessions = pd.Series(
        [market_session(pd.Timestamp(index)+close_offset) for index in sample.index],
        index=sample.index)
    current_session = str(sessions.iloc[-1])
    session_history = history[sessions.iloc[:-1] == current_session]
    session_history = session_history[session_history > 0]
    baseline = (session_history if len(session_history) >=
                settings.volume_session_min_samples else positive)
    sma = {window: _finite(positive.tail(window).mean()) for window in (5, 10, 20)}
    ratio5 = current/sma[5] if sma[5] > 0 else None
    ratio20 = current/sma[20] if sma[20] > 0 else None
    session_mean = _finite(baseline.mean())
    session_ratio = current/session_mean if session_mean > 0 else ratio20
    mean, std = _finite(positive.mean()), _finite(positive.std(ddof=0))
    zscore = (current-mean)/std if std > 0 else 0.0
    percentile = float((positive <= current).mean()*100)
    effective_ratio = session_ratio if session_ratio is not None else ratio20
    return {
        "timeframe": timeframe, "available": True,
        "sourceType": "RELATIVE_TICK_OR_QUOTE_ACTIVITY",
        "currentVolume": round(current, 4),
        "volumeSma5": round(sma[5], 4), "volumeSma10": round(sma[10], 4),
        "volumeSma20": round(sma[20], 4),
        "volumeRatio5": round(ratio5, 4) if ratio5 is not None else None,
        "volumeRatio20": round(ratio20, 4) if ratio20 is not None else None,
        "session": current_session,
        "sessionBaseline": round(session_mean, 4),
        "sessionNormalizedRatio": round(session_ratio, 4)
        if session_ratio is not None else None,
        "volumePercentile": round(percentile, 2), "volumeZscore": round(zscore, 4),
        "volumeState": _volume_state(effective_ratio),
        "effectiveRelativeVolume": round(effective_ratio, 4)
        if effective_ratio is not None else None,
        "sessionSampleCount": len(session_history),
        "candleAnatomy": candle_anatomy(sample, atr=atr),
    }


def volume_candle_interpretation(frame: pd.DataFrame, relative: dict, *,
                                 structural_bias: str = "NEUTRAL") -> dict:
    if frame is None or len(frame) < 2 or not relative.get("available"):
        return {"volumePriceState": "UNAVAILABLE", "longScoreImpact": 0,
                "shortScoreImpact": 0, "breakoutQualityScore": 0,
                "breakoutQuality": "UNAVAILABLE", "reasons": []}
    settings = get_settings()
    anatomy = relative.get("candleAnatomy") or {}
    current, previous = frame.iloc[-1], frame.iloc[-2]
    close, prev_close = _finite(current.get("close")), _finite(previous.get("close"))
    history = frame.iloc[:-1].tail(20)
    prior_high = _finite(history["high"].max()) if not history.empty else close
    prior_low = _finite(history["low"].min()) if not history.empty else close
    rising, falling = close > prev_close, close < prev_close
    state = str(relative.get("volumeState") or "UNAVAILABLE")
    high_volume = state in {"HIGH", "VERY_HIGH", "EXTREME"}
    low_volume = state in {"LOW", "VERY_LOW"}
    candle_type = str(anatomy.get("candleType") or "UNAVAILABLE")
    bullish_break = close > prior_high
    bearish_break = close < prior_low
    long_impact = short_impact = 0
    reasons: list[str] = []
    volume_price_state = "NORMAL_ACTIVITY"
    if high_volume and bullish_break and candle_type == "STRONG_BULLISH_CLOSE":
        volume_price_state = "BULLISH_BREAKOUT_VOLUME"
        long_impact += settings.volume_breakout_score_bonus
        reasons.append("價格收盤突破壓力且成交活躍度明顯增加")
    elif high_volume and bearish_break and candle_type == "STRONG_BEARISH_CLOSE":
        volume_price_state = "BEARISH_BREAKOUT_VOLUME"
        short_impact += settings.volume_breakout_score_bonus
        reasons.append("價格收盤跌破支撐且成交活躍度明顯增加")
    elif high_volume and candle_type == "REJECTION_UP":
        volume_price_state = "BUYING_ABSORPTION"
        short_impact += settings.volume_confirmation_score_bonus
        reasons.append("高成交活躍度伴隨長上影與偏弱收盤，買盤遭到吸收")
    elif high_volume and candle_type == "REJECTION_DOWN":
        volume_price_state = "SELLING_ABSORPTION"
        long_impact += settings.volume_confirmation_score_bonus
        reasons.append("高成交活躍度伴隨長下影並收回，賣壓遭到吸收")
    elif high_volume and rising:
        volume_price_state = "VOLUME_CONFIRMED_RISE"
        long_impact += settings.volume_confirmation_score_bonus
        reasons.append("價格上漲且相對成交活躍度增加")
    elif high_volume and falling:
        volume_price_state = "VOLUME_CONFIRMED_DROP"
        short_impact += settings.volume_confirmation_score_bonus
        reasons.append("價格下跌且相對成交活躍度增加")
    elif low_volume and rising:
        volume_price_state = "LOW_VOLUME_RISE"
        long_impact -= settings.volume_quality_score_penalty
        reasons.append("價格上漲但參與度下降，續漲品質降低")
    elif low_volume and falling:
        volume_price_state = "LOW_VOLUME_DROP"
        short_impact -= settings.volume_quality_score_penalty
        reasons.append("價格下跌但參與度下降，只代表賣壓參與減弱")
    bias = structural_bias.upper()
    recent_range = _finite(history.tail(6)["high"].max()-history.tail(6)["low"].min())
    current_range = _finite(current.get("high"))- _finite(current.get("low"))
    if (low_volume and str(anatomy.get("rangeState")) == "COMPRESSION" and
            current_range <= max(recent_range, 1e-9)):
        volume_price_state = "LOW_VOLUME_CONSOLIDATION"
        reasons.append("價格與成交活躍度同步收斂，等待區間選方向")
    if low_volume and bias in {"BULLISH", "LONG"} and falling and not bearish_break:
        volume_price_state = "LOW_VOLUME_PULLBACK_LONG"
        long_impact += settings.volume_low_pullback_score_bonus
        reasons.append("多方結構內縮量回踩，賣壓有限但仍需價格止跌確認")
    elif low_volume and bias in {"BEARISH", "SHORT"} and rising and not bullish_break:
        volume_price_state = "LOW_VOLUME_PULLBACK_SHORT"
        short_impact += settings.volume_low_pullback_score_bonus
        reasons.append("空方結構內縮量反彈，買盤有限但仍需價格轉弱確認")
    # Divergence is a quality downgrade only, never an opposite entry.
    if len(frame) >= 8:
        recent = frame.tail(8)
        prices, volumes = recent["close"], recent["volume"]
        if prices.iloc[-1] >= prices.iloc[:-1].max() and volumes.iloc[-1] < volumes.iloc[:-1].max():
            relative["volumeDivergence"] = "BULLISH_TREND_WEAKENING"
            long_impact -= settings.volume_quality_score_penalty
        elif prices.iloc[-1] <= prices.iloc[:-1].min() and volumes.iloc[-1] < volumes.iloc[:-1].max():
            relative["volumeDivergence"] = "BEARISH_TREND_WEAKENING"
            short_impact -= settings.volume_quality_score_penalty
    quality_score = 50
    quality_score += long_impact if bullish_break or rising else short_impact if bearish_break or falling else 0
    if anatomy.get("rangeState") == "EXPANSION":
        quality_score += 8
    if candle_type in {"REJECTION_UP", "REJECTION_DOWN", "DOJI"}:
        quality_score -= 12
    quality_score = max(0, min(100, quality_score))
    quality = ("STRONG" if quality_score >= 75 else "VALID" if quality_score >= 60
               else "WEAK" if quality_score >= 42 else "SUSPECT")
    if ((bullish_break and candle_type == "REJECTION_UP") or
            (bearish_break and candle_type == "REJECTION_DOWN")):
        quality = "FALSE_BREAK"
    return {
        "volumePriceState": volume_price_state,
        "longScoreImpact": long_impact, "shortScoreImpact": short_impact,
        "breakoutQualityScore": quality_score, "breakoutQuality": quality,
        "priorResistance": round(prior_high, 5), "priorSupport": round(prior_low, 5),
        "bullishBreakout": bullish_break, "bearishBreakout": bearish_break,
        "reasons": reasons,
    }


def volume_proverb(interpretation: dict) -> dict:
    """Translate a volume/price state into a safe, memorable trading maxim.

    A maxim is presentation guidance only.  It never grants entry permission
    and deliberately avoids deterministic words such as "certain".
    """
    state = str(interpretation.get("volumePriceState") or "UNAVAILABLE")
    if state in {"VOLUME_CONFIRMED_DROP", "BEARISH_BREAKOUT_VOLUME"}:
        key, text = ("HIGH_VOLUME_DROP",
                     "放量下跌：留意急跌後反彈，但先等支撐守住或收盤收回。")
    elif state in {"VOLUME_CONFIRMED_RISE", "BULLISH_BREAKOUT_VOLUME"}:
        key, text = ("HIGH_VOLUME_RISE",
                     "放量上漲：留意高檔回落；若突破收盤站穩，也可能延續。")
    elif state == "LOW_VOLUME_RISE":
        key, text = ("LOW_VOLUME_RISE",
                     "縮量上漲：可能靠慣性續漲，但參與度偏低，不宜當成保證。")
    elif state == "LOW_VOLUME_DROP":
        key, text = ("LOW_VOLUME_DROP",
                     "縮量下跌：可能慣性走低，也可能是賣壓減弱，需看支撐結果。")
    elif state in {"BUYING_ABSORPTION", "BUYING_EXHAUSTION"}:
        key, text = ("HIGH_VOLUME_NO_RISE",
                     "放量不漲：上方可能出現吸收或壓力，需等結構轉弱確認。")
    elif state in {"LOW_VOLUME_CONSOLIDATION", "SELLING_EXHAUSTION"}:
        key, text = ("LOW_VOLUME_NO_DROP",
                     "縮量不跌：可能正在築底，仍需止跌結構與向上突破確認。")
    elif state == "SELLING_ABSORPTION":
        key, text = ("HIGH_VOLUME_RECLAIM",
                     "放量殺低後收回：賣壓可能被吸收，仍需下一根守住確認。")
    elif state == "LOW_VOLUME_PULLBACK_LONG":
        key, text = ("LOW_VOLUME_PULLBACK_LONG",
                     "縮量回踩：賣壓可能有限，等支撐止跌後再評估做多。")
    elif state == "LOW_VOLUME_PULLBACK_SHORT":
        key, text = ("LOW_VOLUME_PULLBACK_SHORT",
                     "縮量反彈：買盤可能有限，等壓力轉弱後再評估做空。")
    else:
        key, text = ("NO_CLEAR_PROVERB",
                     "量價口訣：目前沒有明確組合，只依價格結構等待確認。")
    return {"key": key, "text": text, "actionable": False,
            "requiresPriceConfirmation": True}


def evaluate_volume_intelligence(*, m15_closed: pd.DataFrame | None,
                                 h1_closed: pd.DataFrame | None,
                                 atr15: float = 0.0, atr1h: float = 0.0,
                                 structural_bias: str = "NEUTRAL") -> dict:
    timeframes: dict[str, dict] = {}
    for timeframe, frame, atr in (
            ("15M", m15_closed, atr15), ("1H", h1_closed, atr1h)):
        relative = relative_volume_engine(frame, timeframe=timeframe, atr=atr)
        interpretation = volume_candle_interpretation(
            frame, relative, structural_bias=structural_bias)
        timeframes[timeframe] = {
            **relative, **interpretation,
            "volumeProverb": volume_proverb(interpretation),
        }
    m15, h1 = timeframes["15M"], timeframes["1H"]
    return {
        "schemaVersion": "volume-intelligence-v1",
        "sourceDisclosure": "成交量為相對 tick／報價活躍度，不是集中交易所總量",
        "timeframes": timeframes,
        "volumeScore": {
            "LONG": max(-20, min(20, int(m15.get("longScoreImpact") or 0) +
                                      round(int(h1.get("longScoreImpact") or 0)*.5))),
            "SHORT": max(-20, min(20, int(m15.get("shortScoreImpact") or 0) +
                                       round(int(h1.get("shortScoreImpact") or 0)*.5))),
        },
        "primaryState": m15.get("volumePriceState") or "UNAVAILABLE",
        "session": m15.get("session") or h1.get("session") or "UNKNOWN",
    }
