"""Canonical user decision contract shared by API, web and notifications."""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.engines.candle_confirmation_registry import build_confirmation_registry
from app.engines.data_health_gate import evaluate_data_health

TERMINAL_SETUP_STATES = {"INVALIDATED", "ARCHIVED", "EXPIRED", "SETUP_EXPIRED",
                         "PULLBACK_INVALIDATED", "MISSED_ENTRY"}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def required_entry_for_rr(*, target: float | None, stop: float | None,
                          minimum_rr: float) -> float | None:
    """Solve RR=(reward/risk) for entry; valid for long and short."""
    if target is None or stop is None or minimum_rr <= 0:
        return None
    return round((target + minimum_rr * stop) / (1 + minimum_rr), 2)


def _route(candidate: dict) -> str:
    kind = str(candidate.get("setup_type") or candidate.get("source") or "").upper()
    return "PULLBACK" if any(word in kind for word in ("PULLBACK", "RETEST")) else "BREAKOUT"


def _candidate_view(candidate: dict, current: float | None, minimum_rr: float) -> dict:
    zone = candidate.get("entry_zone")
    low, high = ((float(zone[0]), float(zone[1]))
                 if isinstance(zone, (list, tuple)) and len(zone) == 2 else (None, None))
    direction = str(candidate.get("direction") or "NEUTRAL")
    stop = _number(candidate.get("invalidation_price"))
    targets = [float(value) for value in candidate.get("targets") or []
               if isinstance(value, (int, float))]
    target = targets[0] if targets else None
    in_zone = bool(current is not None and low is not None and high is not None
                   and low <= current <= high)
    evaluation_entry = (current if in_zone else (low + high) / 2
                        if low is not None and high is not None else current)
    rr = _number(candidate.get("risk_reward"))
    if evaluation_entry is not None and stop is not None and target is not None:
        risk = (evaluation_entry - stop if direction == "LONG"
                else stop - evaluation_entry)
        reward = (target - evaluation_entry if direction == "LONG"
                  else evaluation_entry - target)
        rr = round(reward / risk, 3) if risk > 0 else 0.0
    chase = _number(candidate.get("chase_limit"))
    chased = bool(current is not None and chase is not None and
                   ((direction == "LONG" and current > chase) or
                    (direction == "SHORT" and current < chase)))
    if chased:
        quality = "CHASE"
    elif rr is None or rr < minimum_rr:
        quality = "POOR"
    elif in_zone and str(candidate.get("lifecycle_state")) == "ENTRY_READY":
        quality = "IDEAL"
    else:
        quality = "ACCEPTABLE"
    route = _route(candidate)
    zone_label = ("最佳進場區" if quality == "IDEAL" else
                  "不建議進場區" if quality in {"POOR", "CHASE"} else
                  "回踩觀察區" if route == "PULLBACK" else "突破確認區")
    trigger = _number(candidate.get("trigger_price"))
    raw_state = str(candidate.get("lifecycle_state") or "WATCHING")
    setup_state = ({
        "SETUP": "WATCHING", "WAIT_CONFIRMATION": "WATCHING",
        "CONFIRMED_WAIT_RETEST": "CONFIRMED", "WAIT_RETEST": "CONFIRMED",
        "ENTRY_READY": "ENTRY_READY", "MISSED_ENTRY": "MISSED",
        "EXPIRED": "ARCHIVED", "SETUP_EXPIRED": "ARCHIVED",
        "INVALIDATED": "INVALIDATED", "PULLBACK_INVALIDATED": "INVALIDATED",
    }).get(raw_state, raw_state if raw_state in {
        "WATCHING", "SETUP_VALID", "ARMED", "CONFIRMED", "ENTRY_READY",
        "MISSED", "INVALIDATED", "ARCHIVED"} else "WATCHING")
    return {
        "setupId": str(candidate.get("scenario_id") or ""),
        "setupVersion": int(candidate.get("scenario_version") or 1),
        "route": route, "direction": direction,
        "confirmationLevel": trigger,
        "confirmationSource": "CLOSED_CANDLE",
        "entryZone": {"low": low, "high": high}, "entryZoneLabel": zone_label,
        "entryQuality": quality, "tacticalStop": stop,
        "targets": targets, "riskReward": rr,
        "rrPassed": rr is not None and rr >= minimum_rr,
        "requiredEntryPriceForMinRR": required_entry_for_rr(
            target=target, stop=stop, minimum_rr=minimum_rr),
        "chaseLimit": chase, "canEnter": (
            quality == "IDEAL" and str(candidate.get("lifecycle_state")) == "ENTRY_READY"),
        "blockedReasons": list(candidate.get("reason_codes") or []),
        "setupState": setup_state,
        "active": raw_state not in TERMINAL_SETUP_STATES,
    }


