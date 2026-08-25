"""Separate early reversal, executable scalp trigger and trend confirmation."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings


def _number(value: Any, default: float | None = None) -> float | None:
    return float(value) if isinstance(value, (int, float)) else default


def _levels(normalized: dict, kind: str) -> list[tuple[float, str]]:
    values = []
    for item in normalized.get("confirmationLevels") or []:
        price = _number(item.get("price"))
        if item.get("kind") == kind and price is not None:
            values.append((price, str(item.get("timeframe") or "15M").upper()))
    return sorted(values)


def validate_trigger_distance(*, direction: str, current_price: float,
                              trigger: float, atr15: float,
                              invalidation: float, nearest_target: float,
                              minimum_rr: float) -> dict:
    """Reject confirmations that consume the tradable move before entry."""
    settings = get_settings()
    distance = abs(trigger - current_price)
    maximum = max(
        atr15 * settings.scalp_trigger_max_distance_atr_mult,
        current_price * settings.scalp_trigger_max_distance_price_pct,
    )
    risk = trigger - invalidation if direction == "LONG" else invalidation - trigger
    reward = nearest_target - trigger if direction == "LONG" else trigger - nearest_target
    rr = reward / risk if risk > 0 and reward > 0 else 0.0
    too_late = distance > maximum or rr < minimum_rr
    return {
        "valid": not too_late,
        "status": "TRIGGER_TOO_LATE_FOR_SCALP" if too_late else "EXECUTABLE_DISTANCE",
        "distance": round(distance, 3), "maximumDistance": round(maximum, 3),
        "remainingRR": round(rr, 3), "minimumRR": minimum_rr,
    }


def build_scalp_trigger_layers(*, direction: str, source_level: float,
                               normalized: dict, created_at: str = "") -> dict:
    """Build three independent confirmation layers from runtime structure."""
    settings = get_settings()
    current = _number(normalized.get("currentPrice"), source_level) or source_level
    atr = max(_number(normalized.get("atr15"), 0.01) or 0.01, 0.01)
    sign = 1 if direction == "LONG" else -1
    kind = "resistance" if direction == "LONG" else "support"
    levels = _levels(normalized, kind)
    ahead = [(price, tf) for price, tf in levels
             if (price > current if direction == "LONG" else price < current)]
    tactical = [(price, tf) for price, tf in ahead if tf in {"15M", "5M", "1M"}]
    structural = [(price, tf) for price, tf in ahead if tf in {"1H", "4H", "1D"}]
    if not tactical and ahead:
        tactical = [ahead[0] if direction == "LONG" else ahead[-1]]
    raw_trigger = ((min(tactical, key=lambda item: abs(item[0] - current))[0])
                   if tactical else current + sign * atr * .15)
    beyond_trigger = [
        price for price, _ in ahead if sign * (price - raw_trigger) > 0
    ]
    if structural:
        structural_level, structural_timeframe = min(
            structural, key=lambda item: abs(item[0] - current))
    elif beyond_trigger:
        structural_level = min(
            beyond_trigger, key=lambda price: abs(price - raw_trigger))
        structural_timeframe = next(
            tf for price, tf in ahead if price == structural_level)
    else:
        structural_level = raw_trigger + sign * atr * 1.5
        structural_timeframe = "15M"
    opposite = _levels(normalized, "support" if direction == "LONG" else "resistance")
    invalidation_candidates = [price for price, _ in opposite if (
        price < source_level if direction == "LONG" else price > source_level)]
    invalidation = (max(invalidation_candidates) if direction == "LONG" and invalidation_candidates
                    else min(invalidation_candidates) if direction == "SHORT" and invalidation_candidates
                    else source_level - sign * atr * settings.scalp_trigger_invalidation_atr_mult)
    nearest_target = structural_level
    guard = validate_trigger_distance(
        direction=direction, current_price=current, trigger=raw_trigger, atr15=atr,
        invalidation=invalidation, nearest_target=nearest_target,
        minimum_rr=float(settings.decision_assistant_min_rr))
    trigger = raw_trigger
    trigger_source = "LATEST_15M_MICRO_STRUCTURE"
    if not guard["valid"]:
        # The nearby reclaim extension is an execution checkpoint; the distant
        # level remains trend confirmation and can never gate a scalp entry.
        trigger = current + sign * atr * .15
        trigger_source = "FAILED_BREAK_RECLAIM_EXTENSION"
        guard = validate_trigger_distance(
            direction=direction, current_price=current, trigger=trigger, atr15=atr,
            invalidation=invalidation, nearest_target=nearest_target,
            minimum_rr=float(settings.decision_assistant_min_rr))
    width = atr * settings.scalp_trigger_zone_atr_mult
    zone_low, zone_high = sorted((trigger - width, trigger + width))
    adverse_entry = zone_high if direction == "LONG" else zone_low
    risk = (adverse_entry - invalidation if direction == "LONG"
            else invalidation - adverse_entry)
    structural_reward = (nearest_target - adverse_entry if direction == "LONG"
                         else adverse_entry - nearest_target)
    targets = ([nearest_target]
               if risk > 0 and structural_reward / risk >=
               settings.decision_assistant_min_rr else [])
    for multiple in (1.5, 2.0, 3.0):
        projected = adverse_entry + sign * max(risk, atr * .25) * multiple
        if all(abs(projected - value) > atr * .10 for value in targets):
            targets.append(projected)
    targets = sorted(targets, reverse=direction == "SHORT")[:3]
    timestamp = created_at or str(normalized.get("lastClosedCandleTimestamp") or
                                  datetime.now(timezone.utc).isoformat())
    # This identity intentionally excludes current price and candle time.  A new
    # closed candle revalidates the trigger, but cannot move the goalpost unless
    # the underlying support/resistance structure actually changed.
    structure_seed = "|".join(
        f"{item.get('kind')}:{item.get('timeframe')}:{float(item.get('price')):.4f}"
        for item in normalized.get("confirmationLevels") or []
        if isinstance(item.get("price"), (int, float))
    )
    source_id = hashlib.sha256(
        f"{direction}|{source_level:.4f}|{structure_seed}".encode()
    ).hexdigest()[:20]
    try:
        created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        created = datetime.now(timezone.utc)
        timestamp = created.isoformat()
    expires = created + timedelta(
        minutes=15 * settings.fake_breakout_recovery_expiry_bars)
    return {
        "confirmationArchitecture": "scalp-trigger-v1",
        "earlyReversal": {"state": f"EARLY_REVERSAL_{direction}", "active": True,
                          "sourceLevel": round(source_level, 2)},
        "primaryScalpTrigger": {
            "state": "SCALP_ENTRY_TRIGGER", "direction": direction,
            "level": round(trigger, 2), "timeframe": "15M",
            "condition": "closeAbove" if direction == "LONG" else "closeBelow",
            "source": trigger_source, "distanceGuard": guard,
        },
        "structuralConfirmationTrigger": {
            "state": "TREND_REVERSAL_CONFIRMED", "direction": direction,
            "level": round(structural_level, 2), "timeframe": structural_timeframe,
            "entryGate": False,
            "purpose": "CONFIDENCE_HOLDING_TP_RUNNER_ONLY",
        },
        "candidateEntryZone": {"low": round(zone_low, 2), "high": round(zone_high, 2)},
        "invalidationLevel": round(invalidation, 2),
        "targets": [round(value, 2) for value in targets],
        "estimatedRR": guard["remainingRR"],
        "triggerCreatedAt": timestamp, "triggerSourceStructureId": source_id,
        "triggerTTL": settings.fake_breakout_recovery_expiry_bars,
        "triggerExpiresAt": expires.isoformat(),
        "triggerRevalidation": "REVALIDATE_ON_EACH_CLOSED_15M_OR_NEW_STRUCTURE",
    }
