"""Canonical user decision contract shared by API, web and notifications."""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.engines.candle_confirmation_registry import build_confirmation_registry
from app.engines.confidence import get_confidence_grade
from app.engines.data_health_gate import evaluate_data_health
from app.engines.decision_health import evaluate_decision_health

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
    estimated_rr = _number(candidate.get("estimated_risk_reward"))
    lifecycle = str(candidate.get("lifecycle_state") or "WATCHING")
    if (lifecycle == "ENTRY_READY" and in_zone and evaluation_entry is not None
            and stop is not None and target is not None):
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
    elif lifecycle == "ENTRY_READY" and (rr is None or rr < minimum_rr):
        quality = "POOR"
    elif in_zone and lifecycle == "ENTRY_READY" and rr is not None:
        quality = "IDEAL"
    elif (estimated_rr or 0) >= minimum_rr:
        quality = "ACCEPTABLE"
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
        "opportunityType": str(candidate.get("setup_type") or ""),
        "route": route, "direction": direction,
        "confirmationLevel": trigger,
        "confirmationSource": "CLOSED_CANDLE",
        "entryZone": {"low": low, "high": high}, "entryZoneLabel": zone_label,
        "entryQuality": quality, "tacticalStop": stop,
        "targets": targets, "estimatedRR": estimated_rr,
        "executableRR": rr, "riskReward": rr,
        "rrPassed": rr is not None and rr >= minimum_rr,
        "requiredEntryPriceForMinRR": required_entry_for_rr(
            target=target, stop=stop, minimum_rr=minimum_rr),
        "chaseLimit": chase, "canEnter": (
            quality == "IDEAL" and lifecycle == "ENTRY_READY" and rr is not None),
        "blockedReasons": list(candidate.get("reason_codes") or []),
        "setupState": setup_state,
        "active": raw_state not in TERMINAL_SETUP_STATES,
    }


