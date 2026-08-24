"""Deterministic live facts. AI text must never overwrite these values."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from app.engines.freshness_state import evaluate_freshness_state
from app.utils.timeutils import iso_utc, parse_utc

ACTIVE_STATES = {
    "WAIT_BREAKOUT_CONFIRMATION", "BREAKOUT_CONFIRMED", "WAIT_RETEST",
    "ENTRY_READY_BREAKOUT", "ENTRY_READY_RETEST", "BREAKOUT_ENTRY_READY",
    "PULLBACK_ENTRY_READY", "WAIT_BREAKOUT_OR_PULLBACK",
    "WAIT_PULLBACK_CONFIRMATION",
}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _active_setup(data: dict) -> dict:
    setups = ((data.get("breakout_setup_manager") or {}).get("setups") or [])
    active = [s for s in setups if s.get("status") in ACTIVE_STATES]
    return dict(active[-1] if active else (setups[-1] if setups else {}))


def _zone_distance(price: float, low: float | None, high: float | None) -> float | None:
    if low is None or high is None:
        return None
    lo, hi = sorted((low, high))
    if lo <= price <= hi:
        return 0.0
    return round(lo - price if price < lo else price - hi, 4)


def _next_close(now: datetime, minutes: int = 15) -> datetime:
    boundary = now.replace(second=0, microsecond=0)
    boundary = boundary.replace(minute=(boundary.minute // minutes) * minutes)
    if boundary <= now:
        boundary += timedelta(minutes=minutes)
    return boundary


def _dedupe_scenarios(data: dict) -> list[dict]:
    setups = ((data.get("breakout_setup_manager") or {}).get("setups") or [])
    seen: set[tuple] = set()
    result: list[dict] = []
    for setup in reversed(setups):
        key = (
            data.get("symbol") or "XAUUSD", setup.get("direction"),
            setup.get("entryType") or setup.get("setupType") or "BREAKOUT",
            round(_num(setup.get("breakoutTrigger")) or 0.0, 2),
            setup.get("timeframe") or "15M", setup.get("signalGenerationId")
            or setup.get("setupId"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(setup))
    return list(reversed(result))


def validate_trade_prices(direction: str, entry: float | None, stop: float | None,
                          targets: list[float]) -> bool:
    if entry is None or stop is None or not targets:
        return False
    return (stop < entry < min(targets) if direction == "LONG"
            else max(targets) < entry < stop)


def build_realtime_presentation(data: dict, *, price: float | None = None,
                                quote_time: str | None = None,
                                now: datetime | None = None) -> dict:
    now_utc = parse_utc(now or datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    normalized = data.get("normalized_analysis") or {}
    current = _num(price if price is not None else normalized.get("currentPrice"))
    current = current if current is not None else _num((data.get("current_price") or {}).get("mid")) or 0.0
    setup = _active_setup(data)
    direction = str(setup.get("direction") or (data.get("final_decision_state") or {}).get("direction") or "NEUTRAL")
    trigger = _num(setup.get("breakoutTrigger") or setup.get("triggerPrice"))
    entry_low = _num(setup.get("entryZoneLow"))
    entry_high = _num(setup.get("entryZoneHigh"))
    chase = _num(setup.get("maxChasePrice"))
    invalidation = _num(setup.get("stopPrice") or setup.get("invalidationPrice"))
    targets = [_num(setup.get(f"tp{i}")) for i in range(1, 4)]
    targets = [value for value in targets if value is not None]
    closed_price = _num(normalized.get("lastClosedCandlePrice"))
    closed_at = normalized.get("lastClosedCandleTimestamp") or ""
    bullish = direction != "SHORT"
    crossed = bool(trigger is not None and (current >= trigger if bullish else current <= trigger))
    confirmed = bool(trigger is not None and closed_price is not None
                     and (closed_price >= trigger if bullish else closed_price <= trigger))
    too_far = bool(chase is not None and (current > chase if bullish else current < chase))
    if confirmed and too_far:
        opportunity = "WAIT_RETEST"
        trigger_state = "BREAKOUT_CONFIRMED"
    elif confirmed:
        opportunity = "CONFIRMED"
        trigger_state = "BREAKOUT_CONFIRMED"
    elif crossed:
        opportunity = "TRIGGERED"
        trigger_state = "WAIT_CLOSE_CONFIRMATION"
    else:
        opportunity = "WAITING"
        trigger_state = "WAIT_BREAKOUT_CONFIRMATION"
    defense_state = "ACTIVE"
    if invalidation is not None and ((direction == "SHORT" and current > invalidation)
                                     or (direction == "LONG" and current < invalidation)):
        defense_state = "POSITION_DEFENSE_TRIGGERED"
    next_close = _next_close(now_utc)
    freshness_input = {
        **data,
        "current_price": {**(data.get("current_price") or {}),
                          "mid": current,
                          "last_update": quote_time or (data.get("current_price") or {}).get("last_update")},
        "normalized_analysis": {**normalized, "currentPrice": current,
                                "marketDataTimestamp": quote_time or normalized.get("marketDataTimestamp")},
    }
    freshness = evaluate_freshness_state(freshness_input, now=now_utc)
    distance_trigger = None if trigger is None else round((trigger - current) if bullish else (current - trigger), 4)
    reference_entry = entry_high if bullish else entry_low
    risk = abs(reference_entry - invalidation) if reference_entry is not None and invalidation is not None else 0
    reward = abs(targets[0] - reference_entry) if targets and reference_entry is not None else 0
    rr = round(reward / risk, 3) if risk > 0 else None
    payload = {
        "currentPrice": current, "quoteTimeUtc": iso_utc(quote_time or normalized.get("marketDataTimestamp")),
        "latestClosed15mPrice": closed_price, "latestClosed15mTimeUtc": iso_utc(closed_at),
        "triggerPrice": trigger, "triggerState": trigger_state,
        "intrabarCrossed": crossed, "closedConfirmed": confirmed,
        "distanceToTrigger": distance_trigger,
        "entryZoneLow": entry_low, "entryZoneHigh": entry_high,
        "distanceToEntry": _zone_distance(current, entry_low, entry_high),
        "invalidationPrice": invalidation,
        "distanceToInvalidation": None if invalidation is None else round(abs(current - invalidation), 4),
        "chaseLimit": chase,
        "chaseDistance": None if chase is None else round((current - chase) if bullish else (chase - current), 4),
        "targets": targets, "effectiveRR": rr,
        "priceInvariantValid": validate_trade_prices(direction, reference_entry, invalidation, targets),
        "opportunityState": opportunity, "defenseState": defense_state,
        "nextCandleCloseAtUtc": iso_utc(next_close),
        "secondsToCandleClose": max(0, int((next_close - now_utc).total_seconds())),
        "freshnessState": freshness, "activeSetupId": setup.get("setupId") or setup.get("setup_id"),
        "dedupedScenarios": _dedupe_scenarios(data),
    }
    payload["factVersion"] = sha256(repr(sorted((k, str(v)) for k, v in payload.items()
                                                  if k not in {"secondsToCandleClose", "freshnessState"})).encode()).hexdigest()[:16]
    return payload
