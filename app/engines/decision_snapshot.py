"""Canonical V2 snapshot: the sole payload consumed by web and notifications."""
from __future__ import annotations

import hashlib

from app.engines.confidence import get_confidence_grade
from app.engines.data_health_gate import evaluate_data_health
from app.engines.execution_context import execution_cost, market_session


def _selected_setup(data: dict) -> dict:
    continuation = data.get("trend_continuation_engine") or {}
    if continuation.get("selected"):
        return continuation["selected"]
    ledger = data.get("breakout_setup_manager") or {}
    return ledger.get("activeSetup") or ledger.get("active_setup") or {}


def build_decision_snapshot(data: dict, *, risk_mode: str = "STANDARD") -> dict:
    from app.config import get_settings

    settings = get_settings()
    final = data.get("final_decision_state") or {}
    assistant = data.get("decision_assistant") or {}
    setup = _selected_setup(data)
    health = evaluate_data_health(data)
    decision = data.get("decision") or {}
    score = decision.get("signal_score", decision.get("evidence_score"))
    grade = get_confidence_grade(score)
    state = str(final.get("state") or assistant.get("tradeState") or setup.get("status") or "WAIT")
    can_enter = bool(final.get("canEnter")) and health["healthy"]
    if not health["healthy"]:
        state, can_enter, action = "DATA_STALE", False, "DATA_UNAVAILABLE"
    elif final.get("finalAction"):
        action = str(final["finalAction"])
    elif can_enter:
        action = "ENTER_LONG" if (setup.get("direction") or final.get("direction")) == "LONG" else "ENTER_SHORT"
    elif "RETEST" in state:
        action = "WAIT_RETEST"
    elif "MISSED" in state:
        action = "NO_CHASE"
    else:
        action = "WAIT_CONFIRMATION"
    setup_id = str(final.get("selectedScenarioId") or setup.get("setupId") or "")
    candle_time = str(final.get("last_closed_candle_time") or health["marketDataTimestamp"] or "")
    raw = f"XAUUSD|{setup_id}|{state}|{candle_time}|{action}"
    decision_id = hashlib.sha256(raw.encode()).hexdigest()[:24]
    event = data.get("event_risk") or {}
    return {
        "schemaVersion": "decision-snapshot-v3", "decisionId": final.get("decisionId") or decision_id,
        "decisionVersion": final.get("decisionVersion", 0),
        "symbol": data.get("symbol") or "XAUUSD", "setupId": setup_id,
        "setupVersion": final.get("selectedScenarioVersion") or setup.get("setupVersion") or "",
        "direction": final.get("direction") or "NEUTRAL",
        "marketType": assistant.get("regime") or (data.get("trend_continuation_engine") or {}).get("marketType") or "UNDEFINED",
        "state": state, "action": action, "canEnter": can_enter,
        "signalScore": score, "confidenceGrade": None if grade == "U" else grade,
        "confidenceLabel": {"A": "A級（高信心）", "B": "B級（中高信心）",
                            "C": "C級（中低信心）", "D": "D級（低信心）"}.get(grade, "未評級"),
        "setupQualityScore": assistant.get("entryQualityScore", setup.get("signalScore")),
        "entryQualityGrade": assistant.get("entryQualityGrade"),
        "tradeStatus": decision.get("trade_status") or state,
        "blockedReason": ("；".join(health["reasons"]) if not health["healthy"] else
                          final.get("humanSummary") or decision.get("blocked_reason") or final.get("reason") or ""),
        "entryZone": final.get("entryZone"),
        "chaseLimit": final.get("chaseLimit"),
        "stopLoss": final.get("invalidationPrice"), "targets": final.get("targets") or [],
        "riskReward": final.get("effectiveRR"),
        "actionSummary": final.get("humanSummary") or assistant.get("actionSummary") or action,
        "nextTrigger": assistant.get("nextTrigger") or final.get("next_trigger") or final.get("confirmation") or "等待新結構形成",
        "currentPrice": health["currentPrice"], "marketDataTimestamp": health["marketDataTimestamp"],
        "quoteTime": health["quoteTime"], "calculatedAt": data.get("timestamp_utc") or health["evaluatedAt"],
        "dataHealth": health, "riskMode": risk_mode.upper(),
        "marketSession": market_session(health["marketDataTimestamp"]),
        "executionCost": execution_cost(data, slippage_abs=settings.estimated_slippage_abs),
        "eventRisk": event.get("risk_level") or event.get("status") or "UNKNOWN",
        "eventDataStatus": event.get("data_status") or "UNKNOWN",
        "positionMode": "TRACKED" if (data.get("position_management") or {}).get("has_position") else "FLAT",
        "reasons": assistant.get("regimeReasons") or setup.get("passedReasons") or [],
        "missingConditions": assistant.get("noTradeReasons") or setup.get("missingConditions") or [],
        "decisionAssistant": assistant,
        "finalDecision": {
            "action": final.get("finalAction") or action,
            "primaryReason": final.get("primaryReason"),
            "secondaryReasons": final.get("secondaryReasons") or [],
            "humanSummary": final.get("humanSummary"),
            "riskGate": final.get("riskGate"),
        },
    }
