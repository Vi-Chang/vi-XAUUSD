"""Merge rule facts into one user-facing decision alert per evaluation group."""
from __future__ import annotations

import hashlib
import math

from app.config import get_settings

ALERT_PRIORITY = {
    "SCENARIO_INVALIDATED": 110, "EXIT_WARNING": 100, "ENTRY_READY": 80,
    "WAIT_RETEST": 70, "SETUP_CONFIRMED": 60, "MISSED_ENTRY": 50,
    "PULLBACK_ZONE_CREATED": 40, "MEANINGFUL_SCENARIO_UPDATE": 30,
    "WATCHING": 20, "WAIT": 10,
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
    }


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
    scenario_id = (setup.get("setupId") or wrapper.get("setupId")
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
    # A setup's source candle is immutable. Do not use the latest polling candle,
    # otherwise an unchanged WAIT becomes a new alert every 15 minutes.
    source_time = (setup.get("confirmedCandleTime") or setup.get("createdFromCandleTime")
                   or event.get("sourceDataTime")
                   or event.get("decisionBasisCandleCloseTime")
                   or event.get("candleCloseTime") or "")
    parts = {
        "symbol": str(event.get("symbol") or "XAUUSD"),
        "scenarioId": str(scenario_id),
        "direction": str(setup.get("direction") or event.get("direction") or "NONE"),
        "status": str(status),
        "triggerPrice": _price(trigger),
        "chaseLimit": _price(chase),
        "invalidationPrice": _price(invalidation),
        "sourceCandleTime": str(source_time),
    }
    if pullback_zone:
        parts["pullbackZone"] = pullback_zone
    return parts


def notification_fingerprint(event: dict) -> str:
    parts = notification_fingerprint_parts(event)
    settings = get_settings()
    steps = {
        "triggerPrice": settings.telegram_trigger_change_min_delta,
        "chaseLimit": settings.telegram_chase_change_min_delta,
        "invalidationPrice": settings.telegram_invalidation_change_min_delta,
    }
    stable = {**parts, "eventType": str(event.get("event_type") or "DECISION_UPDATED")}
    for field, step in steps.items():
        value = _number(parts.get(field))
        if value is not None and step > 0:
            stable[field] = f"{math.floor(value / step) * step:.2f}"
    if parts.get("pullbackZone"):
        values = str(parts["pullbackZone"]).split("-")
        step = settings.telegram_entry_zone_change_min_delta
        stable["pullbackZone"] = "-".join(
            f"{math.floor(float(value) / step) * step:.2f}" for value in values)
    raw = "|".join(stable.values())
    return hashlib.sha256(raw.encode()).hexdigest()


def alert_category(event: dict) -> str:
    event_type = str(event.get("event_type") or "")
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
        cycle = str(enriched.get("evaluationCycleId") or
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
        if any(f.get("event_type") == "STATE_CHANGED" for f in facts):
            representative["event_type"] = "STATE_CHANGED"
        aggregated.append(representative)
    return aggregated
