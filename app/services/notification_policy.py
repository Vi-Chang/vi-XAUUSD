"""Canonical event priority, eligibility, TTL and trace metadata."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import ClassVar

from app.engines.multi_timeframe_bias import derive_multi_timeframe_bias

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
    "EARLY_ENTRY_WATCH", "EARLY_ENTRY_REPLACED",
}
INFO = {"REGIME_MAJOR_CHANGE", "MARKET_CLOSED", "SETUP_CREATED", "SETUP_ARMED"}
WAIT_STATES = {"WAIT", "NO_TRADE", "WAIT_CONFIRMATION"}


def _canonical(payload: dict) -> dict:
    return dict(payload.get("canonicalDecision") or {})


def _critical_data_block(payload: dict) -> bool:
    canonical = _canonical(payload)
    state = str(payload.get("currentState") or canonical.get("setupState") or "")
    confirmation = str(payload.get("entryConfirmation") or
                       canonical.get("entryConfirmation") or "")
    active = (state in {"PREPARE", "ENTRY_READY", "LONG_READY", "SHORT_READY"}
              or bool(payload.get("positionId"))
              or bool(payload.get("activeSetupId") or canonical.get("activeSetupId")))
    event_type = str(payload.get("event_type") or "")
    return bool(payload.get("criticalDataBlock") or event_type == "DATA_STALE" or
                str(payload.get("currentState") or "") == "DATA_STALE" or
                (active and confirmation == "BLOCKED_BY_DATA"))


def is_user_actionable_notification(payload: dict) -> tuple[bool, str, str]:
    """Classify whether the event can change the user's next trading action."""
    event_type = str(payload.get("event_type") or "DECISION_UPDATED")
    state = str(payload.get("currentState") or "WAIT")
    if payload.get("notificationRoute") == "LOG_ONLY":
        return False, "LOG_ONLY_EVENT", "LOG_ONLY"
    if event_type in {"DELIVERY_UNKNOWN", "HEARTBEAT", "CANDLE_FINALIZED",
                      "OPPORTUNITY_COVERAGE_GAP"}:
        return False, "LOG_ONLY_EVENT", "LOG_ONLY"
    if event_type in {"DATA_DELAYED", "DATA_STALE"}:
        return ((_critical_data_block(payload), "CRITICAL_DATA_BLOCK", "P7")
                if _critical_data_block(payload) else
                (False, "LOW_PRIORITY", "LOG_ONLY"))
    if event_type == "DATA_RECOVERED":
        relevant = bool(payload.get("recoveryRelevant") or payload.get("positionId") or
                        payload.get("activeSetupId") or _canonical(payload).get("activeSetupId"))
        return ((True, "ACTIONABLE_DATA_RECOVERY", "P8") if relevant else
                (False, "LOW_PRIORITY", "LOG_ONLY"))
    if event_type in CRITICAL or event_type in {
            "EXIT_NOW", "EXIT_ZONE_REACHED", "EARLY_EXIT",
            "STOP_TRIGGERED", "POSITION_DEFEND", "TP1_HIT",
            "TP2_HIT", "TP3_HIT", "TAKE_PROFIT_1", "TAKE_PROFIT_2",
            "TAKE_PROFIT_3", "TRAILING_STOP_UPDATE"}:
        return True, "POSITION_ACTION", "P0"
    if event_type in {"ENTRY_READY", "ENTRY_NOW"} or (
            (state.endswith("READY") or state.startswith("ENTRY_READY_")) and
            bool(payload.get("canEnter", True))):
        return True, "ENTRY_PERMISSION_CHANGED", "P1"
    if event_type in {"SETUP_INVALIDATED", "ENTRY_INVALIDATED",
                      "EARLY_ENTRY_INVALIDATED", "RECOVERY_SETUP_INVALIDATED",
                      "DEFENSE_BROKEN_CONFIRMED"} or state in {
            "INVALIDATED", "SETUP_INVALIDATED", "PULLBACK_INVALIDATED",
            "EXPIRED", "SETUP_EXPIRED"}:
        return True, "ACTIVE_SETUP_INVALIDATED", "P2"
    if event_type in {"EARLY_ENTRY_PREPARE", "EARLY_ENTRY_REPLACED"}:
        return True, "PREPARE_ACTION", "P3"
    if event_type in {"EARLY_ENTRY_WATCH", "ENTRY_APPROACHING",
                      "RETRACE_APPROACHING", "RETRACE_ZONE_ENTERED"}:
        return True, "WATCH_ACTION", "P4"
    if event_type in {"MISSED_ENTRY", "EARLY_ENTRY_MISSED", "PRICE_RAN_AWAY"}:
        return True, "MISSED_ACTION", "P5"
    if event_type in {"BIAS_CHANGE", "REGIME_MAJOR_CHANGE", "BULLISH_RECOVERY",
                      "BEARISH_RECOVERY", "FALSE_BREAKOUT"} or bool(
            payload.get("marketBiasChanged")):
        return True, "MATERIAL_BIAS_CHANGE", "P6"
    wrapper = payload.get("breakoutSetupEvent") or payload.get("trendContinuationEvent") or {}
    setup = wrapper.get("setup") or {}
    has_actionable_watch = bool(
        (setup.get("setupId") or wrapper.get("setupId") or payload.get("setupId")) and
        (setup.get("breakoutTrigger") is not None or payload.get("triggerLevel") is not None))
    if state.startswith("WAIT") and has_actionable_watch:
        return True, "WATCH_ACTION", "P4"
    if event_type in {"NO_TRADE", "WAIT", "DECISION_UPDATED"}:
        return False, "NO_ACTION_DELTA", "LOG_ONLY"
    # Existing lifecycle events remain eligible when they already have a
    # defined user action. The meaningful-change gate still suppresses repeats.
    if event_type in ACTION or event_type in IMPORTANT or event_type in INFO:
        return True, "ACTIONABLE_LIFECYCLE_EVENT", "P4"
    return False, "LOW_PRIORITY", "LOG_ONLY"


