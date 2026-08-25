"""Closed loop after a weak break is quickly reclaimed.

This processor never grants entry permission.  It cancels the failed break
direction, builds an immutable opposite-side confirmation plan, and waits for
a later closed candle.  The existing canonical decision engine remains the
only component allowed to publish ENTRY_READY.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.engines.directional_wording import invalidation_wording
from app.engines.scalp_entry_trigger import build_scalp_trigger_layers

ACTIVE_STATES = {"WAIT_CONFIRMATION", "LONG_SETUP_CONFIRMED", "SHORT_SETUP_CONFIRMED"}


def _number(value, default: float | None = None) -> float | None:
    return float(value) if isinstance(value, (int, float)) else default


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _build_next_action(direction: str, level: float, normalized: dict) -> dict:
    layers = build_scalp_trigger_layers(
        direction=direction, source_level=level, normalized=normalized,
        created_at=str(normalized.get("lastClosedCandleTimestamp") or ""))
    scalp = layers["primaryScalpTrigger"]
    structural = layers["structuralConfirmationTrigger"]
    zone = layers["candidateEntryZone"]
    invalidation = float(layers["invalidationLevel"])
    return {
        "action": "WAIT_CONFIRMATION", "direction": direction,
        "triggerLevel": scalp["level"],
        "strongConfirmationLevel": structural["level"],
        "strongConfirmationTimeframe": structural["timeframe"],
        "confirmationType": "CLOSED_CANDLE", "confirmationTimeframe": "15M",
        "invalidationLevel": invalidation,
        "invalidationText": invalidation_wording(direction, invalidation),
        "targets": layers["targets"],
        "entryZoneLow": zone["low"], "entryZoneHigh": zone["high"],
        "estimatedRR": layers["estimatedRR"],
        "primaryScalpTrigger": scalp,
        "structuralConfirmationTrigger": structural,
        "earlyReversal": layers["earlyReversal"],
        "triggerCreatedAt": layers["triggerCreatedAt"],
        "triggerSourceStructureId": layers["triggerSourceStructureId"],
        "triggerTTL": layers["triggerTTL"],
        "triggerExpiresAt": layers["triggerExpiresAt"],
        "triggerRevalidation": layers["triggerRevalidation"],
    }


def evaluate_fake_breakout_recovery(
    *, data: dict, break_state: dict, previous: dict | None = None,
) -> tuple[dict, list[dict]]:
    previous = previous or {}
    settings = get_settings()
    normalized = data.get("normalized_analysis") or {}
    state = str(break_state.get("state") or "")
    quality = int(break_state.get("break_confidence") or 0)
    continuation = str(break_state.get("follow_through") or "INSUFFICIENT")
    level = _number(break_state.get("level"))
    candle = str(break_state.get("last_evaluated_candle") or
                 normalized.get("lastClosedCandleTimestamp") or "")
    close = _number(normalized.get("lastClosedCandlePrice"))
    now = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    failed = (state in {"FAILED_BREAKDOWN", "FAILED_BREAKOUT"}
              and continuation == "INSUFFICIENT"
              and quality < settings.failed_breakout_quality_threshold
              and level is not None)

    previous_state = str(previous.get("state") or "IDLE")
    previous_action = dict(previous.get("nextAction") or {})
    if previous_state in ACTIVE_STATES and previous_action:
        expires_at = _parse(str(previous.get("expiresAt") or ""))
        current_time = _parse(candle) or _parse(now)
        direction = str(previous.get("oppositeDirection") or "")
        invalidation = _number(previous_action.get("invalidationLevel"))
        trigger = _number(previous_action.get("triggerLevel"))
        source_candle = str(previous.get("sourceCandleTime") or "")
        trigger_source_candle = str(previous_action.get("triggerCreatedAt") or source_candle)
        last_evaluated = str(previous.get("lastTriggerEvaluationCandle") or source_candle)
        if candle and candle != last_evaluated:
            proposed_action = _build_next_action(
                direction, float(previous.get("sourceLevel") or level or 0), normalized)
            if (proposed_action.get("triggerSourceStructureId") !=
                    previous_action.get("triggerSourceStructureId")):
                previous_action = proposed_action
            else:
                previous_action["triggerRevalidatedAt"] = candle
            invalidation = _number(previous_action.get("invalidationLevel"))
            trigger = _number(previous_action.get("triggerLevel"))
            trigger_source_candle = str(
                previous_action.get("triggerCreatedAt") or trigger_source_candle)
        invalidated = bool(close is not None and invalidation is not None and (
            close < invalidation if direction == "LONG" else close > invalidation))
        confirmed = bool(close is not None and trigger is not None
                         and candle and candle != trigger_source_candle and (
                             close > trigger if direction == "LONG" else close < trigger))
        if invalidated or state == "RECLAIM_FAILED":
            output = {**previous, "state": "INVALIDATED", "active": False,
                      "updatedAt": now, "stateReason": "反向確認計畫已失效"}
            return output, [_event(output, "RECOVERY_SETUP_INVALIDATED", candle, close)]
        if expires_at and current_time and current_time > expires_at:
            return ({**previous, "state": "EXPIRED", "active": False,
                     "updatedAt": now, "stateReason": "反向確認計畫已到期"}, [])
        if confirmed and "SETUP_CONFIRMED" not in previous_state:
            zone_low = _number(previous_action.get("entryZoneLow"))
            zone_high = _number(previous_action.get("entryZoneHigh"))
            stop = _number(previous_action.get("invalidationLevel"))
            targets = list(previous_action.get("targets") or [])
            target = _number(targets[0]) if targets else None
            in_zone = bool(close is not None and zone_low is not None and zone_high is not None
                           and zone_low <= close <= zone_high)
            risk = ((close - stop) if direction == "LONG" else (stop - close)) \
                if close is not None and stop is not None else 0.0
            reward = ((target - close) if direction == "LONG" else (close - target)) \
                if close is not None and target is not None else 0.0
            executable_rr = reward / risk if risk > 0 and reward > 0 else 0.0
            data_fresh = str(normalized.get("marketDataStatus") or "GOOD").upper() in {
                "GOOD", "HEALTHY"}
            scalp_ready = bool(in_zone and executable_rr >= settings.decision_assistant_min_rr
                               and data_fresh)
            confirmed_state = f"{direction}_SETUP_CONFIRMED"
            output = {**previous, "state": confirmed_state, "active": True,
                      "nextAction": previous_action,
                      "primaryScalpTrigger": previous_action.get("primaryScalpTrigger"),
                      "structuralConfirmationTrigger": previous_action.get(
                          "structuralConfirmationTrigger"),
                      "confirmedAt": now, "confirmedCandleTime": candle,
                      "lastTriggerEvaluationCandle": candle,
                      "scalpEntryReady": scalp_ready,
                      "executableRR": round(executable_rr, 3),
                      "updatedAt": now,
                      "stateReason": ("最近短線結構已由15M收盤確認，完整風控已通過"
                                      if scalp_ready else
                                      "最近短線結構已確認，但位置或風險報酬比尚未通過")}
            return output, [_event(output, "OPPOSITE_SETUP_CONFIRMED", candle, close)]
        # A new failed-break fingerprint supersedes the old recovery plan.
        if not failed:
            return {**previous, "nextAction": previous_action,
                    "primaryScalpTrigger": previous_action.get("primaryScalpTrigger"),
                    "structuralConfirmationTrigger": previous_action.get(
                        "structuralConfirmationTrigger"),
                    "lastTriggerEvaluationCandle": candle or last_evaluated,
                    "updatedAt": now}, []

    if not failed:
        return ({"schemaVersion": "fake-breakout-recovery-v1", "state": "IDLE",
                 "active": False, "updatedAt": now}, [])
    if not isinstance(level, (int, float)):
        return ({"schemaVersion": "fake-breakout-recovery-v1", "state": "IDLE",
                 "active": False, "updatedAt": now,
                 "stateReason": "缺少可驗證的失敗突破價位"}, [])

    direction = "LONG" if state == "FAILED_BREAKDOWN" else "SHORT"
    invalidated_direction = "SHORT" if direction == "LONG" else "LONG"
    failure_state = "BEAR_BREAKOUT_FAILED" if direction == "LONG" else "BULL_BREAKOUT_FAILED"
    next_action = _build_next_action(direction, float(level), normalized)
    reclaim_score = int(break_state.get("reclaim_confidence") or 0)
    failure_score = min(100, round(
        (settings.failed_breakout_quality_threshold - quality)
        / max(settings.failed_breakout_quality_threshold, 1) * 50
        + reclaim_score * 0.50))
    boost = min(settings.failed_breakout_opposite_boost_max,
                max(settings.failed_breakout_opposite_boost_min, failure_score // 4))
    source_id = hashlib.sha256(
        f"XAUUSD|{failure_state}|{float(level):.2f}|{candle}".encode()).hexdigest()[:24]
    source_time = _parse(candle) or _parse(now) or datetime.now(timezone.utc)
    output = {
        "schemaVersion": "fake-breakout-recovery-v1", "state": "WAIT_CONFIRMATION",
        "active": True, "sourceFailureId": source_id, "sourceCandleTime": candle,
        "sourceLevel": round(float(level), 2), "breakoutFailureState": failure_state,
        "liquiditySweepState": "LIQUIDITY_SWEEP_SUSPECTED",
        "invalidatedBreakoutDirection": invalidated_direction,
        "oppositeDirection": direction,
        "oppositeSetupAction": f"RECALCULATE_{direction}_SETUP",
        "earlyReversalLong": direction == "LONG",
        "earlyReversalShort": direction == "SHORT",
        "rescanBothSides": True,
        "rescanReason": "FAILED_BREAK_RECLAIM",
        "failedBreakoutScore": failure_score, "oppositeBiasBoost": boost,
        "breakoutQuality": quality, "continuation": continuation,
        "fastRecovery": True, "nextAction": next_action,
        "primaryScalpTrigger": next_action["primaryScalpTrigger"],
        "structuralConfirmationTrigger": next_action["structuralConfirmationTrigger"],
        "lastTriggerEvaluationCandle": candle,
        "createdAt": now, "updatedAt": now,
        "expiresAt": (source_time + timedelta(
            minutes=15 * settings.fake_breakout_recovery_expiry_bars)).isoformat(),
        "stateReason": "跌破後缺乏延續且快速收復，取消原突破方向並等待反向確認",
    }
    return output, [_event(output, "FAKE_BREAKOUT_CONFIRMED", candle, close)]


def _event(state: dict, event_type: str, candle: str, price: float | None) -> dict:
    next_action = state.get("nextAction") or {}
    seed = (f"XAUUSD|{event_type}|{state.get('oppositeDirection')}|"
            f"{state.get('sourceLevel')}|{candle}")
    return {
        "eventId": hashlib.sha256(seed.encode()).hexdigest()[:32],
        "event_type": event_type, "previousState": "FAST_RECOVERY",
        "currentState": str(state.get("state") or "WAIT_CONFIRMATION"),
        "direction": str(state.get("oppositeDirection") or "NONE"),
        "currentPrice": price, "triggerLevel": next_action.get("triggerLevel"),
        "stopLoss": next_action.get("invalidationLevel"),
        "targets": next_action.get("targets") or [], "candleCloseTime": candle,
        "transitionReason": str(state.get("stateReason") or ""),
        "fakeBreakoutRecovery": state,
    }
