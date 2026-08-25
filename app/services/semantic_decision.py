"""Stable user-action identity for Telegram decisions."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.engines.multi_timeframe_bias import derive_multi_timeframe_bias


def _canonical(payload: dict) -> dict:
    return dict(payload.get("canonicalDecision") or {})


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _rr_state(payload: dict, canonical: dict) -> str:
    entry = canonical.get("newEntryDecision") or {}
    selected = entry.get("selectedSetup") or {}
    rr = next((value for value in (
        payload.get("effectiveRR"), payload.get("riskReward"),
        selected.get("executableRR"), selected.get("riskReward"),
        selected.get("estimatedRR"),
    ) if isinstance(value, (int, float))), None)
    if rr is None:
        return "UNKNOWN"
    minimum = _number(payload.get("minimumRR") or canonical.get("minimumRR")) or 1.5
    return "VALID" if float(rr) >= minimum else "BELOW_MINIMUM"


def _entry_zone_state(payload: dict, canonical: dict) -> str:
    explicit = (payload.get("entryZoneState") or payload.get("zoneState") or
                (canonical.get("newEntryDecision") or {}).get("entryZoneState"))
    if explicit:
        return str(explicit).upper()
    state = str(payload.get("currentState") or canonical.get("setupState") or "WAIT").upper()
    event_type = str(payload.get("event_type") or "").upper()
    if "MISSED" in state or "MISSED" in event_type or "RAN_AWAY" in event_type:
        return "MISSED"
    if "APPROACH" in state or "APPROACH" in event_type:
        return "APPROACHING"
    if ("ENTRY_READY" in state or state.endswith("_READY") or
            event_type in {"ENTRY_READY", "ENTRY_NOW", "RETRACE_ZONE_ENTERED"}):
        return "INSIDE"
    return "OUTSIDE"


def _trigger_state(payload: dict, canonical: dict) -> tuple[str, str]:
    trigger = canonical.get("canonicalNextTrigger") or payload.get("canonicalNextTrigger") or {}
    status = str(trigger.get("status") or payload.get("triggerStatus") or
                 payload.get("entryConfirmation") or canonical.get("entryConfirmation") or
                 "NOT_TRIGGERED").upper()
    event_type = str(payload.get("event_type") or "").upper()
    if "FAILED" in status or "INVALID" in status:
        semantic = "FAILED"
    elif (("CONFIRM" in status and not status.startswith("WAIT") and
           "NOT_" not in status and "UNCONFIRM" not in status) or event_type in {
            "BREAKOUT_CONFIRMED", "BREAK_CONFIRMED", "RECLAIM_CONFIRMED",
            "ENTRY_READY", "ENTRY_NOW"}):
        semantic = "CONFIRMED"
    elif "APPROACH" in status or "APPROACH" in event_type:
        semantic = "APPROACHING"
    else:
        semantic = "NOT_TRIGGERED"
    trigger_type = str(trigger.get("condition") or payload.get("triggerType") or
                       payload.get("setupType") or "NONE").upper()
    return trigger_type, semantic


def build_semantic_decision(payload: dict) -> dict[str, Any]:
    """Return only fields whose transition can change the trader's action."""
    canonical = _canonical(payload)
    multi = payload.get("multiTimeframeBias") or canonical.get("multiTimeframeBias")
    if not multi:
        multi = derive_multi_timeframe_bias(
            payload.get("normalized_analysis") or canonical or payload,
            canonical_bias=str(payload.get("marketBias") or
                               canonical.get("marketBias") or "NEUTRAL"),
        )
    scalp = payload.get("scalpDecision") or canonical.get("scalpDecision") or {}
    entry = canonical.get("newEntryDecision") or {}
    position = canonical.get("positionManagement") or {}
    trigger_type, trigger_status = _trigger_state(payload, canonical)
    return {
        "symbol": str(payload.get("symbol") or "XAUUSD"),
        "canonicalAction": str(payload.get("finalDecision") or payload.get("finalAction") or
                               canonical.get("primaryAction") or entry.get("action") or
                               "WAIT").upper(),
        "shortTermBias": str(scalp.get("scalpBias") or
                             multi.get("shortTermBias") or "UNKNOWN"),
        "preferredScalpSide": str(scalp.get("preferredSide") or
                                  payload.get("preferredScalpSide") or ""),
        "bias15m": str(multi.get("bias15m") or "UNKNOWN"),
        "bias1h": str(multi.get("bias1h") or "UNKNOWN"),
        "bias4h": str(multi.get("bias4h") or "UNKNOWN"),
        "triggerType": trigger_type,
        "triggerStatus": trigger_status,
        "entryZoneState": _entry_zone_state(payload, canonical),
        "rrState": _rr_state(payload, canonical),
        "defenseState": str(payload.get("defenseState") or
                            canonical.get("defenseState") or "NORMAL"),
        "positionState": str(position.get("action") or payload.get("positionState") or
                             (payload.get("event_type") if payload.get("positionId") else "NONE")),
        "dataHealthState": str(payload.get("dataHealth") or
                               canonical.get("dataHealth") or "UNKNOWN"),
        "strategyPhase": str(payload.get("strategyPhase") or
                             canonical.get("scenarioState") or
                             payload.get("currentState") or "WAIT"),
        "scenarioId": str(payload.get("scenarioId") or payload.get("setupId") or
                          canonical.get("activeSetupId") or ""),
    }


def build_decision_signature(payload: dict) -> str:
    raw = json.dumps(build_semantic_decision(payload), ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def detect_meaningful_transition(previous: dict | None,
                                 current: dict) -> str | None:
    if previous is None:
        return "FIRST_NOTIFICATION"
    before, after = build_semantic_decision(previous), build_semantic_decision(current)
    if before == after:
        return None
    ordered = (
        ("positionState", "POSITION_CHANGED"), ("defenseState", "DEFENSE_CHANGED"),
        ("canonicalAction", "ACTION_CHANGED"), ("shortTermBias", "BIAS_CHANGED"),
        ("preferredScalpSide", "BIAS_CHANGED"), ("bias15m", "BIAS_CHANGED"),
        ("bias1h", "BIAS_CHANGED"), ("triggerStatus", "TRIGGER_STATUS_CHANGED"),
        ("entryZoneState", "ENTRY_ZONE_STATE_CHANGED"),
        ("rrState", "RR_STATE_CHANGED"), ("dataHealthState", "DATA_HEALTH_CHANGED"),
        ("strategyPhase", "STRATEGY_PHASE_CHANGED"),
        ("scenarioId", "SCENARIO_CHANGED"), ("triggerType", "TRIGGER_CHANGED"),
        ("bias4h", "BACKGROUND_BIAS_CHANGED"),
    )
    for field, reason in ordered:
        if before[field] == after[field]:
            continue
        if field == "triggerStatus" and after[field] == "CONFIRMED":
            return "TRIGGER_CONFIRMED"
        if field == "entryZoneState" and after[field] == "INSIDE":
            return "ENTRY_ZONE_ENTERED"
        if field == "rrState":
            return "RR_BECAME_VALID" if after[field] == "VALID" else "RR_BECAME_INVALID"
        if field == "dataHealthState":
            return ("DATA_HEALTH_RECOVERED" if after[field] == "HEALTHY"
                    else "DATA_HEALTH_CRITICAL")
        return reason
    return None