def _setup_score(item: dict, bias: str) -> int:
    rr = float(item.get("executableRR") or item.get("estimatedRR") or 0)
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
    decision_health = (data.get("decision_health_state") or
                       evaluate_decision_health(data))
    normalized = data.get("normalized_analysis") or {}
    signal_score = _number((data.get("decision") or {}).get("signal_score"))
    closed_candle = ((data.get("closed_candles") or {}).get("15M") or {})
    closed_available = (bool(closed_candle.get("available")) if closed_candle else
                        bool(normalized.get("lastClosedCandleTimestamp") and
                             normalized.get("lastClosedCandlePrice") is not None))
    closed_error = closed_candle.get("error_reason")
    opportunity_engine = data.get("entry_opportunity_engine") or {}
    raw_opportunities = opportunity_engine.get("opportunities") or []
    dynamic_profit = data.get("dynamic_profit_protection") or {}
    break_lifecycle = data.get("break_lifecycle_engine") or {}
    fake_recovery = data.get("fake_breakout_recovery") or {}
    current = _number(health.get("currentPrice"))
    all_candidates = [_candidate_view(item, current, minimum_rr)
                      for item in final.get("signalCandidates") or []]
    archived_candidates = [item for item in all_candidates if not item["active"]]
    candidates = [item for item in all_candidates if item["active"]]
    behavior_state = data.get("market_behavior_engine") or {}
    rejection = data.get("wick_rejection_engine") or behavior_state.get("wick_rejection") or {}
    market_bias = str(decision_health.get("marketBias") or
                      behavior_state.get("market_bias") or
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
    candle_time = str(closed_candle.get("close_time") or
                      normalized.get("lastClosedCandleTimestamp") or "")
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
    entry_confirmation = str(decision_health.get("entryConfirmation") or
                             "BLOCKED_BY_DATA")
    stale = not bool(health.get("healthy")) or entry_confirmation != "READY"
    behavior = str(behavior_state.get("market_behavior") or "RANGE")
    rr_ok = bool(selected.get("rrPassed")) if selected else False
    confirmation_closed = (trigger_level is None or
                           (confirmation or {}).get("status") == "CLOSED_CONFIRMED")
    can_enter = (bool(final.get("canEnter")) and rr_ok and not stale
                  and closed_available and confirmation_closed)
    if str(decision_health.get("defenseState") or "") == "BROKEN_CONFIRMED":
        can_enter = False
    conflict = str(rejection.get("momentum_price_conflict") or "NONE")
    rejection_state = str(rejection.get("wick_rejection_state") or "NO_SIGNIFICANT_REJECTION")
    rejection_breakout = str(rejection.get("breakout_state") or "NONE")
    if (direction == "LONG" and rejection_state == "REPEATED_UPPER_WICK_REJECTION"
            and rejection_breakout != "BREAKOUT_CONFIRMED"):
        can_enter = False
    if (direction == "SHORT" and rejection_state == "REPEATED_LOWER_WICK_REJECTION"
            and rejection_breakout != "BREAKOUT_CONFIRMED"):
        can_enter = False
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
    if not can_enter and fake_recovery.get("active"):
        recovery_action = fake_recovery.get("nextAction") or {}
        recovery_trigger = _number(recovery_action.get("triggerLevel"))
        recovery_direction = str(fake_recovery.get("oppositeDirection") or direction)
        if recovery_trigger is not None:
            canonical_trigger = {
                "setupId": f"FBR-{fake_recovery.get('sourceFailureId')}",
                "direction": recovery_direction, "level": recovery_trigger,
                "timeframe": "15M",
                "condition": ("closeAbove" if recovery_direction == "LONG"
                              else "closeBelow"),
                "source": "CLOSED_CANDLE", "sourceCandleTime": candle_time,
                "status": "PENDING",
                "label": (f"15M 收盤{'站穩' if recovery_direction == 'LONG' else '跌破'} "
                          f"{recovery_trigger:.2f}；確認後仍須通過位置、停損與 RR 閘門"),
            }
    if entry_confirmation != "READY":
        canonical_trigger = {
            "setupId": selected.get("setupId") if selected else None,
            "direction": direction, "timeframe": "15M",
            "condition": "waitForLatestClosedCandle", "status": "PENDING",
            "source": "CLOSED_CANDLE", "sourceCandleTime": candle_time,
            "label": "等待最新一根 15M K 棒正式收盤；取得後重新計算進場條件",
        }
    primary_reason = str(final.get("humanSummary") or "等待條件一致")
    if entry_confirmation == "WAIT_15M_CLOSE":
        primary_reason = "市場方向保留，但最新15M收盤暫缺，暫停新進場。"
    elif entry_confirmation == "BLOCKED_BY_DATA":
        primary_reason = "行情資料不足，等待最新15M收盤後再判斷。"
    elif stale:
        primary_reason = "行情資料延遲，等待最新資料確認。"
    elif direction == "LONG" and behavior == "SLOW_BEARISH_DRIFT":
        primary_reason = "大方向仍偏多，但15M正在緩步下降，暫停追多並等待止跌。"
    elif direction == "LONG" and behavior in {
            "STRONG_DECLINE", "REVERSAL_WARNING", "REVERSAL_CONFIRMED"}:
        primary_reason = "15M價格行為已轉弱，暫停新的多單進場。"
    elif selected and not rr_ok:
        primary_reason = "方向可能正確，但目前進場盈虧比不合格。"
    elif conflict == "BULLISH_MOMENTUM_BUT_PRICE_REJECTED":
        primary_reason = "多方動能正在恢復，但上方連續出現賣壓拒絕，等待15M實體突破拒絕區。"
    elif conflict == "BEARISH_MOMENTUM_BUT_PRICE_SUPPORTED":
        primary_reason = "空方動能正在增強，但下方連續出現承接，等待15M實體跌破支撐拒絕區。"
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
    raw_positions = list(position.get("positions") or [])
    known = bool(position.get("has_position") and (
        any(float(item.get("position_size") or 0) != 0 for item in raw_positions)
        or float(position.get("position_size") or 0) != 0))
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
        if (rejection_state == "REPEATED_UPPER_WICK_REJECTION"
                and rejection_breakout != "BREAKOUT_CONFIRMED"
                and normalized_position_action == "HOLD"):
            management_mode = "TAKE_PROFIT_WATCH"
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
    if conflict != "NONE":
        reason_codes.append("MOMENTUM_PRICE_CONFLICT")
    reason_codes = list(dict.fromkeys(reason_codes))[:3]
    setup_state = str(selected.get("setupState") or "WATCHING")
    primary_opportunity = opportunity_engine.get("bestReachableOpportunity") or {}
    preferred_route = str(primary_opportunity.get("type") or "") or (
        "PULLBACK" if best_pullback and
        (best_pullback.get("riskReward") or -1) >
        ((best_breakout or {}).get("riskReward") or -1)
        else "BREAKOUT" if best_breakout else None)
    if known and not raw_positions:
        raw_positions = [{
            "position_id": str(position.get("position_id") or "actual-position"),
            "side": position_side, "entry_price": actual_entry,
            "position_size": _number(position.get("position_size")),
            "position_class": str(position.get("position_class") or "CORE"),
            "stop_loss": position.get("stop_loss"),
        }]
    profit_by_id = {str(item.get("position_id") or ""): item
                    for item in dynamic_profit.get("positions") or []}
    per_position = []
    for item in raw_positions:
        pid = str(item.get("position_id") or "")
        profit = profit_by_id.get(pid) or {}
        item_side = str(item.get("side") or position_side or "").upper()
        item_entry = _number(item.get("entry_price"))
        tactical = (_number(profit.get("profit_protection_level")) or
                    _number(profit.get("hard_risk_stop")) or
                    _number(item.get("stop_loss")))
        per_position.append({
            "positionId": pid, "side": item_side,
            "positionClass": str(item.get("position_class") or "CORE"),
            "actualEntryPrice": item_entry,
            "actualSize": _number(item.get("position_size")),
            "currentPrice": current,
            "unrealizedPnl": (round((current - item_entry) * (1 if item_side == "LONG" else -1), 3)
                              if current is not None and item_entry is not None else None),
            "positionAction": str(profit.get("position_action") or
                                  normalized_position_action or "HOLD"),
            "tacticalDefenseLevel": tactical,
            "structuralInvalidationLevel": _number(
                profit.get("structural_exit_confirmation")) or
                _number(normalized.get("structuralInvalidationLevel")),
            "peakProfit": profit.get("max_unrealized_profit"),
            "givebackRatio": profit.get("profit_giveback_ratio"),
            "targets": profit.get("targets") or [],
        })
    notification_route = "POSITION_MANAGEMENT" if known else "NEW_ENTRY"
    primary_position = per_position[0] if per_position else {}
    completeness_errors = []
    market_bias_value = market_bias
    if not market_bias_value:
        completeness_errors.append("MARKET_BIAS_MISSING")
    if signal_score is None:
        completeness_errors.append("SIGNAL_SCORE_MISSING")
    if not health.get("status"):
        completeness_errors.append("DATA_STATUS_MISSING")
    if not closed_available:
        completeness_errors.append(str(closed_error or "CLOSED_CANDLE_UNAVAILABLE"))
    if known:
        for item in per_position:
            if item.get("actualEntryPrice") is None:
                completeness_errors.append("ACTUAL_ENTRY_MISSING")
            if item.get("tacticalDefenseLevel") is None:
                completeness_errors.append("TACTICAL_DEFENSE_MISSING")
    return {
        "schemaVersion": "canonical-trading-decision-v2",
        "timestamp": decision_timestamp,
        "bias4h": _timeframe_bias(normalized, "4H"),
        "bias1h": _timeframe_bias(normalized, "1H"),
        "behavior15m": behavior_state.get("behavior_15m") or behavior,
        "bearishPressure": ("STRONG" if behavior in {"STRONG_DECLINE", "REVERSAL_CONFIRMED"}
                            else "MODERATE" if behavior in {"SLOW_BEARISH_DRIFT", "REVERSAL_WARNING"}
                            else "WEAK"),
        "wickRejection": rejection,
        "wickRejectionState": rejection_state,
        "wickRejectionScore": rejection.get("wick_rejection_score", 0),
        "wickRejectionZone": rejection.get("wick_rejection_zone"),
        "momentumPriceConflict": conflict,
        "breakoutFailureState": rejection_breakout,
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
        "primaryOpportunityId": opportunity_engine.get("primaryOpportunityId"),
        "alternativeOpportunityIds": opportunity_engine.get("alternativeOpportunityIds") or [],
        "entryOpportunities": opportunity_engine.get("opportunities") or [],
        "bestReachableOpportunity": opportunity_engine.get("bestReachableOpportunity"),
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
        "lastClosedCandleTime": candle_time,
        "lastClosedCandlePrice": ((closed_candle.get("close_price") if closed_candle else
                                   normalized.get("lastClosedCandlePrice"))
                                  if closed_available else None),
        "closedCandle": closed_candle,
        "closedCandleAvailable": closed_available,
        "closedCandleErrorReason": closed_error,
        "marketBias": market_bias,
        "dataHealth": decision_health.get("dataHealth"),
        "entryConfirmation": entry_confirmation,
        "defenseState": decision_health.get("defenseState"),
        "defenseLevel": decision_health.get("defenseLevel"),
        "falseBreakDetected": bool(decision_health.get("falseBreakDetected")),
        "contextClosed15m": decision_health.get("contextClosed15m"),
        "signalScore": signal_score,
        "confidenceGrade": (get_confidence_grade(signal_score)
                            if signal_score is not None else None),
        "dataStatus": health.get("status"),
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
        "breakLifecycle": break_lifecycle,
        "breakQuality": {
            "state": break_lifecycle.get("state"),
            "score": break_lifecycle.get("break_confidence"),
            "followThrough": break_lifecycle.get("follow_through"),
            "fastReclaim": break_lifecycle.get("state") in {
                "FAILED_BREAKDOWN", "FAILED_BREAKOUT"},
            "reclaimScore": break_lifecycle.get("reclaim_confidence"),
        },
        "fakeBreakoutRecovery": fake_recovery,
        "dataStale": stale,
        "newEntryDecision": {
            "action": entry_action, "canEnter": can_enter, "direction": direction,
            "tradeStatus": ("WAIT_DATA_CONFIRMATION" if stale or not closed_available else
                            "WAIT_BEHAVIOR_CONFIRMATION" if direction == "LONG" and
                            behavior in {"SLOW_BEARISH_DRIFT", "STRONG_DECLINE",
                                         "REVERSAL_WARNING", "REVERSAL_CONFIRMED"} else
                            "NO_ENTRY_RR" if selected and not rr_ok else
                            "ENTRY_READY" if can_enter else "WAIT_CONFIRMATION"),
            "selectedSetup": selected or None,
            "primaryOpportunityId": opportunity_engine.get("primaryOpportunityId"),
            "shallowPullback": next((x for x in raw_opportunities
                                     if x.get("type") == "SHALLOW_PULLBACK"), None),
            "deepPullback": next((x for x in raw_opportunities
                                  if x.get("type") == "DEEP_PULLBACK"), None),
            "breakoutRetest": next((x for x in raw_opportunities
                                    if x.get("type") == "BREAKOUT_RETEST"), None),
            "pullbackLong": best_pullback,
            "breakoutLong": best_breakout,
            "preferredRoute": preferred_route,
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
            "tacticalDefense": (primary_position.get("tacticalDefenseLevel")
                                  if known else selected.get("tacticalStop") if selected else None),
            "structuralInvalidation": (primary_position.get("structuralInvalidationLevel")
                                        if known else normalized.get("structuralInvalidationLevel")),
            "structuralInvalidationNote": normalized.get("structuralInvalidationNote") or "",
            "targets": selected.get("targets") if selected else [],
            "perPositionDecisions": per_position,
            "hardRiskStop": ((dynamic_profit.get("positions") or [{}])[0]).get(
                "hard_risk_stop") if dynamic_profit.get("positions") else None,
            "structuralExitConfirmation": ((dynamic_profit.get("positions") or [{}])[0]).get(
                "structural_exit_confirmation") if dynamic_profit.get("positions") else None,
        },
        "notificationRoute": notification_route,
        "decisionCompleteness": {
            "valid": not completeness_errors,
            "errors": list(dict.fromkeys(completeness_errors)),
        },
        "reversalProtection": {
            "cooldownActive": bool(tp_facts),
            "sourceEvents": [fact.get("event_type") for fact in tp_facts],
            "requiresIndependentOppositeConfirmation": True,
        },
    }
