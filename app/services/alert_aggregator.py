"""Merge rule facts into one user-facing decision alert per evaluation group."""
from __future__ import annotations

import hashlib

ALERT_PRIORITY = {"ENTRY_READY": 50, "POSITION_MANAGEMENT": 40,
                  "MISSED_ENTRY": 30, "SCENARIO_UPDATED": 20, "WAIT": 10}


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
    if state in {"EXPIRED", "SETUP_EXPIRED", "INVALIDATED"}:
        return "SCENARIO_UPDATED"
    if state.endswith("WATCH") or state.startswith("WAIT"):
        return "WAIT"
    return state


def semantic_key(event: dict) -> str:
    if event.get("trendContinuationEvent"):
        setup = event.get("trendContinuationEvent", {}).get("setup") or {}
        raw = "|".join((str(event.get("symbol") or "XAUUSD"),
                        str(event.get("setupId") or ""), str(event.get("currentState") or ""),
                        str(setup.get("type") or ""), str(event.get("event_type") or "")))
        return hashlib.sha256(raw.encode()).hexdigest()
    if event.get("breakoutSetupEvent"):
        zone = event.get("entryZone") or {}
        raw = "|".join((
            str(event.get("symbol") or "XAUUSD"), str(event.get("setupId") or ""),
            str(event.get("direction") or "NONE"), str(event.get("currentState") or ""),
            str(event.get("triggerLevel") or ""),
            str(event.get("decisionBasisCandleCloseTime") or event.get("candleCloseTime") or ""),
            f"{zone.get('low', '')}:{zone.get('high', '')}",
            str(event.get("blockedReason") or ""), str(event.get("event_type") or ""),
        ))
        return hashlib.sha256(raw.encode()).hexdigest()
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
        key = semantic_key(representative)
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
