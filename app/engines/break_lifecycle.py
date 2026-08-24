"""Closed-candle break/follow-through/reclaim lifecycle without look-ahead."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pandas as pd

from app.config import get_settings
from app.engines.directional_wording import format_level_cross


def _num(value, default=0.0):
    return float(value) if isinstance(value, (int, float)) else default


def _nearest_level(normalized: dict, price: float) -> tuple[str, float] | None:
    levels = [(str(x.get("kind")), float(x["price"]))
              for x in normalized.get("confirmationLevels") or []
              if x.get("kind") in {"support", "resistance"}
              and isinstance(x.get("price"), (int, float))]
    return min(levels, key=lambda item: abs(item[1] - price), default=None)


def evaluate_break_lifecycle(
    frame: pd.DataFrame | None, *, data: dict, previous: dict | None = None
) -> tuple[dict, list[dict]]:
    previous = previous or {}
    settings = get_settings()
    normalized = data.get("normalized_analysis") or {}
    if frame is None or len(frame) < 2:
        return {"schemaVersion": "break-lifecycle-v1", "state": "LEVEL_TEST",
                "break_type": "TOUCH", "break_confidence": 0,
                "follow_through_score": 0, "reclaim_confidence": 0,
                "market_regime": "NORMAL", "position_size_multiplier": 1.0}, []
    sample = frame.tail(4)
    last, prior = sample.iloc[-1], sample.iloc[-2]
    price = float(last["close"])
    chosen = _nearest_level(normalized, price)
    if not chosen:
        return {"schemaVersion": "break-lifecycle-v1", "state": "LEVEL_TEST",
                "break_type": "TOUCH", "break_confidence": 0,
                "follow_through_score": 0, "reclaim_confidence": 0,
                "market_regime": "NORMAL", "position_size_multiplier": 1.0}, []
    kind, level = chosen
    direction = "DOWN" if kind == "support" else "UP"
    atr = max(_num(normalized.get("atr15")),
              float((sample["high"] - sample["low"]).median()), .01)
    close = float(last["close"])
    extreme = float(last["low"] if direction == "DOWN" else last["high"])
    penetrated = (extreme < level if direction == "DOWN" else extreme > level)
    closed_outside = (close < level if direction == "DOWN" else close > level)
    reclaimed = penetrated and not closed_outside
    penetration_atr = abs(extreme - level) / atr if penetrated else 0.0
    close_distance_atr = abs(close - level) / atr if closed_outside else 0.0
    span = max(float(last["high"] - last["low"]), .01)
    body_ratio = abs(float(last["close"] - last["open"])) / span
    volume = _num(last.get("volume"), 0.0)
    median_volume = max(_num(sample.iloc[:-1].get("volume", pd.Series([1])).median()
                             if "volume" in sample else 1), 1.0)
    volume_ratio = volume / median_volume
    old_state = str(previous.get("state") or "LEVEL_TEST")
    old_level = _num(previous.get("level"), level)
    same_level = abs(old_level - level) <= atr * .25
    old_direction = str(previous.get("direction") or direction)
    breach_bar = int(previous.get("bars_since_breach") or 0)
    follow = 0
    if same_level and old_direction == direction and old_state in {
            "BREAK_CONFIRMATION_PENDING", "CLOSE_BREAK", "NO_FOLLOW_THROUGH",
            "FAILED_BREAKDOWN", "FAILED_BREAKOUT"}:
        extension = ((float(last["low"]) < float(prior["low"]) and close < level)
                     if direction == "DOWN" else
                     (float(last["high"]) > float(prior["high"]) and close > level))
        directional_body = (close < float(last["open"]) if direction == "DOWN"
                            else close > float(last["open"]))
        follow = min(100, round(35 * extension + 25 * directional_body
                                + 20 * min(close_distance_atr / .25, 1)
                                + 20 * min(volume_ratio / 1.5, 1)))
        breach_bar += 1
    break_score = min(100, round(25 * closed_outside + 25 * min(close_distance_atr / .25, 1)
                                 + 20 * body_ratio + 15 * min(volume_ratio / 1.5, 1)
                                 + 15 * (follow / 100)))
    reclaim_distance = abs(close - level) / atr if reclaimed else 0.0
    reclaim_score = min(100, round(30 * reclaimed + 25 * min(reclaim_distance / .18, 1)
                                   + 20 * body_ratio + 15 * min(volume_ratio / 1.5, 1)
                                   + 10 * (breach_bar <= 3)))
    if not penetrated:
        state, break_type = "LEVEL_TEST", "TOUCH"
    elif (reclaimed and reclaim_score >= settings.reclaim_confirmation_threshold
          and breach_bar <= settings.break_follow_through_bars
          and (old_state in {"BREAK_CONFIRMATION_PENDING", "CLOSE_BREAK",
                             "NO_FOLLOW_THROUGH"} or penetration_atr >= .05)):
        state = "FAILED_BREAKDOWN" if direction == "DOWN" else "FAILED_BREAKOUT"
        break_type = "WICK_BREACH" if old_state == "LEVEL_TEST" else "CLOSE_BREACH"
    elif reclaimed:
        state, break_type = "LIQUIDITY_SWEEP_CANDIDATE", "WICK_BREACH"
    elif (closed_outside and follow >= settings.break_confirmation_threshold
          and break_score >= settings.break_confirmation_threshold):
        state, break_type = "BREAK_CONFIRMED", "CONFIRMED_BREAK"
    elif closed_outside:
        state = ("NO_FOLLOW_THROUGH" if same_level and old_state in {
            "BREAK_CONFIRMATION_PENDING", "CLOSE_BREAK"} else "BREAK_CONFIRMATION_PENDING")
        break_type = "CLOSE_BREACH"
    else:
        state, break_type = "LEVEL_BREACH", "WICK_BREACH"
    if (same_level and old_state in {"FAILED_BREAKDOWN", "FAILED_BREAKOUT"}
            and closed_outside and follow >= settings.break_confirmation_threshold):
        state = "RECLAIM_FAILED"
        break_score = min(100, break_score + 15)
    history = list(previous.get("recent_events") or [])
    if state != old_state and state in {"FAILED_BREAKDOWN", "FAILED_BREAKOUT",
                                        "LIQUIDITY_SWEEP_CANDIDATE", "RECLAIM_FAILED"}:
        history.append({"state": state, "candle": str(sample.index[-1])})
    history = history[-8:]
    whipsaw_count = sum(x.get("state") in {"FAILED_BREAKDOWN", "FAILED_BREAKOUT",
                                           "LIQUIDITY_SWEEP_CANDIDATE"} for x in history)
    regime = "WHIPSAW" if whipsaw_count >= settings.whipsaw_failed_break_count else "NORMAL"
    now = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    directional_state = {
        ("BREAK_CONFIRMED", "DOWN"): "BEAR_BREAKOUT_CONFIRMED",
        ("BREAK_CONFIRMED", "UP"): "BULL_BREAKOUT_CONFIRMED",
        ("FAILED_BREAKDOWN", "DOWN"): "BEAR_BREAKOUT_FAILED",
        ("FAILED_BREAKOUT", "UP"): "BULL_BREAKOUT_FAILED",
    }.get((state, direction), state)
    output = {
        "schemaVersion": "break-lifecycle-v1", "state": state,
        "directionalState": directional_state,
        "break_type": break_type, "direction": direction, "levelKind": kind,
        "level": round(level, 2),
        "break_confidence": break_score, "follow_through_score": follow,
        "follow_through": "SUFFICIENT" if follow >= 65 else "INSUFFICIENT",
        "reclaim_level": round(level, 2) if reclaimed else None,
        "reclaim_time": now if reclaimed else None,
        "reclaim_bars": breach_bar if reclaimed else None,
        "reclaim_strength": "STRONG" if reclaim_score >= 70 else "MEDIUM" if reclaim_score >= 45 else "WEAK",
        "reclaim_confidence": reclaim_score, "liquidity_sweep_score": (
            min(100, reclaim_score + round(penetration_atr * 20)) if reclaimed else 0),
        "bars_since_breach": breach_bar if closed_outside else 0,
        "market_regime": regime,
        "entry_confirmation_requirement": ("CLOSE_PLUS_RETEST_HOLD" if regime == "WHIPSAW"
                                             else "CLOSED_CANDLE"),
        "chase_breakout": regime != "WHIPSAW",
        "position_size_multiplier": settings.whipsaw_position_size_multiplier if regime == "WHIPSAW" else 1.0,
        "recent_events": history, "last_evaluated_candle": str(sample.index[-1]),
        "evaluated_at": now,
    }
    events = []
    if state != old_state or abs(level - old_level) > atr * .25:
        event_type = {
            "BREAK_CONFIRMATION_PENDING": "BREAK_PENDING",
            "FAILED_BREAKDOWN": "FAILED_BREAKDOWN",
            "FAILED_BREAKOUT": "FAILED_BREAKOUT",
            "BREAK_CONFIRMED": "BREAK_CONFIRMED",
            "RECLAIM_FAILED": "RECLAIM_FAILED",
            "LIQUIDITY_SWEEP_CANDIDATE": "LIQUIDITY_SWEEP_CANDIDATE",
        }.get(state, "LEVEL_BREACH")
        seed = f"XAUUSD|{event_type}|{level:.2f}|{sample.index[-1]}"
        events.append({"eventId": hashlib.sha256(seed.encode()).hexdigest()[:32],
                       "event_type": event_type, "previousState": old_state,
                       "currentState": state, "directionalState": directional_state,
                       "breakLifecycle": output,
                       "currentPrice": price, "triggerLevel": round(level, 2),
                       "candleCloseTime": str(sample.index[-1]), "calculatedAt": now,
                       "transitionReason": _message(state, level, kind)})
    return output, events


def _message(state: str, level: float, kind: str) -> str:
    down = format_level_cross(
        level_kind=kind, movement="DOWN", level=level,
        role="多方防守位" if kind == "support" else None)
    up = format_level_cross(
        level_kind=kind, movement="UP", level=level,
        role="空方防守位" if kind == "resistance" else None)
    return {
        "BREAK_CONFIRMATION_PENDING": (
            f"{down if kind == 'support' else up}，等待後續延續確認"),
        "FAILED_BREAKDOWN": f"下方跌破沒有延續，價格快速收復 {level:.2f}，疑似流動性掃盤",
        "FAILED_BREAKOUT": f"上方突破缺乏延續，價格快速跌回 {level:.2f}，警戒多頭陷阱",
        "BREAK_CONFIRMED": (
            f"{down if kind == 'support' else up}且後續延續，結構確認"),
        "RECLAIM_FAILED": f"收復 {level:.2f} 後再次有效失守，突破可信度提高",
        "LIQUIDITY_SWEEP_CANDIDATE": f"影線越過 {level:.2f} 後收回，疑似流動性掃盤",
    }.get(state, f"關鍵價 {level:.2f} 正在測試")
