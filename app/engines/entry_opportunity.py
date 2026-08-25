"""Unified multi-zone entry opportunities; Canonical remains the sole arbiter."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.engines.scenario_safety import calculate_risk_reward

TYPES = ("SHALLOW_PULLBACK", "DEEP_PULLBACK", "BREAKOUT_RETEST")
TERMINAL = {"REJECTED", "MISSED", "EXPIRED", "INVALIDATED"}
ACTION_PRIORITY = {
    "BREAKOUT_RETEST": 0,
    "SHALLOW_PULLBACK": 1,
    "LOCAL_STRUCTURE_RECLAIM": 2,
    "DEEP_PULLBACK": 3,
}


def _num(value, default=None):
    return float(value) if isinstance(value, (int, float)) else default


def _opportunity_id(setup_id: str, kind: str, low: float, high: float) -> str:
    raw = f"{setup_id}|{kind}|{low:.2f}|{high:.2f}"
    return f"OP-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _rr(side: str, entry: float, stop: float, target: float) -> float | None:
    result = calculate_risk_reward(
        side, evaluation_entry_price=entry, stop_loss=stop, target_price=target)
    return round(float(result["ratio"]), 3) if result["available"] else None


def _zone(kind: str, setup: dict, normalized: dict) -> tuple[float, float, float, list[str]] | None:
    side = str(setup.get("direction") or "LONG")
    sign = 1 if side == "LONG" else -1
    trigger = _num(setup.get("breakoutTrigger"))
    atr = max(_num(setup.get("atr15") or normalized.get("atr15"), 0.0), .01)
    if trigger is None:
        return None
    width = max(atr * .16, abs(_num(setup.get("entryZoneHigh"), trigger) -
                               _num(setup.get("entryZoneLow"), trigger)) / 2)
    if kind == "BREAKOUT_RETEST":
        center, reasons = trigger, ["已確認突破位", "ATR 回測緩衝"]
    elif kind == "SHALLOW_PULLBACK":
        micro = []
        wanted = "support" if side == "LONG" else "resistance"
        for item in normalized.get("confirmationLevels") or []:
            if item.get("kind") == wanted and item.get("timeframe") == "15M" and isinstance(item.get("price"), (int, float)):
                micro.append(float(item["price"]))
        structural = (max((x for x in micro if x <= trigger), default=trigger - sign * atr * .35)
                      if side == "LONG" else
                      min((x for x in micro if x >= trigger), default=trigger - sign * atr * .35))
        center = (structural + (trigger - sign * atr * .35)) / 2
        reasons = ["15M 微型高低點", "ATR 淺回撤"]
    else:
        legacy_low, legacy_high = (_num(setup.get("pullbackEntryZoneLow")),
                                   _num(setup.get("pullbackEntryZoneHigh")))
        center = ((legacy_low + legacy_high) / 2
                  if legacy_low is not None and legacy_high is not None
                  else trigger - sign * atr * .80)
        reasons = list(setup.get("pullbackZoneReason") or ["主要高低點", "ATR 深回撤"])
    low, high = sorted((center - width, center + width))
    stop = (low - max(atr * .35, width * 1.5) if side == "LONG"
            else high + max(atr * .35, width * 1.5))
    return round(low, 2), round(high, 2), round(stop, 2), reasons


def _confirmed(side: str, zone: tuple[float, float], candle: dict) -> tuple[bool, list[str]]:
    low, high = zone
    values = {key: _num(candle.get(key)) for key in ("open", "high", "low", "close")}
    if any(value is None for value in values.values()):
        return False, []
    touched = values["low"] <= high if side == "LONG" else values["high"] >= low
    reclaim = values["close"] >= high if side == "LONG" else values["close"] <= low
    directional = values["close"] > values["open"] if side == "LONG" else values["close"] < values["open"]
    evidence = []
    if touched and reclaim:
        evidence.append("15M 收盤重新站回觀察區" if side == "LONG" else "15M 收盤重新跌回觀察區")
    if touched and directional:
        evidence.append("15M 方向 K 棒確認")
    return bool(evidence), evidence


def _htf_aligned(normalized: dict, side: str) -> bool:
    """4H/1H are the continuation background; 15M remains the trigger."""
    wanted = "bull" if side == "LONG" else "bear"
    assessments = {
        str(item.get("timeframe")): str(item.get("trend") or "").lower()
        for item in normalized.get("timeframeAssessments") or []
    }
    if assessments.get("1H") and assessments.get("4H"):
        return wanted in assessments["1H"] and wanted in assessments["4H"]
    # Backward-compatible fallback for persisted snapshots created before the
    # per-timeframe assessment field existed.
    return str(normalized.get("trendBias") or "") == (
        "bullish" if side == "LONG" else "bearish")


def _nearest_breakout_level(normalized: dict, side: str, price: float,
                            atr: float) -> float | None:
    wanted = "resistance" if side == "LONG" else "support"
    levels = [float(item["price"])
              for item in normalized.get("confirmationLevels") or []
              if item.get("kind") == wanted
              and str(item.get("timeframe") or "15M") == "15M"
              and isinstance(item.get("price"), (int, float))]
    if not levels:
        return None
    # A level just crossed intrabar remains relevant until a closed candle
    # confirms it. Far legacy levels are rejected by the re-anchor gate below.
    nearby = [level for level in levels if abs(level - price) <= max(atr * 2.0, price * .01)]
    return min(nearby or levels, key=lambda level: abs(level - price))


def _breakout_retest_confirmed(side: str, level: float, zone: tuple[float, float],
                               candle: dict) -> tuple[bool, list[str]]:
    low, high = zone
    values = {key: _num(candle.get(key)) for key in ("open", "high", "low", "close")}
    if any(value is None for value in values.values()):
        return False, []
    touched = values["low"] <= high if side == "LONG" else values["high"] >= low
    held = values["close"] >= level if side == "LONG" else values["close"] <= level
    directional = values["close"] > values["open"] if side == "LONG" else values["close"] < values["open"]
    wick_rejection = ((values["open"] - values["low"]) > abs(values["close"] - values["open"])
                      if side == "LONG" else
                      (values["high"] - values["open"]) > abs(values["close"] - values["open"]))
    evidence = []
    if touched and held:
        evidence.append("15M 回測突破位後收盤守住")
    if touched and held and (directional or wick_rejection):
        evidence.append("15M 出現方向 K 棒或影線拒絕")
    return len(evidence) >= 2, evidence


def reanchor_entry_candidates(opportunities: list[dict], *, current_price: float,
                              atr15: float, direction: str,
                              minimum_rr: float) -> list[dict]:
    """Keep distant valid zones as backups but never as the next action."""
    settings = get_settings()
    max_distance = max(
        atr15 * settings.entry_anchor_max_distance_atr_mult,
        current_price * settings.entry_anchor_max_distance_price_pct,
    )
    for item in opportunities:
        zone = item.get("entry_zone") or {}
        low, high = _num(zone.get("lower")), _num(zone.get("upper"))
        midpoint = ((low + high) / 2 if low is not None and high is not None else None)
        anchor_distance = abs(current_price - midpoint) if midpoint is not None else float("inf")
        aligned = str(item.get("side")) == direction
        rr_qualified = (item.get("estimated_rr") is not None
                        and float(item["estimated_rr"]) >= minimum_rr)
        stale_anchor = anchor_distance > max_distance
        item.update({
            "anchor_midpoint": round(midpoint, 2) if midpoint is not None else None,
            "anchor_distance": round(anchor_distance, 2),
            "anchor_max_distance": round(max_distance, 2),
            "anchor_stale": stale_anchor,
            "primary_eligible": bool(aligned and rr_qualified and not stale_anchor
                                     and item.get("state") not in TERMINAL),
            "anchor_role": ("DEEP_PULLBACK_BACKUP" if stale_anchor
                            else "PRIMARY_CANDIDATE"),
        })
    return opportunities


def evaluate_entry_opportunities(data: dict, previous: dict | None = None) -> tuple[dict, list[dict]]:
    previous = previous or {}
    normalized = data.get("normalized_analysis") or {}
    manager = data.get("breakout_setup_manager") or {}
    setup = manager.get("activeSetup") or {}
    if not setup:
        return {"opportunities": [], "primaryOpportunityId": None,
                "alternativeOpportunityIds": []}, []
    setup_id = str(setup.get("setupId") or "")
    side = str(setup.get("direction") or "LONG")
    price = _num(normalized.get("currentPrice"), 0.0)
    target = _num(setup.get("tp1"))
    candle = data.get("latest_closed_15m") or {}
    candle_time = str(normalized.get("lastClosedCandleTimestamp") or "")
    now_text = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    now = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
    expires = min(
        datetime.fromisoformat(str(setup.get("expiresAt") or (now + timedelta(hours=2)).isoformat()).replace("Z", "+00:00")),
        now + timedelta(hours=2),
    )
    previous_map = {str(x.get("opportunity_id")): x for x in previous.get("opportunities") or []}
    opportunities = []
    events = []
    min_rr = float(get_settings().decision_assistant_min_rr)
    atr = max(_num(setup.get("atr15") or normalized.get("atr15"), .01), .01)
    strong_shallow = (str(normalized.get("trendBias")) == ("bullish" if side == "LONG" else "bearish")
                      and str(normalized.get("shortTermMomentum")) in {"accelerating", "stable", "recovering"}
                      and bool(setup.get("breakoutConfirmedAt")))
    break_state = data.get("break_lifecycle_engine") or {}
    continuation_level = (_nearest_breakout_level(normalized, side, price, atr)
                          if _htf_aligned(normalized, side) else None)
    breakout_buffer = atr * float(get_settings().breakout_close_buffer_atr_mult)
    for kind in TYPES:
        continuation = kind == "BREAKOUT_RETEST" and continuation_level is not None
        if continuation:
            width = max(atr * .16, breakout_buffer)
            low, high = sorted((continuation_level - width,
                                continuation_level + width))
            stop = (low - max(atr * .35, width * 1.5) if side == "LONG"
                    else high + max(atr * .35, width * 1.5))
            built = (round(low, 2), round(high, 2), round(stop, 2),
                     ["最近15M高低點", "新突破位轉為回測支撐／壓力"])
        else:
            built = _zone(kind, setup, normalized)
        if not built or target is None:
            continue
        low, high, stop, reasons = built
        opportunity_setup_id = (f"{setup_id}:CONT:{continuation_level:.2f}"
                                if continuation else setup_id)
        oid = _opportunity_id(opportunity_setup_id, kind, low, high)
        old = previous_map.get(oid) or {}
        item_target = target
        if continuation:
            sign = 1 if side == "LONG" else -1
            reference_entry = high if side == "LONG" else low
            risk = abs(reference_entry - stop)
            projected = continuation_level + sign * max(atr * 2.0, risk * 1.8)
            target_valid = sign * (target - reference_entry) > 0
            item_target = (max(target, projected) if side == "LONG" and target_valid else
                           min(target, projected) if side == "SHORT" and target_valid else
                           projected)
            item_target = round(item_target, 2)
        # Preview uses the zone midpoint. The execution gate recalculates from
        # the actual confirmed candidate price and may therefore reject it.
        estimated_entry = (low + high) / 2
        estimated_rr = _rr(side, estimated_entry, stop, item_target)
        distance = 0.0 if low <= price <= high else min(abs(price - low), abs(price - high))
        in_zone = low <= price <= high
        confirmation, evidence = _confirmed(side, (low, high), candle)
        reclaim_requires_hold = (
            (side == "LONG" and break_state.get("state") == "FAILED_BREAKDOWN") or
            (side == "SHORT" and break_state.get("state") == "FAILED_BREAKOUT"))
        if reclaim_requires_hold:
            confirmation = False
            evidence = ["快速收復已出現，仍需下一根15M守住 reclaim level"]
        closed_price = _num(candle.get("close"))
        invalidated = ((closed_price or price) < stop if side == "LONG"
                       else (closed_price or price) > stop)
        breakout_confirmed_now = bool(continuation and closed_price is not None and (
            closed_price > continuation_level + breakout_buffer if side == "LONG"
            else closed_price < continuation_level - breakout_buffer))
        breakout_confirmed_at = (old.get("breakout_confirmed_at") or
                                 (now_text if breakout_confirmed_now else None))
        breakout_candle_time = (old.get("breakout_confirmed_candle_time") or
                                (candle_time if breakout_confirmed_now else None))
        if continuation and breakout_confirmed_at:
            is_later_retest_candle = candle_time != str(breakout_candle_time or "")
            confirmation, evidence = _breakout_retest_confirmed(
                side, continuation_level, (low, high), candle)
            confirmation = confirmation and is_later_retest_candle
            if not is_later_retest_candle:
                evidence = ["突破剛由本根15M確認，下一根開始監控回測"]
        if now >= expires:
            state = "EXPIRED"
        elif continuation and not breakout_confirmed_at:
            state = "WAIT_BREAKOUT"
        elif invalidated:
            state = "INVALIDATED"
        elif continuation and not (in_zone and confirmation):
            state = "WAIT_BREAKOUT_RETEST"
        elif in_zone and not confirmation:
            state = "WAIT_CONFIRMATION"
        elif in_zone and confirmation:
            state = "CONFIRMED"
        elif distance <= atr * .35:
            state = "APPROACHING"
        else:
            state = "WATCHING"
        executable_rr = None
        executable_at = None
        candidate_entry = None
        if state == "CONFIRMED":
            candidate_entry = price
            executable_rr = _rr(side, price, stop, item_target)
            executable_at = now_text
            state = "ENTRY_READY" if executable_rr is not None and executable_rr >= min_rr else "REJECTED"
        # Executable RR is ephemeral and is never retained outside the zone.
        if not in_zone:
            executable_rr = executable_at = candidate_entry = None
            if old.get("state") == "ENTRY_READY" and not continuation:
                state = "MISSED"
        reachability = max(0.0, 100.0 - distance / atr * 45.0)
        if strong_shallow and kind == "SHALLOW_PULLBACK":
            reachability = min(100.0, reachability + 18.0)
        rr_score = min(100.0, (estimated_rr or 0) / 3.0 * 100.0)
        confirmation_score = 100.0 if confirmation else 65.0 if in_zone else 35.0
        score = round(reachability * .40 + rr_score * .25 + confirmation_score * .20
                      + (85.0 if strong_shallow and kind == "SHALLOW_PULLBACK" else 60.0) * .15)
        quality = ("IDEAL" if (executable_rr or 0) >= min_rr and confirmation else
                   "ACCEPTABLE" if (estimated_rr or 0) >= min_rr else "POOR")
        item = {
            "opportunity_id": oid, "setup_id": setup_id, "type": kind, "side": side,
            "entry_zone": {"lower": low, "upper": high}, "state": state,
            "estimated_rr": estimated_rr, "executable_rr": executable_rr,
            "candidate_entry": candidate_entry, "tactical_stop": stop,
            "target1": item_target,
            "trigger_level": continuation_level if continuation else _num(
                setup.get("breakoutTrigger")),
            "entry_type": "BREAKOUT_RETEST" if continuation else kind,
            "entry_quality": quality, "confirmation_status": (
                "CLOSED_CANDLE_CONFIRMED" if confirmation else "WAIT_CLOSED_CANDLE"),
            "confirmation_evidence": evidence, "zone_reasons": reasons,
            "distance_from_current": round(distance, 2),
            "reachability_score": round(reachability), "opportunity_score": score,
            "created_at": str(old.get("created_at") or now_text),
            "confirmed_at": now_text if confirmation else None,
            "confirmed_candle_time": candle_time if confirmation else None,
            "executable_rr_calculated_at": executable_at,
            "expires_at": expires.isoformat(), "max_valid_bars": 8,
            "strong_trend_shallow_retrace_mode": strong_shallow,
            "reclaim_confirmation_required": reclaim_requires_hold,
            "breakout_continuation": continuation,
            "breakout_buffer": round(breakout_buffer, 2) if continuation else None,
            "breakout_confirmed_at": breakout_confirmed_at,
            "breakout_confirmed_candle_time": breakout_candle_time,
        }
        opportunities.append(item)
        if old and old.get("state") != state:
            event_type = {
                "APPROACHING": "RETRACE_APPROACHING",
                "WAIT_CONFIRMATION": "RETRACE_ZONE_ENTERED",
                "WAIT_BREAKOUT_RETEST": "WAIT_RETRACE",
                "ENTRY_READY": "ENTRY_READY",
                "ALTERNATIVE_READY": "SETUP_FORMING",
                "MISSED": "MISSED_ENTRY",
                "EXPIRED": "SETUP_EXPIRED",
                "INVALIDATED": "ENTRY_INVALIDATED",
                "REJECTED": "SETUP_WEAKENING",
            }.get(state, "SETUP_FORMING")
            events.append({"event_type": event_type, "setupId": setup_id,
                           "opportunityId": oid, "previousState": old.get("state"),
                           "currentState": state, "entryZone": {"low": low, "high": high},
                           "effectiveRR": executable_rr, "estimatedRR": estimated_rr,
                           "candleCloseTime": candle_time, "calculatedAt": now_text,
                           "direction": side})
    opportunities = reanchor_entry_candidates(
        opportunities, current_price=price, atr15=atr, direction=side,
        minimum_rr=min_rr)
    ranked = sorted(opportunities, key=lambda x: (
        not bool(x.get("primary_eligible")),
        x["state"] != "ENTRY_READY",
        float(x.get("anchor_distance") or 0),
        ACTION_PRIORITY.get(str(x.get("type")), 99),
        -int(x.get("opportunity_score") or 0),
    ))
    primary = next((item for item in ranked if item.get("primary_eligible")), None)
    for item in ranked:
        if item is not primary and item["state"] == "ENTRY_READY":
            item["state"] = "ALTERNATIVE_READY"
    primary_id = primary["opportunity_id"] if primary else None
    events = [event for event in events
              if event.get("event_type") != "ENTRY_READY"
              or event.get("opportunityId") == primary_id]
    archived = []
    for old in previous.get("opportunities") or []:
        if str(old.get("setup_id")) == setup_id:
            continue
        expired = {**old, "state": "EXPIRED", "executable_rr": None,
                   "candidate_entry": None, "executable_rr_calculated_at": None}
        archived.append(expired)
        if old.get("state") not in TERMINAL:
            events.append({"event_type": "SETUP_EXPIRED", "setupId": old.get("setup_id"),
                           "opportunityId": old.get("opportunity_id"),
                           "previousState": old.get("state"), "currentState": "EXPIRED",
                           "calculatedAt": now_text, "candleCloseTime": candle_time,
                           "direction": old.get("side")})
    return {
        "schemaVersion": "entry-opportunity-v2", "setupId": setup_id,
        "strongTrendShallowRetraceMode": strong_shallow,
        "opportunities": ranked,
        "primaryOpportunityId": primary_id,
        "alternativeOpportunityIds": [x["opportunity_id"] for x in ranked[1:]],
        "bestReachableOpportunity": primary,
        "archivedOpportunities": archived,
    }, events
