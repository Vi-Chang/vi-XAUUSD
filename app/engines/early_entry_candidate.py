"""Independent opportunity lifecycle; formal entry remains canonical."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

ACTIVE = {"WATCH_LONG", "WATCH_SHORT", "PREPARE_LONG", "PREPARE_SHORT"}
TERMINAL = {"LONG_READY", "SHORT_READY", "MISSED_LONG", "MISSED_SHORT",
            "INVALIDATED_LONG", "INVALIDATED_SHORT", "INVALIDATED"}


def _num(value: Any, default: float | None = None) -> float | None:
    return float(value) if isinstance(value, (int, float)) else default


def _zone(item: dict) -> tuple[float, float] | None:
    raw = item.get("entry_zone") or item.get("entryZone") or {}
    low = _num(raw.get("lower") if raw.get("lower") is not None else raw.get("low"))
    high = _num(raw.get("upper") if raw.get("upper") is not None else raw.get("high"))
    return (min(low, high), max(low, high)) if low is not None and high is not None else None


def _bias(data: dict) -> str:
    health, normalized = data.get("decision_health_state") or {}, data.get("normalized_analysis") or {}
    value = str(health.get("marketBias") or normalized.get("trendBias") or "NEUTRAL").upper()
    return {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(value, value)


def opportunity_capabilities(data: dict) -> dict:
    """Separate live-price observation from closed-candle execution capability."""
    health = data.get("decision_health_state") or {}
    normalized = data.get("normalized_analysis") or {}
    status = str(health.get("dataHealth") or normalized.get("marketDataStatus") or "STALE").upper()
    current = _num(normalized.get("currentPrice"))
    price_available = current is not None and current > 0
    critical = status in {"FAILED", "UNSAFE", "INSUFFICIENT", "INVALID_PRICE",
                          "MISSING_CANDLE", "SOURCE_DIVERGENCE", "CRITICAL_DATA_UNSAFE"}
    degraded = status in {"DEGRADED", "DEGRADED_15M", "STALE"}
    return {
        "status": "CRITICAL_DATA_UNSAFE" if critical or not price_available
        else "DEGRADED_15M" if degraded else "HEALTHY",
        "watchAllowed": bool(price_available and not critical),
        "prepareAllowed": bool(price_available and not critical),
        "entryAllowed": bool(price_available and not critical and not degraded),
        "reasons": (["LIVE_PRICE_UNAVAILABLE"] if not price_available else
                    ["CRITICAL_MARKET_DATA"] if critical else
                    ["CLOSED_15M_DEGRADED"] if degraded else []),
    }


def _candidate(data: dict, side: str) -> dict | None:
    engine = data.get("entry_opportunity_engine") or {}
    primary_id = str(engine.get("primaryOpportunityId") or "")
    items = [item for item in engine.get("opportunities") or []
             if str(item.get("side") or "").upper() == side
             and str(item.get("state") or "") not in {"EXPIRED", "INVALIDATED", "REJECTED"}
             and _zone(item)]
    items.sort(key=lambda item: (
        str(item.get("opportunity_id") or "") != primary_id,
        not bool(item.get("primary_eligible", True)),
        float(item.get("distance_from_current") or item.get("anchor_distance") or 0),
        -int(item.get("opportunity_score") or 0)))
    return items[0] if items else None


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
    event_type = {"WATCH": "EARLY_ENTRY_WATCH", "PREPARE": "EARLY_ENTRY_PREPARE",
                  "MISSED": "EARLY_ENTRY_MISSED", "INVALIDATED": "EARLY_ENTRY_INVALIDATED"}[stage]
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
            "rejectionReasons": state.get("rejectionReasons") or [],
            "nextAction": state.get("nextAction"),
            "candidateCreatedAt": state.get("candidateCreatedAt"),
            "transitionReason": state.get("transitionReason"),
            "calculatedAt": state.get("evaluatedAt"), "notificationEligible": True}


def _previous_candidates(previous: dict) -> dict[str, dict]:
    if previous.get("candidates"):
        return {str(k): dict(v) for k, v in previous["candidates"].items()}
    side = str(previous.get("side") or "")
    return {side: dict(previous)} if side in {"LONG", "SHORT"} else {}


def _evaluate_side(data: dict, side: str, previous: dict, *, symbol: str, now: str,
                   price: float, atr: float, bias: str, capabilities: dict) -> tuple[dict, list[dict]]:
    old_state = str(previous.get("state") or "IDLE")
    log = {"timestamp": now, "symbol": symbol, "side": side, "canonical_bias": bias,
           "price": price, "data_health": capabilities["status"], "state_before": old_state,
           "candidate_reasons": [], "rejection_reasons": []}
    if old_state in ACTIVE:
        state = dict(previous)
        active_zone = (float(state["candidateZone"]["low"]),
                       float(state["candidateZone"]["high"]))
        stop = _num(state.get("defenseLevel"))
        target = _num((state.get("targets") or [None])[0])
        break_context = data.get("break_lifecycle_engine") or {}
        structure_broken = (str(break_context.get("state") or "") in {"BREAK_CONFIRMED", "RECLAIM_FAILED"}
                            and ((side == "LONG" and str(break_context.get("direction") or "") == "DOWN")
                                 or (side == "SHORT" and str(break_context.get("direction") or "") == "UP")))
        stop_crossed = stop is not None and ((side == "LONG" and price < stop)
                                             or (side == "SHORT" and price > stop))
        if not capabilities["watchAllowed"] or structure_broken or stop_crossed:
            reason = "DATA_UNSAFE" if not capabilities["watchAllowed"] else "STRUCTURE_INVALIDATED"
            terminal = "INVALIDATED_LONG" if side == "LONG" else "INVALIDATED_SHORT"
            state.update({"state": terminal, "transitionReason": reason, "evaluatedAt": now,
                          "dataHealth": capabilities["status"], "nextAction": "等待新結構"})
            log.update({"state_after": terminal, "rejection_reasons": [reason]})
            state["evaluationLog"] = [*(state.get("evaluationLog") or []), log][-100:]
            return state, [_event(state, "INVALIDATED", old_state)]
        chasing, chase_reason, rr = is_chasing_entry(
            side=side, current_price=price, zone=active_zone, stop=stop, target=target,
            atr=atr, minimum_rr=float(get_settings().decision_assistant_min_rr))
        if chasing:
            terminal = "MISSED_LONG" if side == "LONG" else "MISSED_SHORT"
            state.update({"state": terminal, "transitionReason": chase_reason,
                          "evaluatedAt": now, "dataHealth": capabilities["status"], "rr": rr,
                          "nextAction": "等待價格回到合理區域或建立新劇本"})
            log.update({"state_after": terminal, "rejection_reasons": [chase_reason], "rr": rr})
            state["evaluationLog"] = [*(state.get("evaluationLog") or []), log][-100:]
            return state, [_event(state, "MISSED", old_state)]
        opportunity = _candidate(data, side)
        reasons, rejected = _evidence(data, side, price, atr, opportunity or {})
        if old_state.startswith("WATCH") and capabilities["prepareAllowed"] and not rejected:
            prepared = "PREPARE_LONG" if side == "LONG" else "PREPARE_SHORT"
            state.update({"state": prepared, "candidateReasons": reasons,
                          "candidateScore": min(100, 55 + 15 * len(reasons)),
                          "transitionReason": reasons[0] if reasons else "REACTION_AND_STRUCTURE_CONFIRMED",
                          "evaluatedAt": now, "nextAction": "等待正式收盤與風控閘門確認"})
            log["state_after"] = prepared
            state["evaluationLog"] = [*(state.get("evaluationLog") or []), log][-100:]
            return state, [_event(state, "PREPARE", old_state)]
        state.update({"evaluatedAt": now, "dataHealth": capabilities["status"]})
        log["state_after"] = old_state
        state["evaluationLog"] = [*(state.get("evaluationLog") or []), log][-100:]
        return state, []
    opportunity = _candidate(data, side)
    if opportunity is None:
        log.update({"state_after": old_state, "rejection_reasons": ["NO_RUNTIME_ENTRY_ZONE"]})
        return {"state": old_state, "side": side, "evaluatedAt": now,
                "canonicalBias": bias, "dataHealth": capabilities["status"],
                "rejectionReasons": ["NO_RUNTIME_ENTRY_ZONE"],
                "nextAction": "等待形成有效進場區", "evaluationLog": [log]}, []
    zone = _zone(opportunity)
    assert zone is not None
    watch_padding = atr * get_settings().early_entry_watch_proximity_atr_mult
    prepare_padding = atr * get_settings().early_entry_neighborhood_atr_mult
    watch_nearby = zone[0] - watch_padding <= price <= zone[1] + watch_padding
    prepare_nearby = zone[0] - prepare_padding <= price <= zone[1] + prepare_padding
    rr = _num(opportunity.get("estimated_rr"), 0.0) or 0.0
    reasons, rejected = _evidence(data, side, price, atr, opportunity)
    hard_rejections = []
    break_context = data.get("break_lifecycle_engine") or {}
    if (str(break_context.get("state") or "") in {"BREAK_CONFIRMED", "RECLAIM_FAILED"}
            and ((side == "LONG" and str(break_context.get("direction") or "") == "DOWN")
                 or (side == "SHORT" and str(break_context.get("direction") or "") == "UP"))):
        hard_rejections.append("STRUCTURE_INVALIDATED")
    if not capabilities["watchAllowed"]:
        hard_rejections.append("DATA_CAPABILITY_BLOCKS_WATCH")
    if not watch_nearby:
        hard_rejections.append("PRICE_OUTSIDE_WATCH_DISTANCE")
        hard_rejections.append("PRICE_OUTSIDE_CANDIDATE_NEIGHBORHOOD")
    if rr < get_settings().early_entry_watch_min_rr:
        hard_rejections.append("ESTIMATED_RR_TOO_LOW")
    setup_id = _setup_id(symbol, side, opportunity, zone)
    log.update({"entry_zone": {"low": zone[0], "high": zone[1]}, "rr": rr,
                "candidate_reasons": reasons,
                "rejection_reasons": list(dict.fromkeys(hard_rejections + rejected))})
    if hard_rejections or (old_state in TERMINAL and previous.get("setup_id") == setup_id):
        log["state_after"] = old_state
        return {**previous, "state": old_state, "side": side, "evaluatedAt": now,
                "canonicalBias": bias, "dataHealth": capabilities["status"],
                "rejectionReasons": log["rejection_reasons"],
                "nextAction": previous.get("nextAction") or "等待價格接近有效區域",
                "evaluationLog": [*(previous.get("evaluationLog") or []), log][-100:]}, []
    countertrend = ((bias == "BULLISH" and side == "SHORT") or
                    (bias == "BEARISH" and side == "LONG"))
    can_prepare = prepare_nearby and not rejected and capabilities["prepareAllowed"]
    state_name = (("PREPARE_" if can_prepare else "WATCH_") + side)
    score = min(100, (55 if can_prepare else 40) + 15 * len(reasons) - (15 if countertrend else 0))
    state = {"schemaVersion": "opportunity-liveness-v2", "state": state_name,
             "side": side, "setup_id": setup_id, "candidateSetupId": setup_id,
             "sourceOpportunityId": opportunity.get("opportunity_id"),
             "candidateCreatedAt": now, "candidatePrice": round(price, 2),
             "candidateZone": {"low": zone[0], "high": zone[1]},
             "candidateReason": (reasons[0] if reasons else "PRICE_APPROACHING_VALID_ZONE"),
             "candidateReasons": reasons, "rejectionReasons": rejected,
             "candidateScore": score, "canonicalBias": bias,
             "countertrend": countertrend, "dataHealth": capabilities["status"],
             "defenseLevel": _num(opportunity.get("tactical_stop")),
             "targets": [v for v in [opportunity.get("target1")] if isinstance(v, (int, float))],
             "rr": rr, "evaluatedAt": now,
             "transitionReason": (reasons[0] if can_prepare and reasons else "PRICE_APPROACHING_VALID_ZONE"),
             "nextAction": ("等待正式收盤與風控閘門確認" if can_prepare
                            else "等待價格反應與短線結構確認")}
    log["state_after"] = state_name
    state["evaluationLog"] = [log]
    return state, [_event(state, "PREPARE" if can_prepare else "WATCH", old_state)]


def _primary(candidates: dict[str, dict], bias: str) -> dict:
    preferred = "LONG" if bias == "BULLISH" else "SHORT" if bias == "BEARISH" else ""
    rank = {"LONG_READY": 6, "SHORT_READY": 6, "PREPARE_LONG": 5, "PREPARE_SHORT": 5,
            "WATCH_LONG": 4, "WATCH_SHORT": 4, "MISSED_LONG": 3, "MISSED_SHORT": 3,
            "INVALIDATED_LONG": 2, "INVALIDATED_SHORT": 2, "IDLE": 1}
    return max(candidates.values(), key=lambda item: (
        rank.get(str(item.get("state") or "IDLE"), 0),
        str(item.get("side") or "") == preferred,
        int(item.get("candidateScore") or 0)))


def evaluate_early_entry_candidate(data: dict, previous: dict | None = None) -> tuple[dict, list[dict]]:
    """Evaluate both sides continuously; never grant formal entry."""
    previous = previous or {"state": "IDLE"}
    normalized = data.get("normalized_analysis") or {}
    symbol = str(data.get("symbol") or "XAUUSD")
    now = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    price = _num(normalized.get("currentPrice"), 0.0) or 0.0
    atr = max(_num(normalized.get("atr15"), 0.01) or 0.01, 0.01)
    bias, capabilities = _bias(data), opportunity_capabilities(data)
    prior = _previous_candidates(previous)
    candidates, events = {}, []
    for side in ("LONG", "SHORT"):
        state, side_events = _evaluate_side(
            data, side, prior.get(side, {"state": "IDLE", "side": side}),
            symbol=symbol, now=now, price=price, atr=atr, bias=bias,
            capabilities=capabilities)
        candidates[side] = state
        events.extend(side_events)
    primary = dict(_primary(candidates, bias))
    return {**primary, "schemaVersion": "opportunity-liveness-v2",
            "candidates": candidates, "capabilities": capabilities,
            "evaluatedAt": now}, events


def apply_canonical_entry_result(state: dict, canonical: dict, *, evaluated_at: str) -> dict:
    """Promote only after the existing canonical engine grants formal entry."""
    candidates = _previous_candidates(state)
    action = str(canonical.get("primaryAction") or canonical.get("finalAction") or "")
    health = str(canonical.get("dataHealth") or state.get("dataHealth") or "STALE").upper()
    target_side = "LONG" if action in {"BUY", "ENTER_LONG"} else "SHORT" if action in {"SELL", "ENTER_SHORT"} else ""
    if bool(canonical.get("canEnter")) and target_side and health == "HEALTHY":
        candidate = candidates.get(target_side) or {}
        if str(candidate.get("state") or "") in ACTIVE:
            candidates[target_side] = {**candidate, "state": f"{target_side}_READY",
                                       "formalEntryConfirmedAt": evaluated_at,
                                       "transitionReason": "CANONICAL_ENTRY_GATE_PASSED"}
    if not candidates:
        return state
    primary = _primary(candidates, str(state.get("canonicalBias") or "NEUTRAL"))
    return {**primary, "schemaVersion": "opportunity-liveness-v2",
            "candidates": candidates, "capabilities": state.get("capabilities") or {},
            "evaluatedAt": evaluated_at}
