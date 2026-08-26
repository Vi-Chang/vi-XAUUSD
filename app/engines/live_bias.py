"""Intrabar safety layer between confirmed structure and entry scoring.

Closed candles own structural direction and formal entry confirmation. Live
prices may only suspend an old direction or nominate the opposite direction
for confirmation; they can never promote an executable entry by themselves.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _side(bias: str) -> str:
    value = str(bias or "").upper()
    return "LONG" if "BULL" in value else "SHORT" if "BEAR" in value else "NONE"


def _zone(candidate: dict) -> tuple[float, float] | None:
    value = candidate.get("entry_zone") or candidate.get("entryZone") or {}
    if isinstance(value, (tuple, list)) and len(value) == 2:
        low, high = _number(value[0]), _number(value[1])
    elif isinstance(value, dict):
        low = _number(value.get("low") or value.get("lower"))
        high = _number(value.get("high") or value.get("upper"))
    else:
        return None
    return (min(low, high), max(low, high)) if low is not None and high is not None else None


def _dynamic_level(data: dict, candidates: list[dict], side: str) -> tuple[float | None, str]:
    normalized = data.get("normalized_analysis") or {}
    current = _number(normalized.get("currentPrice"))
    levels: list[tuple[float, str, float]] = []
    for candidate in candidates:
        if str(candidate.get("direction") or "").upper() != side:
            continue
        lifecycle = str(candidate.get("lifecycle_state") or candidate.get(
            "lifecycleState") or candidate.get("status") or "").upper()
        if lifecycle in {"EXPIRED", "INVALIDATED", "SUPERSEDED", "CANCELLED"}:
            continue
        level = _number(candidate.get("invalidation_price") or candidate.get(
            "invalidationPrice") or candidate.get("stopPrice"))
        if level is not None:
            zone = _zone(candidate)
            distance = (0.0 if zone and current is not None and zone[0] <= current <= zone[1]
                        else min(abs(current-zone[0]), abs(current-zone[1]))
                        if zone and current is not None else math.inf)
            levels.append((level, str(candidate.get("scenario_id") or candidate.get(
                "scenarioId") or candidate.get("setupId") or "runtime setup"), distance))
    if levels:
        # Bind the live safety line to the nearest currently relevant setup;
        # a distant future candidate must never move today's invalidation line.
        selected = min(levels, key=lambda item: (item[2], -item[0] if side == "LONG"
                                                 else item[0]))
        return selected[0], f"runtime setup {selected[1]}"
    health = data.get("decision_health_state") or {}
    defense = _number(health.get("defenseLevel"))
    defense_side = str(health.get("side") or "").upper()
    if defense is not None and defense_side in {"", side}:
        return defense, "canonical defense line"
    kind = "support" if side == "LONG" else "resistance"
    runtime_levels = [
        _number(item.get("price")) for item in normalized.get("confirmationLevels") or []
        if str(item.get("kind") or "").lower() == kind
    ]
    valid = [value for value in runtime_levels if value is not None]
    if valid:
        reference = current or valid[0]
        return min(valid, key=lambda value: abs(value-reference)), f"15M {kind}"
    return None, "runtime invalidation unavailable"


def resolve_direction_conflict(
    data: dict,
    *,
    structural_bias: str,
    candidates: list[dict],
    previous: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Return structural/live/execution direction without intrabar flip-flop."""
    settings = get_settings()
    previous = previous or {}
    normalized = data.get("normalized_analysis") or {}
    structural = str(structural_bias or "NEUTRAL").upper()
    structural_side = _side(structural)
    live_price = _number(normalized.get("currentPrice"))
    closed_price = _number(normalized.get("lastClosedCandlePrice"))
    closed_time = str(normalized.get("lastClosedCandleTimestamp") or "")
    evaluated_at = str(data.get("timestamp_utc") or normalized.get("generatedAt") or
                       datetime.now(timezone.utc).isoformat())
    atr = max(_number(normalized.get("atr15")) or .01, .01)
    level, level_source = _dynamic_level(data, candidates, structural_side)
    buffer = max(atr * settings.live_bias_invalidation_buffer_atr,
                 settings.live_bias_invalidation_min_abs)
    old_state = str(previous.get("liveBiasState") or "ALIGNED")
    old_execution = str(previous.get("executionBias") or structural_side or "NEUTRAL")
    old_count = int(previous.get("consecutiveBreachCount") or 0)
    old_closed_time = str(previous.get("lastClosed15m") or "")
    new_closed = bool(closed_time and closed_time != old_closed_time)
    origin = _number(previous.get("biasOriginPrice")) or closed_price or live_price
    displacement = ((live_price-origin) / atr if live_price is not None and origin is not None
                    else 0.0)
    adverse_displacement = (-displacement if structural_side == "LONG" else displacement
                            if structural_side == "SHORT" else abs(displacement))
    challenged = False
    beyond_buffer = False
    closed_invalidated = False
    closed_restored = False
    if live_price is not None and level is not None and structural_side == "LONG":
        challenged = live_price < level
        beyond_buffer = live_price < level-buffer
        closed_invalidated = bool(closed_price is not None and closed_price < level-buffer)
        closed_restored = bool(closed_price is not None and closed_price >= level)
    elif live_price is not None and level is not None and structural_side == "SHORT":
        challenged = live_price > level
        beyond_buffer = live_price > level+buffer
        closed_invalidated = bool(closed_price is not None and closed_price > level+buffer)
        closed_restored = bool(closed_price is not None and closed_price <= level)
    count = old_count + 1 if beyond_buffer else 0
    volume15 = (((data.get("volume_intelligence") or {}).get("timeframes") or {}).get(
        "15M") or {})
    volume_price_state = str(volume15.get("volumePriceState") or "UNAVAILABLE")
    volume_adverse = (
        structural_side == "LONG" and volume_price_state in {
            "VOLUME_CONFIRMED_DROP", "BEARISH_BREAKOUT_VOLUME"}
        or structural_side == "SHORT" and volume_price_state in {
            "VOLUME_CONFIRMED_RISE", "BULLISH_BREAKOUT_VOLUME"})
    strong_override = bool(
        beyond_buffer and (adverse_displacement >=
                           settings.live_bias_strong_displacement_atr or volume_adverse))
    persistent = bool(beyond_buffer and (
        count >= settings.live_bias_persistence_ticks or strong_override))
    data_health = str((data.get("decision_health_state") or {}).get(
        "dataHealth") or normalized.get("marketDataStatus") or "STALE").upper()
    if data_health in {"STALE", "FAILED", "DISCONNECTED"}:
        live_state, execution = "SUSPENDED", "NEUTRAL"
        reason = "核心行情資料過期，暫停使用舊方向建立新單"
    elif closed_invalidated and new_closed:
        live_state = "REVERSAL_CANDIDATE"
        execution = "SHORT_WATCH" if structural_side == "LONG" else "LONG_WATCH"
        reason = "最新已收15M確認原結構失效，等待相反方向完成獨立進場條件"
    elif persistent:
        live_state, execution = "INVALIDATING", "NEUTRAL"
        reason = "即時價格持續越過原方向失效線，先停止原方向新單"
    elif challenged:
        live_state, execution = "WEAKENING", f"{structural_side}_WATCH"
        reason = "即時價格正在測試原方向失效線，但尚未超過動態緩衝"
    elif (old_state in {"INVALIDATING", "WEAKENING"} and new_closed and
          closed_restored):
        live_state, execution = "ALIGNED", structural_side
        reason = f"15M 收盤重新守回原結構，{structural_side} 方向恢復"
    else:
        live_state = "ALIGNED"
        execution = structural_side if structural_side != "NONE" else "NEUTRAL"
        reason = "即時價格與已確認結構一致"
    evaluated_dt = _time(evaluated_at) or datetime.now(timezone.utc)
    closed_dt = _time(closed_time)
    bias_age_minutes = max(0.0, (evaluated_dt-closed_dt).total_seconds()/60) if closed_dt else None
    freshness = ("STALE" if live_state in {"INVALIDATING", "SUSPENDED"} or
                 (bias_age_minutes is not None and
                  bias_age_minutes >= settings.live_bias_stale_age_minutes and
                  adverse_displacement >= settings.live_bias_origin_max_displacement_atr)
                 else "CHALLENGED" if live_state == "WEAKENING" else "FRESH")
    allow_long = execution in {"LONG", "LONG_WATCH"} and live_state != "SUSPENDED"
    allow_short = execution in {"SHORT", "SHORT_WATCH"} and live_state != "SUSPENDED"
    version = int(previous.get("marketStateVersion") or 0) + int(
        live_state != old_state or execution != old_execution or new_closed)
    active_setup = next((candidate for candidate in candidates
                         if str(candidate.get("direction") or "").upper() == structural_side), {})
    setup_state = ("INVALIDATED" if closed_invalidated and new_closed else
                   "INVALIDATING" if persistent else
                   str(active_setup.get("lifecycle_state") or active_setup.get(
                       "status") or "ACTIVE"))
    if beyond_buffer or closed_invalidated:
        # Once price has crossed the side-aware invalidation boundary, momentum
        # must describe that adverse move.  The origin-price displacement can
        # still point in the old direction after a newly anchored setup and is
        # therefore not authoritative for this user-facing state.
        live_momentum = "STRONG_SHORT" if structural_side == "LONG" else "STRONG_LONG"
    elif displacement >= settings.live_bias_strong_displacement_atr:
        live_momentum = "STRONG_LONG"
    elif displacement <= -settings.live_bias_strong_displacement_atr:
        live_momentum = "STRONG_SHORT"
    else:
        live_momentum = "NEUTRAL"
    snapshot = {
        "schemaVersion": "live-bias-v1", "structuralBias": structural,
        "structuralSide": structural_side, "liveMomentum": live_momentum,
        "liveBiasState": live_state, "executionBias": execution,
        "lastClosed15m": closed_time, "lastClosed15mPrice": closed_price,
        "currentPrice": live_price, "biasOriginPrice": origin,
        "invalidationLevel": level, "invalidationLevelSource": level_source,
        "invalidationBuffer": round(buffer, 5),
        "priceDisplacementAtr": round(displacement, 4),
        "adverseDisplacementAtr": round(adverse_displacement, 4),
        "consecutiveBreachCount": count, "strongMomentumOverride": strong_override,
        "volumeSafetyOverride": bool(beyond_buffer and volume_adverse),
        "volumePriceState": volume_price_state,
        "intrabarSafetyOverride": persistent, "biasAgeMinutes": bias_age_minutes,
        "biasFreshness": freshness, "allowLong": allow_long,
        "allowShort": allow_short, "activeSetup": active_setup.get(
            "scenario_id") or active_setup.get("scenarioId"),
        "setupState": setup_state, "reason": reason,
        "evaluationTimestamp": evaluated_at, "marketStateVersion": version,
        "setupVersion": active_setup.get("scenario_version") or active_setup.get(
            "scenarioVersion") or 1,
    }
    events: list[dict] = []
    if live_state != old_state or execution != old_execution:
        if live_state == "INVALIDATING":
            event_type = f"{structural_side}_INVALIDATING"
        elif live_state == "REVERSAL_CANDIDATE":
            event_type = execution
        elif live_state == "ALIGNED" and old_state in {"INVALIDATING", "WEAKENING"}:
            event_type = f"{structural_side}_RESTORED"
        else:
            event_type = "LIVE_BIAS_CHANGED"
        seed = (f"{data.get('symbol', 'XAUUSD')}|{event_type}|{structural_side}|"
                f"{level}|{version}")
        events.append({
            "eventId": hashlib.sha256(seed.encode()).hexdigest()[:32],
            "event_type": event_type, "previousState": old_state,
            "currentState": live_state, "structuralBias": structural,
            "liveBiasState": live_state, "executionBias": execution,
            "currentPrice": live_price, "invalidationLevel": level,
            "invalidationBuffer": buffer, "transitionReason": reason,
            "candleCloseTime": closed_time,
            "notificationEligible": event_type != "LIVE_BIAS_CHANGED",
        })
    return snapshot, events


def evaluate_live_bias(
    data: dict,
    *,
    structural_bias: str,
    candidates: list[dict],
    previous: dict | None = None,
) -> tuple[dict, list[dict]]:
    return resolve_direction_conflict(
        data, structural_bias=structural_bias, candidates=candidates, previous=previous)
