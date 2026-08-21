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
        if risk_gate != "ENTRY_READY":
            errors.append("ENTRY_WITH_RISK_BLOCK")
        if lifecycle != "ENTRY_READY":
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
