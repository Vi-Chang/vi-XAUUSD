"""Tiered entry-opportunity gate for the existing canonical state machine.

Hard blocks are execution safety invariants. Soft filters are score/size
adjustments and can never independently turn a valid setup into BLOCKED.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

HARD_BLOCK_CODES = {
    "DATA_STALE", "DATA_INVALID", "MARKET_CLOSED", "EVENT_BLACKOUT",
    "SPREAD_ABNORMAL", "STOP_INVALID", "RR_BELOW_ABSOLUTE_MINIMUM",
    "PRICE_DATA_DESYNC", "CANDLE_NOT_CLOSED_FOR_CONFIRMED_ENTRY",
    "CHASE_ZONE", "SCENARIO_INVALIDATED", "POSITION_ACTIVE",
    "LIVE_BIAS_INVALIDATING",
}

SOFT_FILTER_CODES = {
    "MARKET_BIAS_NOT_PERFECT", "COUNTER_4H_STRUCTURE", "COUNTER_1D_TREND",
    "RETEST_PARTIAL", "MOMENTUM_MEDIUM", "ENTRY_ZONE_EDGE",
    "DEFENSE_CONFIRMATION_PARTIAL", "MINOR_STRUCTURE_CONFLICT",
    "DISTANCE_FROM_IDEAL_ENTRY", "DATA_DEGRADED_SAFE",
}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _bias_side(value: Any) -> str:
    text = str(value or "").upper()
    return "LONG" if "BULL" in text else "SHORT" if "BEAR" in text else "NONE"


def _zone(candidate: dict) -> tuple[float, float] | None:
    raw = candidate.get("entry_zone") or candidate.get("entryZone") or {}
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        low, high = _number(raw[0]), _number(raw[1])
    else:
        low = _number(raw.get("low") if isinstance(raw, dict) else None)
        high = _number(raw.get("high") if isinstance(raw, dict) else None)
    return (min(low, high), max(low, high)) if low is not None and high is not None else None


def _distance_state(candidate: dict, *, current: float, atr: float) -> tuple[str, float | None]:
    settings = get_settings()
    zone = _zone(candidate)
    if not zone:
        return "NO_ZONE", None
    low, high = zone
    if low <= current <= high:
        width = max(high-low, .01)
        edge_distance = min(current-low, high-current)
        return ("ACCEPTABLE_ENTRY_DISTANCE" if edge_distance <= width * .15
                else "GOOD_ENTRY_DISTANCE"), 0.0
    direction = str(candidate.get("direction") or "NEUTRAL").upper()
    chase = _number(candidate.get("chase_limit") or candidate.get("chaseLimit"))
    if ((direction == "LONG" and current > (chase if chase is not None else high)) or
            (direction == "SHORT" and current < (chase if chase is not None else low))):
        return "CHASE_ZONE", min(abs(current-low), abs(current-high))
    distance = min(abs(current-low), abs(current-high))
    if distance <= max(atr, .01) * settings.entry_gate_acceptable_distance_atr:
        return "DISTANT_ENTRY", distance
    return "DISTANT_ENTRY", distance


def _candidate_gate(candidate: dict, context: dict) -> dict:
    settings = get_settings()
    direction = str(candidate.get("direction") or "NEUTRAL").upper()
    current = float(context.get("currentPrice") or 0)
    atr = max(float(context.get("atr15") or 0), .01)
    rr = _number(candidate.get("risk_reward") or candidate.get("riskReward") or
                 candidate.get("estimated_risk_reward") or candidate.get("estimatedRR"))
    lifecycle = str(candidate.get("lifecycle_state") or candidate.get("lifecycleState") or
                    candidate.get("status") or "SETUP").upper()
    base_score = max(0, min(100, int(candidate.get("strength") or
                                     candidate.get("signalScore") or 50)))
    # The base model is important, but may not saturate the final score by
    # itself.  Keeping headroom makes timeframe and execution penalties
    # observable even for a strong setup.
    score = round(base_score * .40)
    hard_blocks: list[str] = []
    soft_filters: list[dict] = []
    components: dict[str, int] = {"baseSetup": score}

    def soft(code: str, penalty: int, reason: str) -> None:
        nonlocal score
        score -= penalty
        soft_filters.append({"code": code, "penalty": penalty, "reason": reason})

    health = str(context.get("dataHealth") or "STALE").upper()
    if health in {"STALE", "FAILED", "DISCONNECTED"}:
        hard_blocks.append("DATA_STALE")
    elif health not in {"HEALTHY", "GOOD"}:
        soft("DATA_DEGRADED_SAFE", settings.entry_gate_degraded_data_penalty,
             "核心15M／1H仍可用，但部分資料不完整")
    if context.get("priceDataDesync"):
        hard_blocks.append("PRICE_DATA_DESYNC")
    if context.get("marketClosed"):
        hard_blocks.append("MARKET_CLOSED")
    if context.get("eventBlackout"):
        hard_blocks.append("EVENT_BLACKOUT")
    if context.get("spreadAbnormal"):
        hard_blocks.append("SPREAD_ABNORMAL")
    if context.get("positionActive"):
        hard_blocks.append("POSITION_ACTIVE")
    live_state = str(context.get("liveBiasState") or "ALIGNED").upper()
    structural_side = str(context.get("structuralSide") or "NONE").upper()
    if (direction == structural_side and
            live_state in {"INVALIDATING", "REVERSAL_CANDIDATE", "SUSPENDED"}):
        hard_blocks.append("LIVE_BIAS_INVALIDATING")
    if lifecycle in {"EXPIRED", "INVALIDATED", "CANCELLED", "SUPERSEDED"}:
        hard_blocks.append("SCENARIO_INVALIDATED")
    expires_at = candidate.get("expires_at") or candidate.get("expiresAt")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
            evaluated = datetime.fromisoformat(str(
                context.get("evaluatedAt") or datetime.now(timezone.utc).isoformat()
            ).replace("Z", "+00:00"))
            evaluated = (evaluated if evaluated.tzinfo else
                         evaluated.replace(tzinfo=timezone.utc))
            if evaluated >= expiry:
                hard_blocks.append("SCENARIO_INVALIDATED")
        except (TypeError, ValueError):
            hard_blocks.append("SCENARIO_INVALIDATED")
    stop = _number(candidate.get("invalidation_price") or candidate.get("invalidationPrice") or
                   candidate.get("stopPrice"))
    stop_valid = bool(stop is not None and (
        (direction == "LONG" and stop < current) or
        (direction == "SHORT" and stop > current)))
    if not stop_valid:
        hard_blocks.append("STOP_INVALID")
    if rr is None or rr < settings.entry_gate_absolute_min_rr:
        hard_blocks.append("RR_BELOW_ABSOLUTE_MINIMUM")
    elif rr >= settings.decision_assistant_min_rr:
        score += 12
        components["riskReward"] = 12
    else:
        score += 4
        components["riskReward"] = 4
        soft("RETEST_PARTIAL", 2, "RR 可接受但未達理想門檻，僅適合小倉")

    distance_state, distance = _distance_state(candidate, current=current, atr=atr)
    if distance_state == "CHASE_ZONE":
        hard_blocks.append("CHASE_ZONE")
    elif distance_state == "GOOD_ENTRY_DISTANCE":
        score += 10
        components["entryDistance"] = 10
    elif distance_state == "ACCEPTABLE_ENTRY_DISTANCE":
        score += 4
        components["entryDistance"] = 4
        soft("ENTRY_ZONE_EDGE", 2, "價格位於可接受邊緣，降低部位")
    else:
        soft("DISTANCE_FROM_IDEAL_ENTRY", 8, "價格尚未到可執行區")

    closed_available = bool(context.get("closedCandleAvailable"))
    ready_lifecycle = lifecycle == "ENTRY_READY" or lifecycle.startswith("ENTRY_READY_")
    confirmed_lifecycle = ready_lifecycle or lifecycle in {"CONFIRMED", "TRIGGERED"}
    if ready_lifecycle and not closed_available:
        hard_blocks.append("CANDLE_NOT_CLOSED_FOR_CONFIRMED_ENTRY")
    elif ready_lifecycle:
        score += 16
        components["closedConfirmation"] = 16
    elif confirmed_lifecycle:
        score += 10
        components["closedConfirmation"] = 10
        soft("RETEST_PARTIAL", settings.entry_gate_partial_confirmation_penalty,
             "主要結構已成立，但次要進場確認尚未完整")
    else:
        soft("RETEST_PARTIAL", settings.entry_gate_partial_confirmation_penalty,
             "交易機會仍在形成")

    bias15 = _bias_side(context.get("bias15m"))
    bias1h = _bias_side(context.get("bias1h"))
    if bias15 == direction:
        score += 8
        components["structure15m"] = 8
    elif bias15 not in {"NONE", direction}:
        soft("MINOR_STRUCTURE_CONFLICT", 12, "15M 結構與候選方向相反")
    if bias1h == direction:
        score += 6
        components["direction1h"] = 6
    elif bias1h not in {"NONE", direction}:
        soft("MINOR_STRUCTURE_CONFLICT", 7, "1H 方向尚未一致")
    if _bias_side(context.get("bias4h")) not in {"NONE", direction}:
        soft("COUNTER_4H_STRUCTURE", settings.entry_gate_counter_4h_penalty,
             "與4H背景相反，採短打")
    if _bias_side(context.get("bias1d")) not in {"NONE", direction}:
        soft("COUNTER_1D_TREND", settings.entry_gate_counter_1d_penalty,
             "與日線背景相反，降低目標與倉位")
    if _bias_side(context.get("marketBias")) not in {"NONE", direction}:
        soft("MARKET_BIAS_NOT_PERFECT", 4, "Canonical 背景方向未完全一致")

    setup_type = str(candidate.get("setup_type") or candidate.get("setupType") or
                     candidate.get("type") or "OTHER").upper()
    if any(name in setup_type for name in (
            "BREAKOUT", "CONTINUATION", "REJECTION", "FALSE_BREAK",
            "LIQUIDITY_SWEEP")):
        score += 6
        components["recognizedSetup"] = 6
    if str(context.get("momentum") or "").upper() in {"STRONG", "ACCELERATING"}:
        score += 4
        components["momentum"] = 4
    elif str(context.get("momentum") or "").upper() in {"MEDIUM", "STABLE", "WEAKENING"}:
        soft("MOMENTUM_MEDIUM", 3, "動能不是最佳，但不直接否決")
    if str(context.get("defenseState") or "") in {"TESTING", "BROKEN_PENDING_CLOSE"}:
        soft("DEFENSE_CONFIRMATION_PARTIAL", 8, "防守仍在收盤確認階段")
    behavior = str(context.get("marketBehavior") or "").upper()
    if ((direction == "LONG" and behavior in {
            "SLOW_BEARISH_DRIFT", "BEARISH_IMPULSE", "BEARISH_BREAKDOWN"}) or
            (direction == "SHORT" and behavior in {
                "SLOW_BULLISH_DRIFT", "BULLISH_IMPULSE", "BULLISH_BREAKOUT"})):
        soft("MINOR_STRUCTURE_CONFLICT", 30,
             "即時價格行為與候選方向相反，等待短線重新確認")
    if str(context.get("scenarioValidity") or "ACTIVE") in {"INVALIDATED", "STALE"}:
        hard_blocks.append("SCENARIO_INVALIDATED")

    # Relative volume is a bounded confirmation/quality factor.  It may improve
    # or reduce a score, but never creates a direction or an execution veto.
    volume = context.get("volumeIntelligence") or {}
    volume_scores = volume.get("volumeScore") or {}
    volume_impact = int(volume_scores.get(direction) or 0)
    volume_impact = max(-20, min(20, volume_impact))
    if volume_impact:
        score += volume_impact
        components["relativeVolume"] = volume_impact

    score = max(0, min(100, score))
    hard_blocks = list(dict.fromkeys(hard_blocks))
    if hard_blocks:
        state = "BLOCKED"
    elif (score >= settings.entry_gate_full_score and ready_lifecycle and
          distance_state == "GOOD_ENTRY_DISTANCE"):
        state = "ENTRY_READY"
    elif (score >= settings.entry_gate_probe_score and confirmed_lifecycle and
          distance_state in {"GOOD_ENTRY_DISTANCE", "ACCEPTABLE_ENTRY_DISTANCE"}):
        state = "PROBE_READY"
    else:
        state = "WATCH"
    missing = []
    if not confirmed_lifecycle:
        missing.append("等待15M收盤完成主要確認")
    if distance_state not in {"GOOD_ENTRY_DISTANCE", "ACCEPTABLE_ENTRY_DISTANCE"}:
        missing.append("等待價格進入可執行區")
    if rr is None:
        missing.append("等待完整停損與目標以計算RR")
    return {
        "scenarioId": str(candidate.get("scenario_id") or candidate.get("scenarioId") or ""),
        "direction": direction, "setupType": setup_type, "baseScore": base_score,
        "entryConfidenceScore": score, "state": state,
        "positionSizeMultiplier": (1.0 if state == "ENTRY_READY" else
                                   settings.entry_gate_probe_size_multiplier
                                   if state == "PROBE_READY" else 0.0),
        "hardBlocks": hard_blocks, "softFilters": soft_filters,
        "scoreComponents": components, "riskReward": rr,
        "entryDistanceState": distance_state, "entryDistance": distance,
        "missingConditions": missing,
        "structuralBias": context.get("structuralBias"),
        "liveBiasState": live_state, "executionBias": context.get("executionBias"),
        "volumeScoreImpact": volume_impact,
        "volumePriceState": volume.get("primaryState", "UNAVAILABLE"),
    }


def evaluate_entry_opportunity_gate(candidates: list[dict], *, context: dict) -> dict:
    """Score LONG and SHORT independently, then choose one canonical tier."""
    evaluations = [_candidate_gate(candidate, context) for candidate in candidates
                   if str(candidate.get("direction") or "") in {"LONG", "SHORT"}]
    def rank(row: dict) -> tuple[int, bool, int, int]:
        hard = list(row.get("hardBlocks") or [])
        return (
            {"ENTRY_READY": 3, "PROBE_READY": 2, "WATCH": 1, "BLOCKED": 0}[
                row["state"]],
            "SCENARIO_INVALIDATED" not in hard,
            -len(hard),
            row["entryConfidenceScore"],
        )
    by_side = {}
    for side in ("LONG", "SHORT"):
        side_rows = [row for row in evaluations if row["direction"] == side]
        by_side[side] = max(side_rows, key=rank, default=None)
    selected = max(evaluations, key=rank, default=None)
    return {
        "schemaVersion": "entry-opportunity-gate-v1",
        "entryState": selected["state"] if selected else "WATCH",
        "selected": selected, "long": by_side["LONG"], "short": by_side["SHORT"],
        "longScore": (by_side["LONG"] or {}).get("entryConfidenceScore", 0),
        "shortScore": (by_side["SHORT"] or {}).get("entryConfidenceScore", 0),
        "candidateEvaluations": evaluations,
    }