def user_visible_state_fingerprint(payload: dict) -> str:
    """Identity made only from fields that can alter the user's next action."""
    canonical = _canonical(payload)
    multi = payload.get("multiTimeframeBias") or canonical.get("multiTimeframeBias")
    if not multi:
        multi = derive_multi_timeframe_bias(
            payload.get("normalized_analysis") or canonical or payload,
            canonical_bias=str(payload.get("marketBias") or
                               canonical.get("marketBias") or "NEUTRAL"))
    entry = canonical.get("newEntryDecision") or {}
    selected = entry.get("selectedSetup") or {}
    position = canonical.get("positionManagement") or {}
    event_type = str(payload.get("event_type") or "")
    position_event_action = (event_type if event_type in {
        "EXIT_NOW", "EXIT_ZONE_REACHED", "EARLY_EXIT", "STOP_TRIGGERED",
        "POSITION_DEFEND", "TP1_HIT", "TP2_HIT", "TP3_HIT",
        "TAKE_PROFIT_1", "TAKE_PROFIT_2", "TAKE_PROFIT_3",
        "TRAILING_STOP_UPDATE"} else None)
    visible = {
        "shortTermBias": multi.get("shortTermBias"),
        "macroBias": multi.get("macroBias"),
        "opportunityState": payload.get("currentState") or canonical.get("setupState"),
        "entryPermission": payload.get("canEnter", entry.get("canEnter")),
        "entryZone": payload.get("entryZone") or selected.get("entryZone"),
        "invalidation": payload.get("stopLoss") or selected.get("tacticalStop"),
        "positionAction": position.get("action") or position_event_action,
        "criticalDataBlock": _critical_data_block(payload),
    }
    raw = json.dumps(visible, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def has_meaningful_action_delta(previous: dict | None,
                                current: dict) -> tuple[bool, str]:
    actionable, reason, _ = is_user_actionable_notification(current)
    if not actionable:
        return False, reason
    if previous is None:
        return True, reason
    before = user_visible_state_fingerprint(previous)
    after = user_visible_state_fingerprint(current)
    if before == after:
        return False, "NO_ACTION_DELTA"
    return True, reason


class NotificationBudget:
    """Priority contract used by transports; safety events bypass cooldown."""

    BYPASS_COOLDOWN: ClassVar[set[str]] = {"P0", "P1"}

    @classmethod
    def bypass_cooldown(cls, user_priority: str) -> bool:
        return user_priority in cls.BYPASS_COOLDOWN


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
    if event_type == "CANDLE_FINALIZED":
        return {"eligible": False, "reasonCode": "SKIP_LOW_PRIORITY",
                "priority": "DEBUG"}
    level = priority(event_type, payload)
    user_priority = {
        "CRITICAL": "P0", "ACTION": "P1", "IMPORTANT": "P2",
        "INFO": "P3", "DEBUG": "P4",
    }.get(level, "P4")
    actionable, action_reason, action_priority = is_user_actionable_notification(payload)
    if not actionable:
        return {"eligible": False, "reasonCode": action_reason,
                "priority": "DEBUG", "userPriority": "LOG_ONLY"}
    resolved_priority = action_priority if action_priority != "LOG_ONLY" else user_priority
    return {"eligible": True, "reasonCode": action_reason,
            "priority": level, "userPriority": resolved_priority,
            "bypassCooldown": NotificationBudget.bypass_cooldown(resolved_priority)}


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
