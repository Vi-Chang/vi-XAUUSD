"""Merge rule facts into one user-facing decision alert per evaluation group."""
from __future__ import annotations

import hashlib


def alert_category(event: dict) -> str:
    if event.get("tradePlanId"):
        return str(event.get("event_type") or "POSITION_MANAGEMENT")
    state = str(event.get("currentState") or "WAIT")
    if state == "DATA_STALE":
        return "DATA_STATUS"
    if state.endswith("READY"):
        return "ENTRY_READY"
    if state.endswith("MANAGE"):
        return "POSITION_MANAGEMENT"
    return state


def semantic_key(event: dict) -> str:
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
    groups: dict[str, list[dict]] = {}
    for event in events:
        enriched = {**event, "symbol": symbol,
                    "timeframe": str(event.get("timeframe") or "15M")}
        enriched["alertCategory"] = alert_category(enriched)
        key = semantic_key(enriched)
        groups.setdefault(key, []).append(enriched)
    aggregated = []
    for key, facts in groups.items():
        representative = dict(facts[-1])
        reasons = list(dict.fromkeys(
            str(f.get("transitionReason") or f.get("triggerReason") or "")
            for f in facts if f.get("transitionReason") or f.get("triggerReason")))
        cycle = str(representative.get("evaluationCycleId") or
                    f"{representative.get('dataVersion', 0)}:{representative.get('calculatedAt', '')}")
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
