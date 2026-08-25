"""Audit missed opportunity coverage without inventing retrospective entries."""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def evaluate_opportunity_coverage(data: dict, early_state: dict,
                                  previous: dict | None = None) -> tuple[dict, list[dict]]:
    previous = previous or {"sides": {}}
    normalized = data.get("normalized_analysis") or {}
    price = _number(normalized.get("currentPrice")) or 0.0
    atr = max(_number(normalized.get("atr15")) or 0.01, 0.01)
    now = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    sides, events = {}, []
    candidates = early_state.get("candidates") or {}
    previous_sides = previous.get("sides") or {}
    for side in ("LONG", "SHORT"):
        candidate = candidates.get(side) or {}
        old = dict(previous_sides.get(side) or {})
        zone = candidate.get("candidateZone") or old.get("zone") or {}
        low, high = zone.get("low"), zone.get("high")
        low_number, high_number = _number(low), _number(high)
        touched = (low_number is not None and high_number is not None
                   and low_number <= price <= high_number)
        stage = str(candidate.get("state") or "IDLE")
        if touched:
            old.update({"touchPrice": price, "touchAt": now,
                        "zone": {"low": low_number, "high": high_number},
                        "coveredAtTouch": stage.startswith(("WATCH", "PREPARE"))})
        touch_price = old.get("touchPrice")
        numeric_touch = _number(touch_price)
        favorable = ((price - numeric_touch) if side == "LONG"
                     else (numeric_touch - price)) if numeric_touch is not None else 0.0
        gap = (favorable >= atr * get_settings().opportunity_coverage_favorable_atr_mult
               and not old.get("coveredAtTouch") and not old.get("gapLogged"))
        if gap:
            old["gapLogged"] = True
            events.append({
                "event_type": "OPPORTUNITY_COVERAGE_GAP", "symbol": data.get("symbol") or "XAUUSD",
                "direction": side, "touchAt": old.get("touchAt"), "touchPrice": touch_price,
                "currentPrice": price, "favorableMove": round(favorable, 3),
                "atr15": atr, "rejectionReasons": candidate.get("rejectionReasons") or [],
                "notificationEligible": False, "notificationRoute": "LOG_ONLY",
                "calculatedAt": now,
            })
        old.update({"lastPrice": price, "lastStage": stage, "evaluatedAt": now})
        sides[side] = old
    return {"schemaVersion": "opportunity-coverage-v1", "sides": sides,
            "evaluatedAt": now, "events": events}, events