def _setup_score(item: dict, bias: str) -> int:
    rr = float(item.get("riskReward") or 0)
    rr_score = min(100.0, rr / 3.0 * 100.0)
    quality_score = {"IDEAL": 100, "ACCEPTABLE": 70, "POOR": 25,
                     "CHASE": 0, "INVALID": 0}.get(str(item.get("entryQuality")), 40)
    lifecycle_score = {"ENTRY_READY": 100, "CONFIRMED": 85, "ARMED": 70,
                       "WATCHING": 50}.get(str(item.get("setupState")), 45)
    aligned = str(item.get("direction")) == ("LONG" if bias == "BULLISH" else "SHORT")
    score = (lifecycle_score * .25 + rr_score * .25 + quality_score * .20
             + lifecycle_score * .15 + (100 if aligned else 20) * .10 + 50 * .05)
    return max(0, min(100, round(score)))


def _timeframe_bias(normalized: dict, timeframe: str) -> str:
    item = next((row for row in normalized.get("timeframeAssessments") or []
                 if str(row.get("timeframe")) == timeframe), {})
    trend = str(item.get("trend") or "neutral").upper()
    return "BULLISH" if "BULL" in trend else "BEARISH" if "BEAR" in trend else "NEUTRAL"


def build_canonical_decision(data: dict, final: dict) -> dict:
    """Build the sole next-trigger/new-entry/position-management contract."""
    settings = get_settings()
    minimum_rr = float(settings.decision_assistant_min_rr)
    health = evaluate_data_health(data)
    normalized = data.get("normalized_analysis") or {}
    current = _number(health.get("currentPrice"))
    all_candidates = [_candidate_view(item, current, minimum_rr)
                      for item in final.get("signalCandidates") or []]
    archived_candidates = [item for item in all_candidates if not item["active"]]
    candidates = [item for item in all_candidates if item["active"]]
    behavior_state = data.get("market_behavior_engine") or {}
    market_bias = str(behavior_state.get("market_bias") or
                      normalized.get("trendBias") or "neutral").upper()
    market_bias = ("BULLISH" if "BULL" in market_bias else
                   "BEARISH" if "BEAR" in market_bias else "NEUTRAL")
    for item in candidates:
        item["setupScore"] = _setup_score(item, market_bias)
    selected_id = str(final.get("selectedScenarioId") or "")
    engine_selected = next((item for item in candidates if item["setupId"] == selected_id), None)
    ranked = sorted(candidates, key=lambda item: item["setupScore"], reverse=True)
    selected = (engine_selected if bool(final.get("canEnter")) and engine_selected
                else ranked[0] if ranked else {})
    pullbacks = [item for item in candidates if item["route"] == "PULLBACK"]
    breakouts = [item for item in candidates if item["route"] == "BREAKOUT"]
    best_pullback = max(pullbacks, key=lambda item: item.get("riskReward") or -1,
                        default=None)
    best_breakout = max(breakouts, key=lambda item: item.get("riskReward") or -1,
                        default=None)
    trigger_level = selected.get("confirmationLevel") if selected else None
    direction = str(selected.get("direction") or final.get("direction") or "NEUTRAL")
    decision_timestamp = str(data.get("timestamp_utc") or health.get("evaluatedAt") or "")
    candle_time = str(normalized.get("lastClosedCandleTimestamp") or "")
    registry = build_confirmation_registry(
        symbol=str(data.get("symbol") or "XAUUSD"), candidates=candidates,
        live_price=current, last_closed_price=_number(normalized.get("lastClosedCandlePrice")),
        candle_close_time=candle_time, decision_timestamp=decision_timestamp)
    registry_key = (f"{data.get('symbol') or 'XAUUSD'}:15M:{trigger_level:.2f}:"
                    f"{'ABOVE' if direction == 'LONG' else 'BELOW'}"
                    if trigger_level is not None else "")
    confirmation = registry.get(registry_key) if registry_key else None
    canonical_trigger = ({
        "setupId": selected.get("setupId"), "direction": direction,
        "level": trigger_level, "timeframe": "15M", "condition": (
            "closeAbove" if direction == "LONG" else "closeBelow"),
        "source": "CLOSED_CANDLE",
        "sourceCandleTime": candle_time,
        "status": (confirmation or {}).get("status") or "NOT_REACHED",
        "label": (f"15M 收盤{'站上' if direction == 'LONG' else '跌破'} {trigger_level:.2f}；"
                  f"成立後重新計算 RR，≥ {minimum_rr:.2f} 才允許"
                  f"{' BUY' if direction == 'LONG' else ' SELL'}"
                  if trigger_level is not None else "等待新結構形成"),
    })
    early = _number(normalized.get("triggerLevel"))
    if early == trigger_level:
        early = None
    stale = not bool(health.get("healthy"))
    behavior = str(behavior_state.get("market_behavior") or "RANGE")
    rr_ok = bool(selected.get("rrPassed")) if selected else False
    confirmation_closed = (trigger_level is None or
                           (confirmation or {}).get("status") == "CLOSED_CONFIRMED")
    can_enter = bool(final.get("canEnter")) and rr_ok and not stale and confirmation_closed
    if direction == "LONG" and behavior in {
            "SLOW_BEARISH_DRIFT", "STRONG_DECLINE",
            "REVERSAL_WARNING", "REVERSAL_CONFIRMED"}:
        can_enter = False
    # Candidate lifecycle is diagnostic only. Do not let nested cards expose a
    # green permission that contradicts the canonical new-entry decision.
    selected_id = str((selected or {}).get("setupId") or "")
    for candidate in candidates:
        candidate["canEnter"] = bool(
            can_enter and str(candidate.get("setupId") or "") == selected_id)
    entry_action = ("BUY" if can_enter and direction == "LONG" else
              "SELL" if can_enter and direction == "SHORT" else "WAIT")
    if can_enter:
        canonical_trigger = None
    elif confirmation_closed and selected and selected.get("entryZone"):
        zone = selected["entryZone"]
        canonical_trigger = {
            "setupId": selected.get("setupId"), "direction": direction,
            "timeframe": "15M", "condition": "retestAndCloseHold",
            "status": "PENDING", "range": zone, "source": "CLOSED_CANDLE",
            "sourceCandleTime": candle_time,
            "label": (f"等待 15M 回到 {zone.get('low'):.2f}–{zone.get('high'):.2f} "
                      f"並收盤守住；成立後重新計算 RR，≥ {minimum_rr:.2f} 才允許"
                      f" {'BUY' if direction == 'LONG' else 'SELL'}"),
        }
    primary_reason = str(final.get("humanSummary") or "等待條件一致")
    if stale:
        primary_reason = "行情資料延遲，等待最新資料確認。"
    elif direction == "LONG" and behavior == "SLOW_BEARISH_DRIFT":
        primary_reason = "大方向仍偏多，但15M正在緩步下降，暫停追多並等待止跌。"
    elif direction == "LONG" and behavior in {
            "STRONG_DECLINE", "REVERSAL_WARNING", "REVERSAL_CONFIRMED"}:
        primary_reason = "15M價格行為已轉弱，暫停新的多單進場。"
    elif selected and not rr_ok:
        primary_reason = "方向可能正確，但目前進場盈虧比不合格。"
    position = data.get("position_management") or {}
    levels = list(normalized.get("confirmationLevels") or [])
    resistance_levels = [float(item["price"]) for item in levels
                         if item.get("kind") == "resistance"
                         and isinstance(item.get("price"), (int, float))
                         and (current is None or float(item["price"]) >= current)]
    support_levels = [float(item["price"]) for item in levels
                      if item.get("kind") == "support"
                      and isinstance(item.get("price"), (int, float))
                      and (current is None or float(item["price"]) <= current)]
    known = bool(position.get("has_position") and
                 float(position.get("position_size") or 0) != 0)
    position_side = str(position.get("position_side") or "").upper() if known else None
    position_action = str(position.get("recommended_action") or "HOLD").upper()
    normalized_position_action = next((name for name in ("EXIT", "REDUCE", "HOLD")
                                       if name in position_action), "HOLD") if known else None
    management_mode = None
    if known and position_side == "LONG":
        if behavior == "REVERSAL_CONFIRMED":
            normalized_position_action, management_mode = "EXIT", "REVERSAL_EXIT"
        elif behavior == "STRONG_DECLINE":
            normalized_position_action, management_mode = "REDUCE", "DEFENSIVE_MANAGEMENT"
        elif behavior in {"SLOW_BEARISH_DRIFT", "REVERSAL_WARNING"}:
            normalized_position_action, management_mode = "HOLD", "HOLD_WITH_CAUTION"
    action = normalized_position_action if known else entry_action
    actual_entry = _number(position.get("entry_price"))
    position_rr = None
    if (known and actual_entry is not None and selected
            and selected.get("tacticalStop") is not None and selected.get("targets")):
        stop = float(selected["tacticalStop"])
        target = float(selected["targets"][0])
        risk = actual_entry - stop if position_side == "LONG" else stop - actual_entry
        reward = target - actual_entry if position_side == "LONG" else actual_entry - target
        position_rr = round(reward / risk, 3) if risk > 0 else None
    tp_facts = [fact for fact in data.get("signal_facts") or []
                if str(fact.get("event_type") or "").startswith("TAKE_PROFIT_")]
    reason_codes = []
    if confirmation and confirmation["status"] != "CLOSED_CONFIRMED":
        reason_codes.append("CANDLE_NOT_CLOSED" if confirmation["status"] == "IN_PROGRESS"
                            else "STRUCTURE_NOT_CONFIRMED")
    if selected and not rr_ok:
        reason_codes.append("RR_INSUFFICIENT")
    if selected and selected.get("entryQuality") == "CHASE":
        reason_codes.append("CHASING")
    if stale:
        reason_codes.append("DATA_STALE")
    if selected and selected.get("entryQuality") == "ACCEPTABLE":
        reason_codes.append("ENTRY_ZONE_NOT_REACHED")
    reason_codes = list(dict.fromkeys(reason_codes))[:3]
    setup_state = str(selected.get("setupState") or "WATCHING")
    return {
        "schemaVersion": "canonical-trading-decision-v2",
        "timestamp": decision_timestamp,
        "bias4h": _timeframe_bias(normalized, "4H"),
        "bias1h": _timeframe_bias(normalized, "1H"),
        "behavior15m": behavior_state.get("behavior_15m") or behavior,
        "bearishPressure": ("STRONG" if behavior in {"STRONG_DECLINE", "REVERSAL_CONFIRMED"}
                            else "MODERATE" if behavior in {"SLOW_BEARISH_DRIFT", "REVERSAL_WARNING"}
                            else "WEAK"),
        "primaryAction": action,
        "primaryReason": primary_reason,
        "canonicalNextTrigger": canonical_trigger,
        "primaryNextTrigger": canonical_trigger,
        "activeSetupId": selected.get("setupId") or None,
        "activeSetupType": selected.get("route") or None,
        "setupState": setup_state,
        "confirmationLevel": trigger_level,
        "confirmationStatus": (confirmation or {}).get("status") or "NOT_REACHED",
        "confirmationRegistry": registry,
        "executableZone": selected.get("entryZone") if selected else None,
        "rr": selected.get("riskReward") if selected else None,
        "rrValid": rr_ok,
        "entryQuality": selected.get("entryQuality") if selected else "INVALID",
        "reasonCodes": reason_codes,
        "primarySetup": selected or None,
        "alternativeSetups": [item for item in ranked
                              if item.get("setupId") != selected.get("setupId")],
        "archivedSetups": archived_candidates,
        "bestCurrentOpportunity": (f"等待{'多方' if direction == 'LONG' else '空方'}"
                                   f"{'回踩' if selected.get('route') == 'PULLBACK' else '突破'}"
                                   if selected else "目前沒有有效交易機會"),
        "behaviorTransition": {
            "previous": behavior_state.get("previous_behavior"),
            "current": behavior_state.get("market_behavior") or behavior,
            "changedAt": behavior_state.get("changed_at") or decision_timestamp,
        },
        "earlyStrengthLevel": ({"level": early, "label": "初步轉強價"}
                               if early is not None else None),
        "entryConfirmationLevel": trigger_level,
        "confirmationSource": "CLOSED_CANDLE",
        "lastClosedCandleTime": str(normalized.get("lastClosedCandleTimestamp") or ""),
        "lastClosedCandlePrice": normalized.get("lastClosedCandlePrice"),
        "marketBias": behavior_state.get("market_bias") or str(
            normalized.get("trendBias") or "neutral").upper(),
        "marketBehavior": behavior,
        "behaviorConfidence": behavior_state.get("behavior_confidence"),
        "behavior1h": behavior_state.get("behavior_1h"),
        "behavior4h": behavior_state.get("behavior_4h"),
        "structureStatus": behavior_state.get("structure_status"),
        "momentumStatus": behavior_state.get("momentum_status"),
        "nextBullishConfirmation": (min(resistance_levels) if resistance_levels
                                    else trigger_level if direction == "LONG" else None),
        "nextBearishConfirmation": max(support_levels) if support_levels else None,
        "marketBehaviorDecision": {
            "market_bias": behavior_state.get("market_bias") or str(
                normalized.get("trendBias") or "neutral").upper(),
            "market_behavior": behavior,
            "behavior_confidence": behavior_state.get("behavior_confidence"),
            "behavior_15m": behavior_state.get("behavior_15m"),
            "behavior_1h": behavior_state.get("behavior_1h"),
            "behavior_4h": behavior_state.get("behavior_4h"),
            "structure_status": behavior_state.get("structure_status"),
            "momentum_status": behavior_state.get("momentum_status"),
            "primary_action": action, "new_entry_action": entry_action,
            "reason": primary_reason,
            "next_bullish_confirmation": (min(resistance_levels) if resistance_levels
                                           else trigger_level if direction == "LONG" else None),
            "next_bearish_confirmation": max(support_levels) if support_levels else None,
        },
        "dataStale": stale,
        "newEntryDecision": {
            "action": entry_action, "canEnter": can_enter, "direction": direction,
            "tradeStatus": ("WAIT_DATA_CONFIRMATION" if stale else
                            "WAIT_BEHAVIOR_CONFIRMATION" if direction == "LONG" and
                            behavior in {"SLOW_BEARISH_DRIFT", "STRONG_DECLINE",
                                         "REVERSAL_WARNING", "REVERSAL_CONFIRMED"} else
                            "NO_ENTRY_RR" if selected and not rr_ok else
                            "ENTRY_READY" if can_enter else "WAIT_CONFIRMATION"),
            "selectedSetup": selected or None,
            "pullbackLong": best_pullback,
            "breakoutLong": best_breakout,
            "preferredRoute": ("PULLBACK" if best_pullback and
                               (best_pullback.get("riskReward") or -1) >
                               ((best_breakout or {}).get("riskReward") or -1)
                               else "BREAKOUT" if best_breakout else None),
        },
        "positionManagement": {
            "positionKnown": known,
            "collapsedByDefault": not known,
            "message": ("未取得實際持倉資料" if not known else None),
            "actualSide": position_side,
            "actualEntryPrice": actual_entry,
            "actualSize": _number(position.get("position_size")),
            "currentPrice": current,
            "unrealizedPnl": _number(position.get("unrealized_pnl")),
            "action": normalized_position_action,
            "managementMode": management_mode,
            "riskRewardFromActualEntry": position_rr,
            "tacticalDefense": selected.get("tacticalStop") if selected else None,
            "structuralInvalidation": normalized.get("structuralInvalidationLevel"),
            "structuralInvalidationNote": normalized.get("structuralInvalidationNote") or "",
            "targets": selected.get("targets") if selected else [],
        },
        "reversalProtection": {
            "cooldownActive": bool(tp_facts),
            "sourceEvents": [fact.get("event_type") for fact in tp_facts],
            "requiresIndependentOppositeConfirmation": True,
        },
    }
