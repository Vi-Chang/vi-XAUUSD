"""Canonical event priority, eligibility, TTL and trace metadata."""
from __future__ import annotations

from datetime import datetime, timezone

CRITICAL = {"STOP_TRIGGERED", "POSITION_EXIT", "HARD_INVALIDATED"}
ACTION = {
    "ENTRY_READY", "ENTRY_NOW", "POSITION_DEFEND", "SETUP_INVALIDATED",
    "SOFT_INVALIDATED", "EXIT_NOW", "ENTRY_INVALIDATED",
    "EARLY_ENTRY_INVALIDATED",
}
IMPORTANT = {
    "DATA_DELAYED", "DATA_STALE", "DATA_RECOVERED", "DOUBLE_SWEEP_CONFIRMED",
    "FAILED_BREAKOUT", "FAILED_BREAKDOWN", "LIQUIDITY_SWEEP_HIGH",
    "LIQUIDITY_SWEEP_LOW", "WAIT_RETEST", "MISSED_ENTRY", "SETUP_EXPIRED",
    "TP1_HIT", "TP2_HIT", "TP3_HIT", "TRAIL_UPDATED", "POSITION_REDUCE",
    "WHIPSAW_DETECTED", "MARKET_REOPENED",
    "POSITION_WARNING", "SOFT_INVALIDATION_PENDING", "POSITION_RECOVERED",
    "POSITION_DATA_RISK",
    "BIAS_CHANGE", "SETUP_FORMING", "ENTRY_APPROACHING", "PRICE_RAN_AWAY",
    "RETRACE_APPROACHING", "RETRACE_ZONE_ENTERED", "SETUP_WEAKENING",
    "REENTRY_AVAILABLE", "TARGET_UPDATED", "TP_APPROACHING", "TP_HIT",
    "TRAILING_STOP_UPDATE", "EXIT_WARNING", "NEW_STRUCTURE",
    "BREAK_PENDING", "BREAK_CONFIRMED", "RECLAIM_FAILED",
    "LIQUIDITY_SWEEP_CANDIDATE", "PROFIT_GIVEBACK_ALERT",
    "PROFIT_STATE_CHANGED",
    "FAKE_BREAKOUT_CONFIRMED", "OPPOSITE_SETUP_CONFIRMED",
    "RECOVERY_SETUP_INVALIDATED",
    "DEFENSE_TEST", "DEFENSE_RECLAIMED", "DEFENSE_HELD",
    "DEFENSE_BROKEN_CONFIRMED",
    "EARLY_ENTRY_PREPARE", "EARLY_ENTRY_MISSED",
}
INFO = {"REGIME_MAJOR_CHANGE", "MARKET_CLOSED", "SETUP_CREATED", "SETUP_ARMED"}
WAIT_STATES = {"WAIT", "NO_TRADE", "WAIT_CONFIRMATION"}


def priority(event_type: str, payload: dict) -> str:
    if event_type in {"DATA_DELAYED", "DATA_STALE"} and payload.get("positionId"):
        return "CRITICAL"
    if event_type in CRITICAL:
        return "CRITICAL"
    if event_type in ACTION:
        return "ACTION"
    if event_type in IMPORTANT:
        return "IMPORTANT"
    if event_type in INFO:
        return "INFO"
    return "DEBUG"


def eligibility(payload: dict) -> dict:
    event_type = str(payload.get("event_type") or "DECISION_UPDATED")
    level = priority(event_type, payload)
    user_priority = {
        "CRITICAL": "P0", "ACTION": "P1", "IMPORTANT": "P2",
        "INFO": "P3", "DEBUG": "P4",
    }.get(level, "P4")
    if event_type == "CANDLE_FINALIZED":
        return {"eligible": False, "reasonCode": "SKIP_LOW_PRIORITY",
                "priority": "DEBUG"}
    if event_type in {"NO_TRADE", "WAIT"}:
        return {"eligible": False, "reasonCode": "SKIP_SAME_STATE",
                "priority": "DEBUG"}
    if level == "DEBUG":
        return {"eligible": False, "reasonCode": "SKIP_LOW_PRIORITY",
                "priority": level}
    return {"eligible": True, "reasonCode": f"SEND_{level}_EVENT",
            "priority": level, "userPriority": user_priority}


def actionability_ttl_seconds(event_type: str) -> int | None:
    if event_type in {"ENTRY_NOW", "ENTRY_READY"}:
        return 5 * 60
    if event_type in {
        "WAIT_RETEST", "DOUBLE_SWEEP_CONFIRMED", "DATA_RECOVERED",
        "FAKE_BREAKOUT_CONFIRMED", "OPPOSITE_SETUP_CONFIRMED",
    }:
        return 30 * 60
    if event_type in {
        "STOP_TRIGGERED", "POSITION_EXIT", "POSITION_DEFEND",
        "SOFT_INVALIDATED", "HARD_INVALIDATED",
    }:
        return None
    return 60 * 60


def is_expired(payload: dict, *, now: datetime | None = None) -> bool:
    ttl = actionability_ttl_seconds(str(payload.get("event_type") or ""))
    if ttl is None:
        return False
    raw = str(payload.get("generatedAtUtc") or payload.get("calculatedAt") or "")
    if not raw:
        return True
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    current = now or datetime.now(timezone.utc)
    return (current - created).total_seconds() > ttl


def canonical_dedupe_key(payload: dict) -> str:
    # Delegate to the meaningful market fingerprint. It deliberately excludes
    # quote/calculation time, but includes action-changing prices and state.
    from app.services.alert_aggregator import notification_fingerprint
    return notification_fingerprint(payload)
