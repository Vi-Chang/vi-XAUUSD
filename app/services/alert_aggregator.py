"""Merge rule facts into one user-facing decision alert per evaluation group."""
from __future__ import annotations

import hashlib
import math

from app.config import get_settings
from app.services.semantic_decision import build_semantic_decision

ALERT_PRIORITY = {
    "CANDLE_CLOSE_REPORT": 150,
    "POSITION_EMERGENCY": 140, "SCENARIO_INVALIDATED": 135,
    "ENTRY_READY": 130, "PROBE_READY": 125, "EXIT_WARNING": 120,
    "PREPARE": 90, "WATCHING": 80,
    "MISSED_ENTRY": 70, "DATA_STATUS": 60, "WAIT_RETEST": 50,
    "SETUP_CONFIRMED": 45, "PULLBACK_ZONE_CREATED": 40,
    "MEANINGFUL_SCENARIO_UPDATE": 30, "WAIT": 10,
}


def _price(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _profile(event: dict) -> dict:
    wrapper = event.get("breakoutSetupEvent") or event.get("trendContinuationEvent") or {}
    setup = wrapper.get("setup") or {}
    zone = event.get("entryZone") or {}
    return {
        "status": str(setup.get("status") or wrapper.get("currentState")
                      or event.get("currentState") or "WAIT"),
        "setupId": str(setup.get("setupId") or wrapper.get("setupId")
                       or event.get("setupId") or ""),
        "eventType": str(event.get("event_type") or ""),
        "direction": str(setup.get("direction") or event.get("direction") or "NONE"),
        "trigger": _number(setup.get("breakoutTrigger") if setup.get("breakoutTrigger") is not None
                           else event.get("triggerLevel")),
        "chase": _number(setup.get("maxChasePrice") if setup.get("maxChasePrice") is not None
                         else event.get("chaseLimit")),
        "invalidation": _number(setup.get("stopPrice") if setup.get("stopPrice") is not None
                                else event.get("stopLoss")),
        "entryLow": _number(setup.get("entryZoneLow") if setup.get("entryZoneLow") is not None
                            else zone.get("low")),
        "entryHigh": _number(setup.get("entryZoneHigh") if setup.get("entryZoneHigh") is not None
                             else zone.get("high")),
        "pullbackLow": _number(setup.get("pullbackEntryZoneLow")),
        "pullbackHigh": _number(setup.get("pullbackEntryZoneHigh")),
        "atr": max(_number(setup.get("atr15")) or _number(event.get("atr15")) or 0.0, 0.0),
        "marketBias": str(event.get("marketBias") or
                          (event.get("canonicalDecision") or {}).get("marketBias") or "NEUTRAL"),
        "liveBiasState": str(event.get("liveBiasState") or
                             (event.get("canonicalDecision") or {}).get(
                                 "liveBiasState") or "ALIGNED"),
        "executionBias": str(event.get("executionBias") or
                             (event.get("canonicalDecision") or {}).get(
                                 "executionBias") or "NEUTRAL"),
        "entryConfirmation": str(event.get("entryConfirmation") or
                                 (event.get("canonicalDecision") or {}).get(
                                     "entryConfirmation") or ""),
        "defenseState": str(event.get("defenseState") or
                            (event.get("canonicalDecision") or {}).get("defenseState") or ""),
        "dataHealth": str(event.get("dataHealth") or
                          (event.get("canonicalDecision") or {}).get("dataHealth") or ""),
        "scenarioValidity": str(event.get("scenarioValidity") or
                                 (event.get("canonicalDecision") or {}).get(
                                     "scenarioValidity") or ""),
        "scenarioState": str(event.get("scenarioState") or
                              (event.get("canonicalDecision") or {}).get(
                                  "scenarioState") or ""),
        "primaryTriggerId": str(event.get("primaryTriggerId") or
                                (event.get("canonicalDecision") or {}).get(
                                    "activeSetupId") or ""),
    }


def notification_state_regression(previous: dict | None,
                                  current: dict) -> tuple[bool, str]:
    """Block an earlier lifecycle snapshot for the same scenario.

    A new scenario id starts a new lifecycle.  Within one scenario, an already
    notified confirmed break / invalidation can never be followed by a pending
    defense, reclaim or held notification.
    """
    if not previous:
        return False, "NO_PREVIOUS_NOTIFICATION"
    old, new = _profile(previous), _profile(current)
    if not old["setupId"] or not new["setupId"] or old["setupId"] != new["setupId"]:
        return False, "DIFFERENT_SCENARIO"

    def terminal(profile: dict) -> bool:
        return (profile["scenarioState"] in {"INVALIDATED", "SCENARIO_INVALIDATED"}
                or profile["scenarioValidity"] == "INVALIDATED"
                or profile["status"] in {"INVALIDATED", "SCENARIO_INVALIDATED"})

    old_defense, new_defense = old["defenseState"], new["defenseState"]
    if terminal(old) and not terminal(new):
        return True, "STATE_REGRESSION_BLOCKED"
    if old_defense == "BROKEN_CONFIRMED" and new_defense != "BROKEN_CONFIRMED":
        return True, "STATE_REGRESSION_BLOCKED"
    return False, "MONOTONIC_OR_NEW_EVENT"


def is_meaningful_change(previous: dict | None, current: dict) -> tuple[bool, str]:
    """Quote, distance, timestamps and tiny recalculations are never decisions."""
    if not previous:
        return True, "FIRST_NOTIFICATION"
    old, new = _profile(previous), _profile(current)
    if old["status"] != new["status"]:
        return True, "STATUS_CHANGED"
    if old["direction"] != new["direction"]:
        return True, "DIRECTION_CHANGED"
    if old["setupId"] != new["setupId"]:
        return True, "NEW_SCENARIO"
    for field in (
            "marketBias", "liveBiasState", "executionBias",
            "entryConfirmation", "defenseState", "dataHealth",
            "scenarioValidity",
            "primaryTriggerId"):
        if old[field] != new[field]:
            return True, f"{field.upper()}_CHANGED"
    category = alert_category(current)
    if old["eventType"] != new["eventType"] and category in {
            "ENTRY_READY", "EXIT_WARNING", "MISSED_ENTRY",
            "SCENARIO_INVALIDATED", "PULLBACK_ZONE_CREATED"}:
        return True, category
    settings = get_settings()
    atr = max(old["atr"], new["atr"])
    checks = (
        ("trigger", settings.telegram_trigger_change_atr_ratio,
         settings.telegram_trigger_change_min_delta),
        ("entryLow", settings.telegram_entry_zone_change_atr_ratio,
         settings.telegram_entry_zone_change_min_delta),
        ("entryHigh", settings.telegram_entry_zone_change_atr_ratio,
         settings.telegram_entry_zone_change_min_delta),
        ("invalidation", settings.telegram_invalidation_change_atr_ratio,
         settings.telegram_invalidation_change_min_delta),
        ("chase", settings.telegram_chase_change_atr_ratio,
         settings.telegram_chase_change_min_delta),
    )
    for field, ratio, minimum in checks:
        before, after = old[field], new[field]
        if before is None and after is not None:
            return True, f"{field.upper()}_CREATED"
        if before is not None and after is None:
            return True, f"{field.upper()}_REMOVED"
        if (before is not None and after is not None
                and abs(after - before) >= max(atr * ratio, minimum)):
            return True, f"{field.upper()}_CHANGED"
    old_pullback = (old["pullbackLow"], old["pullbackHigh"])
    new_pullback = (new["pullbackLow"], new["pullbackHigh"])
    if old_pullback == (None, None) and new_pullback != (None, None):
        return True, "PULLBACK_ZONE_CREATED"
    threshold = max(atr * settings.telegram_entry_zone_change_atr_ratio,
                    settings.telegram_entry_zone_change_min_delta)
    for before, after in zip(old_pullback, new_pullback, strict=True):
        if before is not None and after is not None and abs(after - before) >= threshold:
            return True, "PULLBACK_ZONE_CHANGED"
    return False, "NO_MEANINGFUL_DECISION_CHANGE"


def notification_fingerprint_parts(event: dict) -> dict[str, str]:
    """Stable decision identity; excludes quote time, price, text and dataVersion."""
    wrapper = event.get("breakoutSetupEvent") or event.get("trendContinuationEvent") or {}
    setup = wrapper.get("setup") or {}
    scenario_id = (event.get("opportunityId") or setup.get("setupId") or wrapper.get("setupId")
                   or event.get("scenario_id") or event.get("setupId") or "")
    status = (setup.get("status") or wrapper.get("currentState")
              or event.get("currentState") or "WAIT")
    trigger = (setup.get("breakoutTrigger") if setup.get("breakoutTrigger") is not None
               else event.get("triggerLevel"))
    chase = (setup.get("maxChasePrice") if setup.get("maxChasePrice") is not None
             else event.get("chaseLimit"))
    invalidation = (setup.get("stopPrice") if setup.get("stopPrice") is not None
                    else event.get("stopLoss"))
    pullback_zone = "-".join(filter(None, (
        _price(setup.get("pullbackEntryZoneLow")),
        _price(setup.get("pullbackEntryZoneHigh")),
    )))
    event_type = str(event.get("event_type") or "")
    candle_scoped_events = {
        "DEFENSE_BROKEN_CONFIRMED", "BREAKOUT_CONFIRMED", "BREAK_CONFIRMED",
        "RECLAIM_CONFIRMED", "DEFENSE_RECLAIMED", "DEFENSE_HELD",
        "ENTRY_READY", "ENTRY_NOW",
        "LONG_WATCH", "SHORT_WATCH", "LONG_RESTORED", "SHORT_RESTORED",
    }
    # Candle time belongs to the identity only when that exact event requires a
    # closed candle. A newer context candle must not turn an unchanged WAIT,
    # defense test or retest wait into a new notification.
    requires_closed_candle = (
        event_type in candle_scoped_events or event_type.startswith("ENTRY_READY")
    )
    source_time = ""
    if requires_closed_candle:
        source_time = (setup.get("confirmedCandleTime")
                       or event.get("decisionBasisCandleCloseTime")
                       or event.get("closedBarTimestamp")
                       or event.get("candleCloseTime")
                       or setup.get("createdFromCandleTime")
                       or event.get("sourceDataTime") or "")
    semantic = build_semantic_decision(event)
    parts = {
        "symbol": str(event.get("symbol") or "XAUUSD"),
        "scenarioId": str(scenario_id),
        "direction": str(setup.get("direction") or event.get("direction") or "NONE"),
        "status": str(status),
        "triggerPrice": _price(trigger),
        "chaseLimit": _price(chase),
        "invalidationPrice": _price(invalidation),
        "sourceCandleTime": str(source_time),
        "canonicalState": str(event.get("canonicalState") or
                              event.get("currentState") or status),
        "marketBias": str(event.get("marketBias") or
                          (event.get("canonicalDecision") or {}).get("marketBias") or "NEUTRAL"),
        "liveBiasState": str(semantic["liveBiasState"]),
        "executionBias": str(semantic["executionBias"]),
        "entryConfirmation": str(event.get("entryConfirmation") or
                                 (event.get("canonicalDecision") or {}).get(
                                     "entryConfirmation") or ""),
        "defenseState": str(event.get("defenseState") or
                            (event.get("canonicalDecision") or {}).get("defenseState") or ""),
        "dataHealth": str(event.get("dataHealth") or
                          (event.get("canonicalDecision") or {}).get("dataHealth") or ""),
        "primaryTriggerId": str(event.get("primaryTriggerId") or
                                (event.get("canonicalDecision") or {}).get(
                                    "activeSetupId") or scenario_id),
        "canonicalAction": str(semantic["canonicalAction"]),
        "shortTermBias": str(semantic["shortTermBias"]),
        "bias15m": str(semantic["bias15m"]),
        "bias1h": str(semantic["bias1h"]),
        "bias4h": str(semantic["bias4h"]),
        "triggerStatus": str(semantic["triggerStatus"]),
        "entryZoneState": str(semantic["entryZoneState"]),
        "rrState": str(semantic["rrState"]),
        "positionState": str(semantic["positionState"]),
        "strategyPhase": str(semantic["strategyPhase"]),
        "waitReason": str(semantic["waitReason"]),
        "targetCloseTime": str(((event.get("canonicalDecision") or {}).get(
            "closeGate") or event.get("closeGate") or {}).get(
                "targetCandleCloseTime") or ""),
    }
    if event_type in {"DATA_DELAYED", "DATA_STALE", "DATA_RECOVERED"}:
        # Freshness alerts are transitions, not candle-scoped market setups.
        incident = str(event.get("dataIncidentId") or "DATA_HEALTH")
        parts["scenarioId"] = incident
        parts["primaryTriggerId"] = str(
            event.get("dataHealthEventKey") or f"{event_type}:{incident}")
        parts["sourceCandleTime"] = ""
    scenario_validity = str(event.get("scenarioValidity") or
                            (event.get("canonicalDecision") or {}).get(
                                "scenarioValidity") or "")
    if scenario_validity:
        parts["scenarioValidity"] = scenario_validity
    if pullback_zone:
        parts["pullbackZone"] = pullback_zone
    return parts


def notification_fingerprint(event: dict) -> str:
    if str(event.get("event_type") or "") in {
            "CANDLE_CLOSE_ANALYSIS_15M", "CANDLE_CLOSE_ANALYSIS_1H",
            "CANDLE_CLOSE_ANALYSIS_COMBINED"}:
        identity = str(event.get("reportDedupeKey") or event.get("eventKey") or "")
        return hashlib.sha256(identity.encode()).hexdigest()
    parts = notification_fingerprint_parts(event)
    settings = get_settings()
    steps = {
        "triggerPrice": settings.telegram_trigger_change_min_delta,
        "chaseLimit": settings.telegram_chase_change_min_delta,
        "invalidationPrice": settings.telegram_invalidation_change_min_delta,
    }
    stable = dict(parts)
    wrapper = event.get("breakoutSetupEvent") or event.get("trendContinuationEvent") or {}
    setup = wrapper.get("setup") or {}
    entry_zone = "-".join(filter(None, (
        _price(setup.get("entryZoneLow") if setup.get("entryZoneLow") is not None
               else (event.get("entryZone") or {}).get("low")),
        _price(setup.get("entryZoneHigh") if setup.get("entryZoneHigh") is not None
               else (event.get("entryZone") or {}).get("high")),
    )))
    if entry_zone:
        stable["entryZone"] = entry_zone
    for field, step in steps.items():
        value = _number(parts.get(field))
        if value is not None and step > 0:
            stable[field] = f"{math.floor(value / step) * step:.2f}"
    if parts.get("pullbackZone"):
        values = str(parts["pullbackZone"]).split("-")
        step = settings.telegram_entry_zone_change_min_delta
        stable["pullbackZone"] = "-".join(
            f"{math.floor(float(value) / step) * step:.2f}" for value in values)
    if stable.get("entryZone"):
        values = str(stable["entryZone"]).split("-")
        step = settings.telegram_entry_zone_change_min_delta
        stable["entryZone"] = "-".join(
            f"{math.floor(float(value) / step) * step:.2f}" for value in values)
    raw = "|".join(stable.values())
    return hashlib.sha256(raw.encode()).hexdigest()


def alert_category(event: dict) -> str:
    event_type = str(event.get("event_type") or "")
    if event_type in {"CANDLE_CLOSE_ANALYSIS_15M", "CANDLE_CLOSE_ANALYSIS_1H",
                      "CANDLE_CLOSE_ANALYSIS_COMBINED"}:
        return "CANDLE_CLOSE_REPORT"
    if event_type in {"TRUE_ENGINE_CONFLICT", "SYSTEM_STATE_INVARIANT_BLOCKED"}:
        return "POSITION_EMERGENCY"
    if event_type in {"TIMEFRAME_DIVERGENCE", "BIAS_TRANSITION", "SCORE_NEAR_TIE"}:
        return "MEANINGFUL_SCENARIO_UPDATE"
    if event_type in {"ENTRY_READY", "ENTRY_NOW"}:
        return "ENTRY_READY"
    if event_type == "PROBE_READY":
        return "PROBE_READY"
    if event_type == "EARLY_ENTRY_PREPARE":
        return "PREPARE"
    if event_type == "EARLY_ENTRY_REPLACED":
        return "PREPARE"
    if event_type == "EARLY_ENTRY_WATCH":
        return "WATCHING"
    if event_type == "EARLY_ENTRY_MISSED":
        return "MISSED_ENTRY"
    if event_type == "EARLY_ENTRY_INVALIDATED":
        return "SCENARIO_INVALIDATED"
    if event_type in {
        "FAKE_BREAKOUT_CONFIRMED", "OPPOSITE_SETUP_CONFIRMED",
        "RECOVERY_SETUP_INVALIDATED",
    }:
        return "MEANINGFUL_SCENARIO_UPDATE"
    if event_type in {"DOUBLE_SWEEP_CONFIRMED", "DOUBLE_SWEEP_EDGE_CONSUMED"}:
        return "MEANINGFUL_SCENARIO_UPDATE"
    if event.get("tradePlanId"):
        return "EXIT_WARNING"
    state = str(event.get("currentState") or "WAIT")
    if state == "DATA_STALE":
        return "DATA_STATUS"
    actionable = (bool(event.get("canEnter")) and
                  str(event.get("finalAction") or "") in {"ENTER_LONG", "ENTER_SHORT"})
    if (state.endswith("READY") or state.startswith("ENTRY_READY_")) and actionable:
        return "ENTRY_READY"
    if state.endswith("READY") or state.startswith("ENTRY_READY_"):
        return "SETUP_CONFIRMED"
    if state.endswith("MANAGE") or event_type in {
            "TAKE_PROFIT_1", "TAKE_PROFIT_2", "TAKE_PROFIT_3", "EARLY_EXIT",
            "STOP_TRIGGERED", "STRUCTURE_INVALIDATED", "EXIT_NOW", "EXIT_ZONE_REACHED"}:
        return "EXIT_WARNING"
    if state in {"MISSED_ENTRY", "MISS_ENTRY"}:
        return "MISSED_ENTRY"
    if state in {"EXPIRED", "SETUP_EXPIRED", "INVALIDATED", "PULLBACK_INVALIDATED"}:
        return "SCENARIO_INVALIDATED"
    if state == "WAIT_RETEST":
        return "WAIT_RETEST"
    if state in {"SETUP_CONFIRMED", "BREAKOUT_CONFIRMED"}:
        return "SETUP_CONFIRMED"
    if event_type == "PULLBACK_ZONE_UPDATED":
        return "PULLBACK_ZONE_CREATED"
    if state.endswith("WATCH"):
        return "WATCHING"
    if state.startswith("WAIT"):
        return "WAIT"
    return "MEANINGFUL_SCENARIO_UPDATE"


def semantic_key(event: dict) -> str:
    if event.get("trendContinuationEvent") or event.get("breakoutSetupEvent"):
        return notification_fingerprint(event)
    if event.get("tradePlanId"):
        raw = "|".join((
            str(event.get("tradePlanId")), str(event.get("event_type")),
            str(event.get("targetIndex") or 0),
        ))
        return hashlib.sha256(raw.encode()).hexdigest()
    if any(key in event for key in (
            "canonicalDecision", "marketBias", "entryConfirmation", "defenseState")):
        return notification_fingerprint(event)
    raw = "|".join((
        str(event.get("symbol") or "XAUUSD"),
        str(event.get("timeframe") or "15M"),
        str(event.get("decisionBasisCandleCloseTime") or event.get("candleCloseTime") or ""),
        str(event.get("setupId") or ""),
        str(event.get("direction") or "NONE"),
        str(event.get("currentState") or "WAIT"),
        str(event.get("event_type") or "DECISION_UPDATED"),
        str(event.get("alertCategory") or alert_category(event)),
        str(event.get("triggerLevel") or ""),
    ))
    return hashlib.sha256(raw.encode()).hexdigest()


def aggregate_signal_facts(symbol: str, events: list[dict]) -> list[dict]:
    """Return one FinalDecision alert for each semantic group.

    Raw events remain available as ``signalFacts`` for audit and UI evidence.
    """
    # A single evaluation may invalidate an old scenario and create a new one.
    # Users need one consolidated action, not a burst of lifecycle messages.
    cycles: dict[str, list[dict]] = {}
    for event in events:
        enriched = {**event, "symbol": symbol,
                    "timeframe": str(event.get("timeframe") or "15M")}
        enriched["alertCategory"] = alert_category(enriched)
        cycle = str(enriched.get("evaluationCycleId") or enriched.get("snapshotId") or
                    f"{enriched.get('dataVersion', 0)}:{enriched.get('calculatedAt', '')}")
        cycles.setdefault(cycle, []).append(enriched)
    aggregated = []
    for cycle, facts in cycles.items():
        representative = dict(max(
            enumerate(facts),
            key=lambda pair: (ALERT_PRIORITY.get(alert_category(pair[1]), 15), pair[0]),
        )[1])
        # When an old setup expires and a new WAIT setup is created in the same
        # cycle, the message is an update but its durable fingerprint belongs
        # to the new active setup. This prevents the next scheduler poll from
        # announcing that same WAIT setup again.
        wait_basis = next((fact for fact in reversed(facts)
                           if alert_category(fact) == "WAIT"), None)
        key_basis = (wait_basis if alert_category(representative) == "SCENARIO_INVALIDATED"
                     and wait_basis else representative)
        key = semantic_key(key_basis)
        reasons = list(dict.fromkeys(
            str(f.get("transitionReason") or f.get("triggerReason") or "")
            for f in facts if f.get("transitionReason") or f.get("triggerReason")))
        event_id = (str(representative.get("eventId")) if len(facts) == 1
                    else hashlib.sha256(f"{key}|{cycle}".encode()).hexdigest()[:32])
        representative.update({
            "eventId": event_id,
            "evaluationCycleId": cycle,
            "semanticDedupKey": key,
            "signalFacts": facts,
            "factCount": len(facts),
            "transitionReasons": reasons,
            "transitionReason": "；".join(reasons),
            "triggerReason": "；".join(reasons),
            "topic": f"decision-alert:{key}",
        })
        # STATE_CHANGED is an audit fact, not a priority override.  Previously
        # it could rename ENTRY_READY / EXIT / defense notifications and make
        # the formatter and eligibility layer treat them as a generic update.
        if (any(f.get("event_type") == "STATE_CHANGED" for f in facts)
                and alert_category(representative) in {
                    "WAIT", "MEANINGFUL_SCENARIO_UPDATE"}):
            representative["event_type"] = "STATE_CHANGED"
        aggregated.append(representative)
    return aggregated
