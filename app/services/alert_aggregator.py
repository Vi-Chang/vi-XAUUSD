"""Merge rule facts into one user-facing decision alert per evaluation group."""
from __future__ import annotations

import hashlib

ALERT_PRIORITY = {"ENTRY_READY": 50, "POSITION_MANAGEMENT": 40,
                  "MISSED_ENTRY": 30, "SCENARIO_UPDATED": 20, "WAIT": 10}


def _price(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


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
    raw = "|".join(parts.values())
    return hashlib.sha256(raw.encode()).hexdigest()


def alert_category(event: dict) -> str:
    if event.get("tradePlanId"):
        return str(event.get("event_type") or "POSITION_MANAGEMENT")
    state = str(event.get("currentState") or "WAIT")
    if state == "DATA_STALE":
        return "DATA_STATUS"
    if state.endswith("READY") or state.startswith("ENTRY_READY_"):
        return "ENTRY_READY"
    if state.endswith("MANAGE"):
        return "POSITION_MANAGEMENT"
    if state in {"MISSED_ENTRY", "MISS_ENTRY"}:
        return "MISSED_ENTRY"
    if state in {"EXPIRED", "SETUP_EXPIRED", "INVALIDATED", "PULLBACK_INVALIDATED"}:
        return "SCENARIO_UPDATED"
    if state.endswith("WATCH") or state.startswith("WAIT"):
        return "WAIT"
    return state


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
        key_basis = (wait_basis if alert_category(representative) == "SCENARIO_UPDATED"
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
