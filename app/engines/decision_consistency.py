"""Fail-closed invariants for the canonical final decision."""
from __future__ import annotations

from typing import Any


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def validate_final_decision(decision: dict) -> list[str]:
    errors: list[str] = []
    action = str(decision.get("finalAction") or "WAIT")
    direction = str(decision.get("direction") or "NEUTRAL")
    zone = decision.get("entryZone") or {}
    low, high = _number(zone.get("low")), _number(zone.get("high"))
    current = _number(decision.get("currentPrice"))
    chase = _number(decision.get("chaseLimit"))
    invalidation = _number(decision.get("invalidationPrice"))
    targets = [_number(value) for value in decision.get("targets") or []]
    tp1 = next((value for value in targets if value is not None), None)
    risk_gate = str(decision.get("riskGate") or "")
    lifecycle = str(decision.get("selectedLifecycleState") or "")
    versions = decision.get("priceScenarioVersions") or {}
    used_versions = {int(v) for v in versions.values() if isinstance(v, int)}
    live_state = str(decision.get("liveBiasState") or "ALIGNED")
    execution_bias = str(decision.get("executionBias") or "NEUTRAL")
    structural_side = ("LONG" if "BULL" in str(decision.get("structuralBias") or "")
                       else "SHORT" if "BEAR" in str(
                           decision.get("structuralBias") or "") else "NONE")

    if low is not None and high is not None and low > high:
        errors.append("ENTRY_ZONE_REVERSED")
    if direction == "LONG" and high is not None and chase is not None and high > chase:
        errors.append("LONG_ENTRY_ZONE_ABOVE_CHASE")
    if direction == "SHORT" and low is not None and chase is not None and low < chase:
        errors.append("SHORT_ENTRY_ZONE_BELOW_CHASE")
    if direction == "LONG" and high is not None and tp1 is not None and tp1 <= high:
        errors.append("LONG_TARGET_WRONG_SIDE")
    if direction == "SHORT" and low is not None and tp1 is not None and tp1 >= low:
        errors.append("SHORT_TARGET_WRONG_SIDE")
    if action in {"ENTER_LONG", "ENTER_SHORT"}:
        if live_state in {"INVALIDATING", "SUSPENDED"}:
            errors.append("ENTRY_DURING_LIVE_BIAS_SUSPENSION")
        if execution_bias == "NEUTRAL":
            errors.append("ENTRY_WITH_NEUTRAL_EXECUTION_BIAS")
        if direction == structural_side and live_state == "REVERSAL_CANDIDATE":
            errors.append("ENTRY_IN_INVALIDATED_STRUCTURAL_DIRECTION")
        if low is None or high is None or current is None or not low <= current <= high:
            errors.append("ENTRY_PRICE_OUTSIDE_ZONE")
        if action == "ENTER_LONG" and chase is not None and current is not None and current > chase:
            errors.append("LONG_ABOVE_CHASE_LIMIT")
        if action == "ENTER_SHORT" and chase is not None and current is not None and current < chase:
            errors.append("SHORT_BELOW_CHASE_LIMIT")
        if action == "ENTER_LONG" and invalidation is not None and low is not None and invalidation >= low:
            errors.append("LONG_INVALIDATION_WRONG_SIDE")
        if action == "ENTER_SHORT" and invalidation is not None and high is not None and invalidation <= high:
            errors.append("SHORT_INVALIDATION_WRONG_SIDE")
        if risk_gate not in {"ENTRY_READY", "PROBE_READY"}:
            errors.append("ENTRY_WITH_RISK_BLOCK")
        if (risk_gate == "ENTRY_READY" and lifecycle != "ENTRY_READY") or (
                risk_gate == "PROBE_READY" and lifecycle not in {"ENTRY_READY", "CONFIRMED"}):
            errors.append("ENTRY_WITHOUT_READY_LIFECYCLE")
    if len(used_versions) > 1:
        errors.append("MIXED_SCENARIO_VERSIONS")
    if direction == "LONG" and action == "ENTER_SHORT":
        errors.append("DIRECTION_ACTION_CONFLICT")
    if direction == "SHORT" and action == "ENTER_LONG":
        errors.append("DIRECTION_ACTION_CONFLICT")
    return sorted(set(errors))


def fail_closed(decision: dict, errors: list[str]) -> dict:
    safe = dict(decision)
    safe.update({
        "finalAction": "NO_TRADE", "state": "NO_TRADE", "canEnter": False,
        "primaryReason": "SYSTEM_DECISION_CONFLICT",
        "noTradeReason": "SYSTEM_DECISION_CONFLICT", "riskGate": "INTERNAL_CONFLICT",
        "humanSummary": "系統正在重新確認條件，暫時不給新的進場建議。",
        "notificationSeverity": "CRITICAL", "consistencyErrors": errors,
    })
    return safe


