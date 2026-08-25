"""Independent pre-entry lifecycle; formal entry stays canonical."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

ACTIVE = {"PREPARE_LONG", "PREPARE_SHORT"}
TERMINAL = {"LONG_READY", "SHORT_READY", "MISSED_LONG", "MISSED_SHORT", "INVALIDATED"}


def _num(value: Any, default: float | None = None) -> float | None:
    return float(value) if isinstance(value, (int, float)) else default


def _zone(item: dict) -> tuple[float, float] | None:
    raw = item.get("entry_zone") or item.get("entryZone") or {}
    low = _num(raw.get("lower") if raw.get("lower") is not None else raw.get("low"))
    high = _num(raw.get("upper") if raw.get("upper") is not None else raw.get("high"))
    return tuple(sorted((low, high))) if low is not None and high is not None else None


def _bias(data: dict) -> str:
    health, normalized = data.get("decision_health_state") or {}, data.get("normalized_analysis") or {}
    value = str(health.get("marketBias") or normalized.get("trendBias") or "NEUTRAL").upper()
    return {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(value, value)


def _data_health(data: dict) -> str:
    health, normalized = data.get("decision_health_state") or {}, data.get("normalized_analysis") or {}
    return str(health.get("dataHealth") or normalized.get("marketDataStatus") or "STALE").upper()


def _candidate(data: dict, side: str) -> dict | None:
    engine = data.get("entry_opportunity_engine") or {}
    opportunities = [item for item in engine.get("opportunities") or []
                     if str(item.get("side") or "").upper() == side
                     and str(item.get("state") or "") not in {"EXPIRED", "INVALIDATED", "REJECTED"}
                     and _zone(item)]
    primary_id = str(engine.get("primaryOpportunityId") or "")
    opportunities.sort(key=lambda item: (
        str(item.get("opportunity_id") or "") != primary_id,
        not bool(item.get("primary_eligible", True)),
        float(item.get("distance_from_current") or item.get("anchor_distance") or 0),
        -int(item.get("opportunity_score") or 0)))
    return opportunities[0] if opportunities else None


def _evidence(data: dict, side: str, price: float, atr: float,
              opportunity: dict) -> tuple[list[str], list[str]]:
    break_state = data.get("break_lifecycle_engine") or {}
    wick = data.get("wick_rejection_engine") or {}
    behavior = data.get("market_behavior_engine") or {}
    reasons, rejected = [], []
    expected_failed = "FAILED_BREAKDOWN" if side == "LONG" else "FAILED_BREAKOUT"
    expected_wick = "LOWER" if side == "LONG" else "UPPER"
    failed = str(break_state.get("state") or "") == expected_failed
    wick_state = str(wick.get("wick_rejection_state") or "")
    wick_matches = expected_wick in wick_state and str(
        wick.get("wick_rejection_strength") or "NONE") in {"MEDIUM", "STRONG"}
    if failed:
        reasons.extend([expected_failed, "SWEEP_RECLAIM"])
    if wick_matches:
        reasons.append("SUPPORT_REJECTION" if side == "LONG" else "RESISTANCE_REJECTION")
    behavior_name = str(behavior.get("marketBehavior") or behavior.get("behavior")
                        or behavior.get("state") or "").upper()
    micro = ((side == "LONG" and any(x in behavior_name for x in ("REBOUND", "RECOVER", "HIGHER_LOW")))
             or (side == "SHORT" and any(x in behavior_name for x in ("WEAK", "LOWER_HIGH", "REJECT"))))
    if micro:
        reasons.append("MICRO_HIGHER_LOW" if side == "LONG" else "MICRO_LOWER_HIGH")
    trigger = _num(opportunity.get("trigger_level"))
    compression = (str(opportunity.get("type") or opportunity.get("entry_type") or "") == "BREAKOUT_RETEST"
                   and trigger is not None
                   and abs(price - trigger) <= atr * get_settings().early_entry_breakout_near_atr_mult
                   and not wick_matches)
    if compression:
        reasons.append("BREAKOUT_COMPRESSION")
    if not (failed or wick_matches or micro or compression):
        rejected.append("NO_REACTION_CONFIRMATION")
    if not (failed or micro or compression or bool(opportunity.get("confirmation_evidence"))):
        rejected.append("NO_STRUCTURE_CONFIRMATION")
    return list(dict.fromkeys(reasons)), rejected


def is_chasing_entry(*, side: str, current_price: float, zone: tuple[float, float],
                     stop: float | None, target: float | None, atr: float,
                     minimum_rr: float) -> tuple[bool, str, float | None]:
    """Long only chases above a zone; short only chases below it."""
    escaped = current_price > zone[1] if side == "LONG" else current_price < zone[0]
    if not escaped:
        return False, "WITHIN_EXECUTABLE_SIDE", None
    extension = current_price - zone[1] if side == "LONG" else zone[0] - current_price
    rr = None
    if stop is not None and target is not None:
        risk = current_price - stop if side == "LONG" else stop - current_price
        reward = target - current_price if side == "LONG" else current_price - target
        rr = reward / risk if risk > 0 and reward > 0 else 0.0
    if extension > atr * get_settings().early_entry_max_extension_atr_mult:
        return True, "PRICE_TOO_EXTENDED", round(rr, 3) if rr is not None else None
    if rr is not None and rr < minimum_rr:
        return True, "RR_TOO_LOW", round(rr, 3)
    return False, "EXTENSION_STILL_ACCEPTABLE", round(rr, 3) if rr is not None else None


def _setup_id(symbol: str, side: str, opportunity: dict, zone: tuple[float, float]) -> str:
    anchor = str(opportunity.get("opportunity_id") or opportunity.get("setup_id")
                 or f"{zone[0]:.2f}:{zone[1]:.2f}")
    return "EARLY-" + hashlib.sha256(f"{symbol}|{side}|{anchor}".encode()).hexdigest()[:16]


def _event(state: dict, stage: str, previous_state: str) -> dict:
    setup_id = str(state["setup_id"])
    event_type = {"PREPARE": "EARLY_ENTRY_PREPARE", "MISSED": "EARLY_ENTRY_MISSED",
                  "INVALIDATED": "EARLY_ENTRY_INVALIDATED"}[stage]
    key = f"{setup_id}:{stage}"
    return {"eventId": hashlib.sha256(key.encode()).hexdigest()[:32], "eventKey": key,
            "event_type": event_type, "setupId": setup_id, "opportunityId": setup_id,
            "notificationStage": stage, "previousState": previous_state,
            "currentState": state["state"], "direction": state["side"],
            "entryZone": dict(state["candidateZone"]), "stopLoss": state.get("defenseLevel"),
            "targets": state.get("targets") or [], "effectiveRR": state.get("rr"),
            "candidateZone": dict(state["candidateZone"]), "candidateSide": state["side"],
            "candidateDefenseLevel": state.get("defenseLevel"),
            "candidateTargets": state.get("targets") or [], "candidateRR": state.get("rr"),
            "marketBias": state.get("canonicalBias"), "dataHealth": state.get("dataHealth"),
            "candidateScore": state.get("candidateScore"),
            "candidateReasons": state.get("candidateReasons") or [],
            "candidateCreatedAt": state.get("candidateCreatedAt"),
            "transitionReason": state.get("transitionReason"),
            "calculatedAt": state.get("evaluatedAt"), "notificationEligible": True}


def evaluate_early_entry_candidate(data: dict, previous: dict | None = None) -> tuple[dict, list[dict]]:
    """Evaluate PREPARE/MISSED/INVALIDATED; never grant formal entry."""
    previous = previous or {"state": "IDLE"}
    normalized = data.get("normalized_analysis") or {}
    symbol = str(data.get("symbol") or "XAUUSD")
    now = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    price = _num(normalized.get("currentPrice"), 0.0) or 0.0
    atr = max(_num(normalized.get("atr15"), 0.01) or 0.01, 0.01)
    bias, health = _bias(data), _data_health(data)
    side = "LONG" if bias == "BULLISH" else "SHORT" if bias == "BEARISH" else "NONE"
    old_state = str(previous.get("state") or "IDLE")
    log = {"timestamp": now, "symbol": symbol, "canonical_bias": bias, "price": price,
           "data_health": health, "state_before": old_state,
           "candidate_reasons": [], "rejection_reasons": []}
    if old_state in ACTIVE:
        state = dict(previous)
        candidate_side = str(state.get("side") or "LONG")
        zone = (float(state["candidateZone"]["low"]), float(state["candidateZone"]["high"]))
        stop = _num(state.get("defenseLevel"))
        target = _num((state.get("targets") or [None])[0])
        break_context = data.get("break_lifecycle_engine") or {}
        break_state = str(break_context.get("state") or "")
        break_direction = str(break_context.get("direction") or "")
        structure_broken = (break_state in {"BREAK_CONFIRMED", "RECLAIM_FAILED"}
                            and ((candidate_side == "LONG" and break_direction == "DOWN")
                                 or (candidate_side == "SHORT" and break_direction == "UP")))
        bias_conflict = side not in {candidate_side, "NONE"}
        unsafe = health in {"STALE", "FAILED", "UNSAFE", "INSUFFICIENT"}
        stop_crossed = stop is not None and ((candidate_side == "LONG" and price < stop)
                                             or (candidate_side == "SHORT" and price > stop))
        chasing, chase_reason, rr = is_chasing_entry(
            side=candidate_side, current_price=price, zone=zone, stop=stop, target=target,
            atr=atr, minimum_rr=float(get_settings().decision_assistant_min_rr))
        if unsafe or bias_conflict or structure_broken or stop_crossed:
            reason = "DATA_UNSAFE" if unsafe else "BIAS_CONFLICT" if bias_conflict else "STRUCTURE_INVALIDATED"
            state.update({"state": "INVALIDATED", "transitionReason": reason,
                          "evaluatedAt": now, "dataHealth": health})
            log.update({"state_after": "INVALIDATED", "rejection_reasons": [reason]})
            state["evaluationLog"] = [*(state.get("evaluationLog") or []), log][-100:]
            return state, [_event(state, "INVALIDATED", old_state)]
        if chasing:
            missed = "MISSED_LONG" if candidate_side == "LONG" else "MISSED_SHORT"
            state.update({"state": missed, "transitionReason": chase_reason,
                          "evaluatedAt": now, "dataHealth": health, "rr": rr})
            log.update({"state_after": missed, "rejection_reasons": [chase_reason], "rr": rr})
            state["evaluationLog"] = [*(state.get("evaluationLog") or []), log][-100:]
            return state, [_event(state, "MISSED", old_state)]
        state.update({"evaluatedAt": now, "dataHealth": health})
        log["state_after"] = old_state
        state["evaluationLog"] = [*(state.get("evaluationLog") or []), log][-100:]
        return state, []
    opportunity = _candidate(data, side) if side != "NONE" else None
    if opportunity is None:
        log.update({"state_after": old_state, "rejection_reasons": [
            "BIAS_CONFLICT" if side == "NONE" else "NO_RUNTIME_ENTRY_ZONE"]})
        return {**previous, "state": old_state, "evaluatedAt": now, "canonicalBias": bias,
                "dataHealth": health,
                "evaluationLog": [*(previous.get("evaluationLog") or []), log][-100:]}, []
    zone = _zone(opportunity)
    assert zone is not None
    padding = atr * get_settings().early_entry_neighborhood_atr_mult
    nearby = zone[0] - padding <= price <= zone[1] + padding
    reasons, rejected = _evidence(data, side, price, atr, opportunity)
    if not nearby:
        rejected.append("PRICE_OUTSIDE_CANDIDATE_NEIGHBORHOOD")
    score = min(100, 35 + 20 * len(set(reasons)) + (10 if nearby else 0))
    if score < get_settings().early_entry_min_score:
        rejected.append("CANDIDATE_SCORE_TOO_LOW")
    log.update({"entry_zone": {"low": zone[0], "high": zone[1]},
                "candidate_score": score, "candidate_reasons": reasons,
                "rejection_reasons": list(dict.fromkeys(rejected)),
                "rr": opportunity.get("estimated_rr")})
    if rejected:
        log["state_after"] = old_state
        return {**previous, "state": old_state, "evaluatedAt": now, "canonicalBias": bias,
                "dataHealth": health,
                "evaluationLog": [*(previous.get("evaluationLog") or []), log][-100:]}, []
    setup_id = _setup_id(symbol, side, opportunity, zone)
    if old_state in TERMINAL and str(previous.get("setup_id") or "") == setup_id:
        log["state_after"] = old_state
        return {**previous, "evaluatedAt": now,
                "evaluationLog": [*(previous.get("evaluationLog") or []), log][-100:]}, []
    state_name = "PREPARE_LONG" if side == "LONG" else "PREPARE_SHORT"
    state = {"schemaVersion": "early-entry-candidate-v1", "state": state_name,
             "side": side, "setup_id": setup_id, "candidateSetupId": setup_id,
             "sourceOpportunityId": opportunity.get("opportunity_id"),
             "candidateCreatedAt": now, "candidatePrice": round(price, 2),
             "candidateZone": {"low": zone[0], "high": zone[1]},
             "candidateReason": reasons[0], "candidateReasons": reasons,
             "candidateScore": score, "canonicalBias": bias, "dataHealth": health,
             "defenseLevel": _num(opportunity.get("tactical_stop")),
             "targets": [v for v in [opportunity.get("target1")] if isinstance(v, (int, float))],
             "rr": opportunity.get("estimated_rr"), "evaluatedAt": now,
             "transitionReason": reasons[0]}
    log["state_after"] = state_name
    state["evaluationLog"] = [log]
    return state, [_event(state, "PREPARE", old_state)]


def apply_canonical_entry_result(state: dict, canonical: dict, *, evaluated_at: str) -> dict:
    """Promote only after the existing canonical engine grants formal entry."""
    if str(state.get("state") or "") not in ACTIVE:
        return state
    side = str(state.get("side") or "")
    action = str(canonical.get("primaryAction") or canonical.get("finalAction") or "")
    expected = {"LONG": {"BUY", "ENTER_LONG"}, "SHORT": {"SELL", "ENTER_SHORT"}}[side]
    health = str(canonical.get("dataHealth") or state.get("dataHealth") or "STALE").upper()
    if bool(canonical.get("canEnter")) and action in expected and health == "HEALTHY":
        return {**state, "state": "LONG_READY" if side == "LONG" else "SHORT_READY",
                "formalEntryConfirmedAt": evaluated_at,
                "transitionReason": "CANONICAL_ENTRY_GATE_PASSED"}
    return state
