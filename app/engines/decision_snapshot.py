"""Canonical V2 snapshot: the sole payload consumed by web and notifications."""
from __future__ import annotations

import hashlib

from app.engines.confidence import get_confidence_grade
from app.engines.data_health_gate import evaluate_data_health


def _selected_setup(data: dict) -> dict:
    continuation = data.get("trend_continuation_engine") or {}
    if continuation.get("selected"):
        return continuation["selected"]
    ledger = data.get("breakout_setup_manager") or {}
    return ledger.get("activeSetup") or ledger.get("active_setup") or {}


def build_decision_snapshot(data: dict, *, risk_mode: str = "STANDARD") -> dict:
    final = data.get("final_decision_state") or {}
    setup = _selected_setup(data)
    health = evaluate_data_health(data)
    decision = data.get("decision") or {}
    score = decision.get("signal_score", decision.get("evidence_score"))
    grade = get_confidence_grade(score)
    state = str(final.get("state") or setup.get("status") or "WAIT")
    can_enter = bool(decision.get("can_enter")) and ("READY" in state) and health["healthy"]
    if not health["healthy"]:
        state, can_enter, action = "DATA_STALE", False, "DATA_UNAVAILABLE"
    elif can_enter:
        action = "ENTER_LONG" if (setup.get("direction") or final.get("direction")) == "LONG" else "ENTER_SHORT"
    elif "RETEST" in state:
        action = "WAIT_RETEST"
    elif "MISSED" in state:
        action = "NO_CHASE"
    else:
        action = "WAIT_CONFIRMATION"
    setup_id = str(setup.get("setupId") or final.get("setup_id") or "")
    candle_time = str(final.get("last_closed_candle_time") or health["marketDataTimestamp"] or "")
    raw = f"XAUUSD|{setup_id}|{state}|{candle_time}|{action}"
    decision_id = hashlib.sha256(raw.encode()).hexdigest()[:24]
    event = data.get("event_risk") or {}
    return {
        "schemaVersion": "decision-snapshot-v2", "decisionId": decision_id,
        "symbol": data.get("symbol") or "XAUUSD", "setupId": setup_id,
        "setupVersion": setup.get("setupVersion") or "", "direction": setup.get("direction") or final.get("direction") or "NEUTRAL",
        "marketType": (data.get("trend_continuation_engine") or {}).get("marketType") or "UNDEFINED",
        "state": state, "action": action, "canEnter": can_enter,
        "signalScore": score, "confidenceGrade": None if grade == "U" else grade,
        "confidenceLabel": {"A": "A級（高信心）", "B": "B級（中高信心）",
                            "C": "C級（中低信心）", "D": "D級（低信心）"}.get(grade, "未評級"),
        "setupQualityScore": setup.get("signalScore"), "tradeStatus": decision.get("trade_status") or state,
        "blockedReason": ("；".join(health["reasons"]) if not health["healthy"] else
                          decision.get("blocked_reason") or final.get("reason") or ""),
        "entryZone": {"low": setup.get("entryZoneLow"), "high": setup.get("entryZoneHigh")},
        "stopLoss": setup.get("stopPrice"), "targets": [setup.get("tp1"), setup.get("tp2"), setup.get("tp3")],
        "riskReward": setup.get("riskReward"), "nextTrigger": final.get("next_trigger") or final.get("confirmation") or "等待新結構形成",
        "currentPrice": health["currentPrice"], "marketDataTimestamp": health["marketDataTimestamp"],
        "quoteTime": health["quoteTime"], "calculatedAt": data.get("timestamp_utc") or health["evaluatedAt"],
        "dataHealth": health, "riskMode": risk_mode.upper(),
        "eventRisk": event.get("risk_level") or event.get("status") or "UNKNOWN",
        "eventDataStatus": event.get("data_status") or "UNKNOWN",
        "positionMode": "TRACKED" if (data.get("position_management") or {}).get("has_position") else "FLAT",
        "reasons": setup.get("passedReasons") or [], "missingConditions": setup.get("missingConditions") or [],
    }