def validate_canonical_contract(decision: dict) -> list[str]:
    """Cross-field invariants for API, dashboard and Telegram's shared view."""
    errors: list[str] = []
    entry = decision.get("newEntryDecision") or {}
    selected = entry.get("selectedSetup") or {}
    action = str(decision.get("primaryAction") or "WAIT")
    can_enter = bool(entry.get("canEnter"))
    active_id = str(decision.get("activeSetupId") or "")
    engine_selected_id = str(decision.get("engineSelectedSetupId") or "")
    selected_id = str(selected.get("setupId") or "")
    trigger = decision.get("canonicalNextTrigger") or {}
    position = decision.get("positionManagement") or {}
    live_state = str(decision.get("liveBiasState") or "ALIGNED")
    execution_bias = str(decision.get("executionBias") or "NEUTRAL")

    if action in {"BUY", "SELL"} and not can_enter:
        errors.append("ACTION_WITHOUT_ENTRY_PERMISSION")
    if can_enter:
        if live_state in {"INVALIDATING", "SUSPENDED"} or execution_bias == "NEUTRAL":
            errors.append("CAN_ENTER_DURING_LIVE_BIAS_SUSPENSION")
        if not bool(decision.get("executionAllowed")):
            errors.append("ENTRY_WITH_EXECUTION_BLOCKED")
        if not bool(decision.get("rrValid")):
            errors.append("ENTRY_WITH_RR_BLOCKED")
        if bool(decision.get("dataStale")) or not bool(
                decision.get("closedCandleAvailable")):
            errors.append("ENTRY_WITH_STALE_OR_OPEN_CANDLE")
        if str(decision.get("scenarioValidity")) != "ACTIVE":
            errors.append("ENTRY_WITH_INACTIVE_SCENARIO")
        if str(entry.get("tradeStatus")) not in {"ENTRY_READY", "PROBE_READY"}:
            errors.append("ENTRY_PERMISSION_STATUS_CONFLICT")
    if active_id != selected_id:
        errors.append("ACTIVE_SETUP_SELECTION_CONFLICT")
    if engine_selected_id and engine_selected_id != active_id:
        errors.append("ENGINE_CANONICAL_SETUP_CONFLICT")
    trigger_setup = str(trigger.get("setupId") or "")
    if trigger_setup and active_id and trigger_setup != active_id:
        errors.append("NEXT_TRIGGER_SETUP_CONFLICT")
    if str(trigger.get("status") or "") in {"SATISFIED", "CLOSED_CONFIRMED"}:
        errors.append("COMPLETED_TRIGGER_EXPOSED_AS_NEXT")
    candidate_permissions = [item for item in [selected] + list(
        decision.get("alternativeSetups") or []) if item.get("canEnter")]
    if len(candidate_permissions) > 1:
        errors.append("MULTIPLE_EXECUTABLE_SETUPS")
    if not bool(position.get("positionKnown")):
        if any(position.get(key) is not None for key in (
                "actualSide", "actualEntryPrice", "actualSize", "action",
                "tacticalDefense", "structuralInvalidation")):
            errors.append("UNKNOWN_POSITION_HAS_MANAGEMENT_VALUES")
        if position.get("targets"):
            errors.append("UNKNOWN_POSITION_HAS_TARGETS")
    return sorted(set(errors))


def fail_closed_canonical(decision: dict, errors: list[str]) -> dict:
    safe = dict(decision)
    entry = dict(safe.get("newEntryDecision") or {})
    entry.update({
        "action": "WAIT", "canEnter": False,
        "tradeStatus": "SYSTEM_CONFLICT",
    })
    safe.update({
        "primaryAction": ((safe.get("positionManagement") or {}).get("action")
                          if (safe.get("positionManagement") or {}).get("positionKnown")
                          else "WAIT"),
        "primaryReason": "系統發現決策資料互相矛盾，已暫停新的進場建議並重新計算。",
        "executionAllowed": False,
        "canonicalNextTrigger": {
            "setupId": None, "timeframe": "15M",
            "condition": "recalculateCanonicalDecision", "status": "PENDING",
            "source": "CLOSED_CANDLE", "label": "等待系統完成一致性重新計算",
        },
        "primaryNextTrigger": {
            "setupId": None, "timeframe": "15M",
            "condition": "recalculateCanonicalDecision", "status": "PENDING",
            "source": "CLOSED_CANDLE", "label": "等待系統完成一致性重新計算",
        },
        "consistencyErrors": errors,
        "newEntryDecision": entry,
    })
    completeness = dict(safe.get("decisionCompleteness") or {})
    completeness["valid"] = False
    completeness["errors"] = list(dict.fromkeys(
        list(completeness.get("errors") or []) + errors))
    safe["decisionCompleteness"] = completeness
    return safe
