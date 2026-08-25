"""The sole action arbiter for dashboard, replay and Telegram.

Individual engines may describe opportunities, but only this module can grant
trade permission or create a user-facing market notification.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

from app.config import get_settings
from app.engines.data_health_gate import evaluate_data_health
from app.engines.decision_health import evaluate_decision_health
from app.engines.entry_location import classify_entry_location, stop_is_valid
from app.engines.scenario_execution import (
    can_execute_scenario,
    resolve_scenario_validity,
)
from app.engines.unified_decision_state import evaluate_unified_decision

Direction = Literal["LONG", "SHORT", "NEUTRAL"]
FinalAction = Literal["ENTER_LONG", "ENTER_SHORT", "WAIT", "NO_TRADE", "MANAGE_POSITION"]


@dataclass(frozen=True)
class SignalCandidate:
    source: str
    timeframe: Literal["15M", "1H", "4H"]
    direction: Direction
    strength: int
    confidence: int
    reason_codes: list[str] = field(default_factory=list)
    trigger_price: float | None = None
    invalidation_price: float | None = None
    entry_zone: tuple[float, float] | None = None
    chase_limit: float | None = None
    targets: tuple[float, ...] = field(default_factory=tuple)
    expires_at: str | None = None
    scenario_id: str = ""
    scenario_version: int = 1
    lineage_id: str = ""
    setup_type: str = "OTHER"
    risk_reward: float | None = None
    estimated_risk_reward: float | None = None
    level_sources: dict[str, dict] = field(default_factory=dict)
    lifecycle_state: str = "SETUP"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _zone(item: dict) -> tuple[float, float] | None:
    low = _number(item.get("entryZoneLow"))
    high = _number(item.get("entryZoneHigh"))
    return (low, high) if low is not None and high is not None else None


def _lifecycle(status: str) -> str:
    status = str(status or "").upper()
    if status == "ALTERNATIVE_READY":
        return "CONFIRMED"
    if "INVALID" in status:
        return "INVALIDATED"
    if "MISSED" in status or status == "MISS_ENTRY":
        return "MISSED"
    if "EXPIRED" in status or status == "ARCHIVED":
        return "EXPIRED"
    explicit_ready = status in {
        "READY", "ENTRY_READY", "LONG_READY", "SHORT_READY",
        "BREAKOUT_ENTRY_READY", "PULLBACK_ENTRY_READY", "TRIGGERED",
        "ENTRY_TRIGGERED",
    }
    suffixed_ready = status.endswith("_READY") and not status.startswith(
        ("NOT_", "NO_", "WAIT_", "BLOCKED_", "INVALID_", "UN"))
    if explicit_ready or suffixed_ready or status.startswith("ENTRY_READY_"):
        return "ENTRY_READY"
    if "CONFIRMED" in status:
        return "CONFIRMED"
    if "TRIGGER" in status:
        return "TRIGGERED"
    if "WATCH" in status or "APPROACH" in status or "ARMED" in status:
        return "ARMED"
    return "SETUP"


def _level_source(price: float | None, source: str, data: dict,
                  confidence: float = .75) -> dict:
    return {
        "price": price,
        "source": source,
        "created_at": str(data.get("timestamp_utc") or ""),
        "source_candle": str((data.get("normalized_analysis") or {}).get(
            "lastClosedCandleTimestamp") or ""),
        "confidence": confidence,
    }


def collect_signal_candidates(data: dict) -> list[SignalCandidate]:
    """Convert heterogeneous engine output to one auditable candidate schema."""
    candidates: list[SignalCandidate] = []
    assistant = data.get("decision_assistant") or {}
    recovery = data.get("fake_breakout_recovery") or {}
    recovery_active = bool(recovery.get("active")) and str(recovery.get("state")) in {
        "WAIT_CONFIRMATION", "LONG_SETUP_CONFIRMED", "SHORT_SETUP_CONFIRMED"}
    recovery_direction = str(recovery.get("oppositeDirection") or "").upper()
    invalidated_direction = str(recovery.get("invalidatedBreakoutDirection") or "").upper()
    recovery_boost = int(recovery.get("oppositeBiasBoost") or 0)
    setup_ledgers: list[dict] = []
    if recovery_active:
        next_action = recovery.get("nextAction") or {}
        targets = list(next_action.get("targets") or [])
        setup_ledgers.append({
            "setupId": f"FBR-{recovery.get('sourceFailureId')}",
            "lineageId": str(recovery.get("sourceFailureId") or ""),
            "direction": recovery_direction, "status": "WATCHING",
            "signalScore": min(100, 50 + recovery_boost),
            "breakoutTrigger": next_action.get("triggerLevel"),
            "stopPrice": next_action.get("invalidationLevel"),
            "tp1": targets[0] if targets else None,
            "tp2": targets[1] if len(targets) > 1 else None,
            "tp3": targets[2] if len(targets) > 2 else None,
            "expiresAt": recovery.get("expiresAt"),
            "type": "FAKE_BREAKOUT_RECOVERY",
            "passedReasons": [
                str(recovery.get("breakoutFailureState") or ""),
                str(recovery.get("liquiditySweepState") or ""),
            ],
            "missingConditions": ["等待新的15M收盤完成反向確認"],
        })
    opportunity_engine = data.get("entry_opportunity_engine") or {}
    unified_opportunities = list(opportunity_engine.get("opportunities") or [])
    for opportunity in unified_opportunities:
        # Distant legacy anchors remain visible as deep-pullback backups, but
        # cannot compete for the canonical next action.
        if opportunity.get("primary_eligible") is False:
            continue
        zone = opportunity.get("entry_zone") or {}
        setup_ledgers.append({
            "setupId": opportunity.get("opportunity_id"),
            "lineageId": opportunity.get("setup_id"),
            "direction": opportunity.get("side"),
            "status": opportunity.get("state"),
            "signalScore": opportunity.get("opportunity_score"),
            "entryZoneLow": zone.get("lower"), "entryZoneHigh": zone.get("upper"),
            "stopPrice": opportunity.get("tactical_stop"),
            "tp1": opportunity.get("target1"),
            "breakoutTrigger": opportunity.get("trigger_level"),
            "expiresAt": opportunity.get("expires_at"),
            "type": opportunity.get("entry_type") or opportunity.get("type"),
            # Only the post-confirmation, current execution RR can grant entry.
            "riskReward": opportunity.get("executable_rr"),
            "estimatedRR": opportunity.get("estimated_rr"),
            "reasonCodes": opportunity.get("confirmation_evidence") or [],
        })
    continuation = data.get("trend_continuation_engine") or {}
    continuation_candidates = list(continuation.get("candidates") or [])
    if unified_opportunities:
        continuation_candidates = [item for item in continuation_candidates
                                   if "PULLBACK" not in str(item.get("type") or "")
                                   and "RETEST" not in str(item.get("type") or "")]
    setup_ledgers.extend(continuation_candidates)
    breakout = data.get("breakout_setup_manager") or {}
    if not unified_opportunities:
        setup_ledgers.extend(breakout.get("setups") or [])
    active = breakout.get("activeSetup") or {}
    if active and not unified_opportunities:
        setup_ledgers.append(active)
    entry = data.get("entry_engine") or {}
    if entry:
        setup_ledgers.append({
            "setupId": entry.get("setup_id"), "direction": entry.get("direction"),
            "status": entry.get("status"), "signalScore": entry.get("confidence_score"),
            "entryZoneLow": entry.get("entry_zone_low"),
            "entryZoneHigh": entry.get("entry_zone_high"),
            "stopPrice": entry.get("stop_loss"), "tp1": entry.get("take_profit_1"),
            "expiresAt": entry.get("expires_at"), "type": "ENTRY_ENGINE",
        })
    # The same setup is often mirrored by more than one compatibility engine.
    # It is still one market opportunity, not one candidate per status string.
    # Keeping a status in the identity could make one setup simultaneously
    # WAIT and READY and later leak contradictory actions to the UI/outbox.
    lifecycle_priority = {
        "INVALIDATED": 100, "EXPIRED": 90, "MISSED": 80,
        "ENTRY_READY": 70, "CONFIRMED": 60, "TRIGGERED": 50,
        "ARMED": 40, "SETUP": 10,
    }
    setup_ledgers.sort(
        key=lambda row: lifecycle_priority.get(
            _lifecycle(str(row.get("status") or "SETUP")), 0),
        reverse=True,
    )
    seen: set[tuple[str, str]] = set()
    for item in setup_ledgers:
        scenario = str(item.get("setupId") or item.get("setup_id") or "")
        status = str(item.get("status") or "SETUP")
        raw_direction = str(item.get("direction") or "NEUTRAL").upper()
        key = (scenario, raw_direction)
        if not scenario or key in seen:
            continue
        seen.add(key)
        # A confirmed fast reclaim cancels the old break direction for this
        # recovery window.  It does not grant the opposite trade; it only
        # removes stale candidates and boosts fresh opposite-side candidates.
        if recovery_active and raw_direction == invalidated_direction:
            continue
        direction = cast(Direction, raw_direction if raw_direction in {"LONG", "SHORT"}
                         else "NEUTRAL")
        trigger = _number(item.get("breakoutTrigger") or item.get("triggerPrice"))
        invalidation = _number(item.get("stopPrice") or item.get("invalidationPrice"))
        lineage = str(item.get("lineageId") or item.get("lineage_id") or
                      item.get("previousSetupId") or scenario)
        version = int(item.get("scenarioVersion") or item.get("setupVersionNumber") or 1)
        reasons = list(item.get("passedReasons") or item.get("reasonCodes") or [])
        missing = list(item.get("missingConditions") or [])
        assistant_direction = str(assistant.get("direction") or "NEUTRAL").upper()
        signal_score = int(item.get("signalScore") or 50)
        if recovery_active and raw_direction == recovery_direction:
            signal_score = min(100, signal_score + recovery_boost)
        candidates.append(SignalCandidate(
            source=str(item.get("type") or "scenario_engine"), timeframe="15M",
            direction=direction, strength=signal_score,
            confidence=min(100, int(item.get("confidence") or
                                    item.get("signalScore") or 50)
                           + (recovery_boost if recovery_active and
                              raw_direction == recovery_direction else 0)),
            reason_codes=[str(x) for x in reasons + missing], trigger_price=trigger,
            invalidation_price=invalidation, entry_zone=_zone(item),
            chase_limit=_number(item.get("maxChasePrice")),
            targets=tuple(value for value in (
                _number(item.get("tp1")), _number(item.get("tp2")),
                _number(item.get("tp3"))) if value is not None),
            expires_at=str(item.get("expiresAt") or "") or None,
            scenario_id=scenario, scenario_version=version, lineage_id=lineage,
            setup_type=str(item.get("type") or "OTHER"),
            risk_reward=_number(item.get("riskReward")),
            estimated_risk_reward=_number(item.get("estimatedRR")),
            lifecycle_state=_lifecycle(status),
            level_sources={
                "trigger": _level_source(trigger, "15M_confirmed_structure", data),
                "invalidation": _level_source(invalidation, "15M_structure_invalidation", data),
                "entry_zone": _level_source(
                    sum(_zone(item) or (0, 0)) / 2 if _zone(item) else None,
                    "ATR_and_structure_confluence", data),
                "chase_limit": _level_source(_number(item.get("maxChasePrice")),
                                                "entry_zone_plus_ATR_limit", data),
                "target_1": _level_source(_number(item.get("tp1")),
                                            "next_valid_structure_zone", data),
                "target_2": _level_source(_number(item.get("tp2")),
                                            "second_valid_structure_zone", data),
                "target_3": _level_source(_number(item.get("tp3")),
                                            "third_valid_structure_zone", data),
            },
        ))
    if not candidates and assistant:
        zone = assistant.get("entryZone") or {}
        low, high = _number(zone.get("low")), _number(zone.get("high"))
        candidates.append(SignalCandidate(
            source="decision_assistant", timeframe="15M",
            direction=cast(Direction, assistant_direction if assistant_direction in {
                "LONG", "SHORT"} else "NEUTRAL"),
            strength=int(assistant.get("entryQualityScore") or 0),
            confidence=int(assistant.get("entryQualityScore") or 0),
            reason_codes=list(assistant.get("noTradeReasons") or []),
            invalidation_price=_number(assistant.get("invalidation")),
            entry_zone=(low, high) if low is not None and high is not None else None,
            chase_limit=_number(assistant.get("maxChasePrice")),
            targets=tuple(float(value) for value in assistant.get("targets") or []
                          if isinstance(value, (int, float))),
            scenario_id=str(assistant.get("scenarioId") or ""),
            scenario_version=int(assistant.get("scenarioVersion") or 1),
            lineage_id=str(assistant.get("scenarioId") or ""),
            setup_type=str(assistant.get("scenarioType") or "OTHER"),
            risk_reward=_number(assistant.get("rewardRiskRatio")),
            lifecycle_state=_lifecycle(str(assistant.get("tradeState") or "SETUP")),
        ))
    return candidates


def _calibrated_probability(raw_score: int, data: dict) -> float | None:
    calibration = data.get("historical_calibration") or {}
    bucket = next((item for item in calibration.get("buckets") or []
                   if int(item.get("low", -1)) <= raw_score <= int(item.get("high", -1))
                   and int(item.get("sampleSize") or 0) >= 30), None)
    return round(float(bucket["observedSuccessRate"]), 3) if bucket else None


def _summary(reason: str, action: str) -> str:
    messages = {
        "DATA_STALE": "行情資料延遲，暫停產生新交易訊號。",
        "EVENT_BLACKOUT": "接近重大數據時間，暫停新進場。",
        "SPREAD_TOO_HIGH": "目前點差異常，實際交易成本太高。",
        "RR_TOO_LOW": "方向可能正確，但現在的風險報酬不划算。",
        "OVEREXTENDED": "方向仍有優勢，但價格已離理想位置太遠。",
        "TIMEFRAME_CONFLICT": "短線與大方向不一致，先等待結構確認。",
        "STRUCTURE_UNCLEAR": "目前沒有清楚、可執行的市場結構。",
        "POSITION_ACTIVE": "目前先管理既有部位，不建立互相衝突的新交易。",
        "ENTRY_READY": "進場、失效與目標條件均已完成，可依計畫評估執行。",
        "WAIT_CONFIRMATION": "機會仍在，但目前還不到可以下單的條件。",
        "BEHAVIOR_WAIT_PULLBACK": "大方向仍偏多，但15M正在緩步下降，暫停追多並等待止跌確認。",
        "BEHAVIOR_LONG_BLOCK": "15M出現急跌或反轉風險，暫停新的多單進場。",
        "WAIT_15M_CLOSE": "大方向保留，但最新15M收盤待確認，暫停新進場。",
        "WAIT_NEW_STRUCTURE": "高週期方向保留；當前劇本失效，等待新的短線結構與交易機會。",
        "BLOCKED_BY_DATA": "行情資料不足，等待最新已收盤15M後再評估。",
        "SCENARIO_DEFENSE_INVALIDATED": "當前交易劇本的防守已失效；高週期方向保留，重新尋找短線結構。",
        "SCENARIO_INVALIDATED": "當前交易劇本已失效；高週期方向保留，等待重新計算新的進場條件。",
        "DEFENSE_BREAK_WAIT_OPPOSITE_CONFIRMATION": "當前劇本失效，但高週期方向不變；反方向仍須獨立完成回測、確認與風控。",
    }
    return messages.get(reason, "現在沒有足夠優勢，先等待新的市場條件。")


def evaluate_final_decision(data: dict, previous: dict | None = None) -> tuple[dict, list[dict]]:
    """Apply the only permission decision, in strict safety priority order."""
    previous = previous or {}
    base, _legacy_events = evaluate_unified_decision(data, previous)
    candidates = collect_signal_candidates(data)
    assistant = data.get("decision_assistant") or {}
    recovery = data.get("fake_breakout_recovery") or {}
    health = evaluate_data_health(data)
    decision_health = (data.get("decision_health_state") or
                       evaluate_decision_health(data, previous=previous))
    market_bias = str(decision_health.get("marketBias") or "NEUTRAL")
    entry_confirmation = str(decision_health.get("entryConfirmation") or
                             "BLOCKED_BY_DATA")
    defense_state = str(decision_health.get("defenseState") or "INACTIVE")
    settings = get_settings()
    current_price = float((data.get("normalized_analysis") or {}).get("currentPrice") or 0)
    spread = float((data.get("current_price") or {}).get("spread") or 0)
    atr = float((data.get("normalized_analysis") or {}).get("atr15") or 0)
    spread_limit = max(settings.gate_spread_max_abs,
                       settings.gate_spread_max_atr15_mult * atr)
    event = data.get("event_risk") or {}
    position_active = bool((data.get("position_management") or {}).get("has_position")
                           or (data.get("trade_plan_manager") or {}).get("activePlans"))
    behavior = str((data.get("market_behavior_engine") or {}).get(
        "market_behavior") or "RANGE")
    def _selection_rank(item: SignalCandidate) -> tuple[bool, bool, int]:
        zone = item.entry_zone
        in_zone = bool(zone and zone[0] <= current_price <= zone[1])
        executable = (
            item.lifecycle_state == "ENTRY_READY" and in_zone
            and stop_is_valid(item.direction, current_price, item.invalidation_price)
            and item.risk_reward is not None
            and item.risk_reward >= settings.decision_assistant_min_rr
            and health["healthy"]
        )
        return executable, item.lifecycle_state == "ENTRY_READY", item.strength

    selected = max(candidates, key=_selection_rank, default=None)
    # Score and R/R must belong to the selected setup. Assistant summaries may
    # describe a different, higher-scoring but non-executable candidate.
    raw_score = int(selected.strength if selected else
                    assistant.get("entryQualityScore") or 0)
    rr = (selected.risk_reward if selected and selected.risk_reward is not None
          else _number(assistant.get("rewardRiskRatio")))
    selected_zone = selected.entry_zone if selected else None
    effective_chase_limit = selected.chase_limit if selected else None
    if selected and selected_zone and effective_chase_limit is None:
        chase_distance = settings.decision_assistant_missed_entry_atr * max(atr, 0.01)
        effective_chase_limit = (selected_zone[1] + chase_distance
                                 if selected.direction == "LONG"
                                 else selected_zone[0] - chase_distance)
    entry_location = (classify_entry_location(
        selected.direction, current_price, selected_zone[0], selected_zone[1],
        effective_chase_limit)
        if selected and selected_zone else "NO_EXECUTABLE_ZONE")
    valid_stop = (stop_is_valid(selected.direction, current_price, selected.invalidation_price)
                  if selected else False)
    in_executable_zone = entry_location == "IN_EXECUTABLE_ZONE"
    closed_candle_confirmed = bool((data.get("normalized_analysis") or {}).get(
        "lastClosedCandleTimestamp"))
    preliminary_risk_valid = bool(
        not event.get("event_lockout") and not event.get("post_event_wait")
        and spread <= spread_limit and not position_active
        and not (selected and selected.direction == "LONG" and behavior in {
            "STRONG_DECLINE", "REVERSAL_WARNING", "REVERSAL_CONFIRMED"})
        and not (defense_state == "BROKEN_CONFIRMED" and (
            not selected or selected.direction == decision_health.get("side"))))
    execution_gate = (can_execute_scenario(
        direction=selected.direction,
        current_price=current_price,
        invalidation_price=selected.invalidation_price,
        lifecycle_state=selected.lifecycle_state,
        data_health=str(decision_health.get("dataHealth") or health.get("status") or "STALE"),
        entry_confirmation=entry_confirmation,
        closed_candle_confirmed=closed_candle_confirmed,
        in_executable_zone=in_executable_zone,
        risk_valid=preliminary_risk_valid,
        rr_valid=rr is not None and rr >= settings.decision_assistant_min_rr,
        stop_valid=valid_stop,
        expires_at=selected.expires_at,
        evaluated_at=str(data.get("timestamp_utc") or ""),
    ) if selected else {
        "scenarioValidity": ("BLOCKED_BY_DATA" if not health["healthy"]
                             else "PENDING_CONFIRMATION"),
        "executionAllowed": False, "candidateInvalidated": False,
        "scenarioInvalidated": False, "marketBiasChanged": False,
        "checks": {}, "blockedReasons": ["SCENARIO_MISSING"],
    })
    secondary: list[str] = []
    fact_types = {str(item.get("event_type") or "")
                  for item in data.get("signal_facts") or [] if isinstance(item, dict)}
    risk_gate = "PASS"
    action: FinalAction
    if entry_confirmation != "READY":
        action = "NO_TRADE"
        primary = ("WAIT_15M_CLOSE" if entry_confirmation == "WAIT_15M_CLOSE"
                   else "WAIT_NEW_STRUCTURE" if entry_confirmation == "WAIT_NEW_STRUCTURE"
                   else "BLOCKED_BY_DATA")
        risk_gate = ("WAIT" if entry_confirmation == "WAIT_NEW_STRUCTURE"
                     else "DATA_INVALID")
    elif not health["healthy"]:
        action = "NO_TRADE"
        primary, risk_gate = "DATA_STALE", "DATA_INVALID"
    elif bool(event.get("event_lockout") or event.get("post_event_wait")):
        action, primary, risk_gate = "NO_TRADE", "EVENT_BLACKOUT", "RISK_BLOCK"
    elif spread > spread_limit:
        action, primary, risk_gate = "NO_TRADE", "SPREAD_TOO_HIGH", "RISK_BLOCK"
    elif position_active:
        action, primary, risk_gate = "MANAGE_POSITION", "POSITION_ACTIVE", "POSITION_MANAGEMENT"
    elif (selected and selected.direction == "LONG"
          and behavior in {"STRONG_DECLINE", "REVERSAL_WARNING", "REVERSAL_CONFIRMED"}):
        action, primary, risk_gate = "NO_TRADE", "BEHAVIOR_LONG_BLOCK", "RISK_BLOCK"
    elif (selected and selected.direction == "LONG"
          and behavior == "SLOW_BEARISH_DRIFT"):
        action, primary, risk_gate = "WAIT", "BEHAVIOR_WAIT_PULLBACK", "WAIT"
    elif str(assistant.get("regime")) in {"RANGE", "NO_EDGE", "REVERSAL_RISK"}:
        action, primary, risk_gate = "NO_TRADE", "STRUCTURE_UNCLEAR", "NO_TRADE"
    elif (defense_state == "BROKEN_CONFIRMED"
          and (not selected or selected.direction == decision_health.get("side"))):
        action, primary, risk_gate = "NO_TRADE", "SCENARIO_DEFENSE_INVALIDATED", "RISK_BLOCK"
    elif (defense_state == "BROKEN_CONFIRMED" and selected
          and selected.direction != decision_health.get("side")
          and not (fact_types & {"RETEST_REJECTED", "OPPOSITE_SETUP_CONFIRMED"})):
        action, primary, risk_gate = (
            "NO_TRADE", "DEFENSE_BREAK_WAIT_OPPOSITE_CONFIRMATION", "RISK_BLOCK")
    elif execution_gate["scenarioValidity"] in {"INVALIDATED", "STALE"}:
        action, primary, risk_gate = "NO_TRADE", "SCENARIO_INVALIDATED", "RISK_BLOCK"
    elif (selected and selected.lifecycle_state == "ENTRY_READY"
          and in_executable_zone and valid_stop
          and (rr is None or rr < settings.decision_assistant_min_rr)):
        action, primary, risk_gate = "NO_TRADE", "RR_TOO_LOW", "RISK_BLOCK"
    elif (selected and selected.lifecycle_state == "ENTRY_READY"
          and execution_gate["executionAllowed"]):
        if raw_score < 50:
            action, primary, risk_gate = "NO_TRADE", "QUALITY_TOO_LOW", "RISK_BLOCK"
        else:
            action = "ENTER_LONG" if selected.direction == "LONG" else "ENTER_SHORT"
            primary, risk_gate = "ENTRY_READY", "ENTRY_READY"
    elif selected and entry_location in {"CHASE_LONG", "CHASE_SHORT"}:
        action, primary, risk_gate = "WAIT", "OVEREXTENDED", "WAIT_RETEST"
    elif selected and selected.lifecycle_state in {"ENTRY_READY", "CONFIRMED", "WATCHING"}:
        action, primary, risk_gate = "WAIT", "SETUP_CONFIRMED_WAIT_PRICE", "WAIT"
    elif rr is not None and rr < settings.decision_assistant_min_rr:
        action, primary, risk_gate = "NO_TRADE", "RR_TOO_LOW", "RISK_BLOCK"
    else:
        action, primary, risk_gate = "WAIT", "WAIT_CONFIRMATION", "WAIT"
    if str(assistant.get("regime")) == "HTF_BULLISH_LTF_WEAKENING":
        secondary.append("TIMEFRAME_CONFLICT")

    candle_time = str((data.get("normalized_analysis") or {}).get(
        "lastClosedCandleTimestamp") or "")
    scenario_id = selected.scenario_id if selected else str(assistant.get("scenarioId") or "")
    zone = selected.entry_zone if selected else None
    chase_limit = selected.chase_limit if selected else None
    if chase_limit is None and zone and selected:
        chase_limit = zone[1] if selected.direction == "LONG" else zone[0]
    targets = list(selected.targets if selected and selected.targets else
                   tuple(float(value) for value in assistant.get("targets") or []
                         if isinstance(value, (int, float))))
    hysteresis_delta = max(settings.decision_assistant_min_price_delta,
                           atr * settings.decision_assistant_trigger_hysteresis_atr)
    def stable_level(value: float | None) -> float | None:
        if value is None:
            return None
        return round(value / hysteresis_delta) * hysteresis_delta if hysteresis_delta else value
    signature_payload = (
        action, primary, scenario_id, selected.scenario_version if selected else 1,
        market_bias, entry_confirmation, defense_state,
        execution_gate["scenarioValidity"],
        str(assistant.get("regime") or ""),
        tuple(stable_level(v) for v in zone) if zone else None,
        stable_level(selected.invalidation_price) if selected else None,
        stable_level(chase_limit), tuple(stable_level(v) for v in targets),
    )
    signature = hashlib.sha256(repr(signature_payload).encode()).hexdigest()[:24]
    previous_signature = str(previous.get("decisionSignature") or "")
    changed = signature != previous_signature
    version = int(previous.get("decisionVersion") or 0) + (1 if changed else 0)
    decision_id = hashlib.sha256(
        f"{data.get('symbol', 'XAUUSD')}|{version}|{signature}".encode()).hexdigest()[:24]
    state_map = {"ENTER_LONG": "LONG_READY", "ENTER_SHORT": "SHORT_READY",
                 "NO_TRADE": ("WAIT_NEW_STRUCTURE"
                              if primary in {"WAIT_NEW_STRUCTURE", "SCENARIO_INVALIDATED"}
                              else str(base.get("state") or "NO_TRADE")
                              if primary in {"WAIT_15M_CLOSE", "BLOCKED_BY_DATA"}
                              else "NO_TRADE"),
                 "MANAGE_POSITION": "MANAGE_POSITION",
                 "WAIT": str(base.get("state") or "WAIT")}
    major_update = bool(fact_types & {
        "BULLISH_RESTORED", "BEARISH_CONFIRMED", "BREAKOUT_CONFIRMED",
        "BREAKOUT_FAILED", "PULLBACK_ZONE_CREATED", "SCENARIO_INVALIDATED",
        "TARGET_HIT", "POSITION_RISK",
    })
    severity = ("CRITICAL" if primary in {"DATA_STALE", "SPREAD_TOO_HIGH", "EVENT_BLACKOUT"}
                else "ACTION" if action in {"ENTER_LONG", "ENTER_SHORT", "MANAGE_POSITION"}
                else "UPDATE" if major_update else "INFO")
    evaluated_at = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    created_at = (evaluated_at if changed else
                  str(previous.get("decisionCreatedAt") or evaluated_at))
    ready_until = ""
    if action in {"ENTER_LONG", "ENTER_SHORT"}:
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            ready_until = (created_dt + timedelta(
                seconds=settings.entry_ready_max_decision_age_seconds)).isoformat()
        except ValueError:
            ready_until = ""
    candidate_payloads: list[dict] = []
    for item in candidates:
        payload = asdict(item)
        payload.update(resolve_scenario_validity(
            direction=item.direction, current_price=current_price,
            invalidation_price=item.invalidation_price,
            lifecycle_state=item.lifecycle_state,
            data_health=str(decision_health.get("dataHealth") or health.get("status") or "STALE"),
            entry_confirmation=entry_confirmation,
            expires_at=item.expires_at,
            evaluated_at=str(data.get("timestamp_utc") or ""),
        ))
        candidate_payloads.append(payload)
    base.update({
        "engineVersion": "final-decision-v1", "decisionId": decision_id,
        "decisionVersion": version, "decisionSignature": signature,
        "decisionChanged": changed, "finalAction": action, "state": state_map[action],
        "canEnter": action in {"ENTER_LONG", "ENTER_SHORT"},
        "primaryReason": primary, "secondaryReasons": secondary,
        "noTradeReason": primary if action == "NO_TRADE" else "",
        "humanSummary": _summary(primary, action), "riskGate": risk_gate,
        "rawScore": raw_score, "calibratedProbability": _calibrated_probability(raw_score, data),
        "signalCandidates": candidate_payloads,
        "selectedScenarioId": scenario_id,
        "selectedScenarioVersion": selected.scenario_version if selected else 1,
        "selectedLineageId": selected.lineage_id if selected else scenario_id,
        "selectedSetupType": selected.setup_type if selected else "OTHER",
        "selectedLifecycleState": selected.lifecycle_state if selected else "SETUP",
        "direction": selected.direction if selected else "NEUTRAL",
        "currentPrice": current_price,
        "entryZone": ({"low": zone[0], "high": zone[1]} if zone else None),
        "chaseLimit": chase_limit,
        "invalidationPrice": selected.invalidation_price if selected else None,
        "targets": targets,
        "qualityScore": selected.confidence if selected else raw_score,
        "qualityGrade": ("A" if raw_score >= 80 else "B" if raw_score >= 65
                         else "C" if raw_score >= 35 else "D"),
        "sourceCandleCloseTime": candle_time,
        "decisionCreatedAt": created_at, "evaluatedAt": evaluated_at,
        "validUntil": ready_until, "entryReadyValidUntil": ready_until,
        "atr15": atr,
        "sourceDataVersion": int(data.get("version") or 0),
        "priceScenarioVersions": {
            key: (selected.scenario_version if selected else 1) for key, value in {
                "entryZone": zone, "chaseLimit": chase_limit,
                "invalidation": selected.invalidation_price if selected else None,
                "targets": targets,
            }.items() if value is not None and value != []
        },
        "effectiveRR": rr,
        "stateHysteresis": {
            "minimumPriceDelta": round(hysteresis_delta, 3),
            "requiresConfirmedCandle": True,
            "pendingTransition": (data.get("regime_state_machine") or {}).get(
                "livePriceState") == "LIVE_TESTING_RECLAIM",
        },
        "dataAgeSeconds": health.get("dataAgeSeconds"),
        "marketBias": market_bias,
        "dataHealth": decision_health.get("dataHealth"),
        "entryConfirmation": entry_confirmation,
        "scenarioValidity": execution_gate["scenarioValidity"],
        "executionAllowed": execution_gate["executionAllowed"],
        "candidateInvalidated": execution_gate["candidateInvalidated"],
        "scenarioInvalidated": execution_gate["scenarioInvalidated"],
        "marketBiasChanged": execution_gate["marketBiasChanged"],
        "executionGate": execution_gate,
        "defenseState": defense_state,
        "defenseLevel": decision_health.get("defenseLevel"),
        "defenseSide": decision_health.get("side"),
        "confirmationBuffer": decision_health.get("confirmationBuffer"),
        "falseBreakDetected": bool(decision_health.get("falseBreakDetected")),
        "activeLongScenario": decision_health.get("activeLongScenario", "ACTIVE"),
        "activeShortScenario": decision_health.get("activeShortScenario", "ACTIVE"),
        "shortTermStructure": decision_health.get("shortTermStructure", "UNCHANGED"),
        "marketContext": dict(decision_health.get("marketContext") or {}),
        "scenarioState": decision_health.get("scenarioState", "ACTIVE"),
        "scenarioTerminal": bool(decision_health.get("scenarioTerminal")),
        "historicalDefenseLevel": decision_health.get("historicalDefenseLevel"),
        "activeDefenseRole": decision_health.get("activeDefenseRole", "ACTIVE_DEFENSE"),
        "confirmedStrategyEvents": list(
            decision_health.get("confirmedStrategyEvents") or []),
        "reclaimEvent": decision_health.get("reclaimEvent"),
        "pendingNewScenarioId": decision_health.get("pendingNewScenarioId"),
        "searchNextScenario": bool(decision_health.get("searchNextScenario")),
        "nextScenarioCandidates": list(
            decision_health.get("nextScenarioCandidates") or []),
        "notificationSeverity": severity,
    })
    # Compatibility name, same canonical value.  A second resolver here used
    # to let marketDirection disagree with marketBias in the same payload.
    base["marketDirection"] = market_bias
    base["marketDirectionSource"] = "decision_health_state.marketBias"
    base["entrySignal"] = ("READY" if base.get("canEnter") else
                           "PAUSED" if (not health["healthy"] or
                                        execution_gate["scenarioValidity"] in {
                                            "INVALIDATED", "STALE", "BLOCKED_BY_DATA"})
                           else "WAIT")
    if recovery.get("active"):
        base["fakeBreakoutRecovery"] = recovery
        base["nextAction"] = recovery.get("nextAction") or {}
    ready_directions = {
        candidate.direction for candidate in candidates
        if candidate.lifecycle_state == "ENTRY_READY"
        and candidate.direction in {"LONG", "SHORT"}
        and candidate.entry_zone is not None
        and candidate.entry_zone[0] <= current_price <= candidate.entry_zone[1]
        and stop_is_valid(candidate.direction, current_price,
                          candidate.invalidation_price)
        and candidate.risk_reward is not None
        and candidate.risk_reward >= settings.decision_assistant_min_rr
        and health["healthy"]
    }
    if len(ready_directions) > 1:
        base.update({"finalAction": "NO_TRADE", "state": "NO_TRADE",
                     "canEnter": False, "primaryReason": "TIMEFRAME_CONFLICT",
                     "noTradeReason": "TIMEFRAME_CONFLICT", "riskGate": "RISK_BLOCK",
                     "humanSummary": _summary("TIMEFRAME_CONFLICT", "NO_TRADE")})
    from app.engines.decision_consistency import fail_closed, validate_final_decision
    consistency_errors = validate_final_decision(base)
    if consistency_errors:
        base = fail_closed(base, consistency_errors)
        safe_signature = hashlib.sha256(repr((
            "NO_TRADE", "SYSTEM_DECISION_CONFLICT", scenario_id,
            candle_time, tuple(consistency_errors),
        )).encode()).hexdigest()[:24]
        safe_decision_id = hashlib.sha256(
            f"{data.get('symbol', 'XAUUSD')}|{safe_signature}".encode()
        ).hexdigest()[:32]
        changed = safe_decision_id != str(previous.get("decisionId") or "")
        version = int(previous.get("decisionVersion") or 0) + (1 if changed else 0)
        base.update({"decisionSignature": safe_signature,
                     "decisionId": safe_decision_id,
                     "decisionChanged": changed,
                     "decisionVersion": version})
    # This field is consumed by both the dashboard and Telegram.  Derive it
    # only after the consistency validator has had the final word so a
    # fail-closed decision can never retain a stale READY label.
    base["entrySignal"] = (
        "READY" if bool(base.get("canEnter"))
        else "PAUSED" if str(base.get("state") or "") in {"DATA_STALE", "NO_TRADE"}
        else "WAIT"
    )
    # Event publication must use the post-validation canonical result. Keeping
    # the pre-validation locals could emit ENTRY_READY after fail-closed.
    action = cast(FinalAction, str(base.get("finalAction") or "NO_TRADE"))
    primary = str(base.get("primaryReason") or "SYSTEM_DECISION_CONFLICT")
    events: list[dict] = []
    event_types: list[str] = []
    if changed:
        if action in {"ENTER_LONG", "ENTER_SHORT"}:
            event_types.append("ENTRY_READY")
        elif primary in {"SCENARIO_INVALIDATED", "SCENARIO_DEFENSE_INVALIDATED"}:
            event_types.append("SCENARIO_INVALIDATED")
        elif primary == "OVEREXTENDED":
            event_types.append("WAIT_RETEST")
        if action == "MANAGE_POSITION":
            event_types.append("POSITION_HOLD")
    meaningful_facts = {
        "DOUBLE_SWEEP_CONFIRMED", "FAILED_BREAKOUT", "FAILED_BREAKDOWN",
        "LIQUIDITY_SWEEP_HIGH", "LIQUIDITY_SWEEP_LOW", "SETUP_INVALIDATED",
        "SETUP_EXPIRED", "MISSED_ENTRY", "POSITION_DEFEND", "POSITION_EXIT",
        "STOP_TRIGGERED", "TP1_HIT", "TP2_HIT", "TP3_HIT", "TRAIL_UPDATED",
        "REGIME_MAJOR_CHANGE", "WHIPSAW_DETECTED",
        "MARKET_BEHAVIOR_CHANGED",
        "POSITION_WARNING", "SOFT_INVALIDATION_PENDING", "SOFT_INVALIDATED",
        "HARD_INVALIDATED", "POSITION_RECOVERED", "POSITION_DATA_RISK",
        "BREAK_PENDING", "BREAK_CONFIRMED", "RECLAIM_FAILED",
        "LIQUIDITY_SWEEP_CANDIDATE", "PROFIT_GIVEBACK_ALERT",
        "PROFIT_STATE_CHANGED",
        "FAKE_BREAKOUT_CONFIRMED", "OPPOSITE_SETUP_CONFIRMED",
        "RECOVERY_SETUP_INVALIDATED",
        "DEFENSE_TEST", "DEFENSE_RECLAIMED", "DEFENSE_HELD",
        "DEFENSE_BROKEN_CONFIRMED",
        "DATA_DELAYED", "DATA_STALE", "DATA_RECOVERED",
    }
    # Lifecycle facts are state transitions in their own right. They must not
    # disappear merely because the high-level ENTER/WAIT/MANAGE action stayed
    # unchanged during the same evaluation cycle.
    event_types.extend(sorted(fact_types & meaningful_facts))
    if candle_time and candle_time != str(previous.get("sourceCandleCloseTime") or ""):
        event_types.append("CANDLE_FINALIZED")
    for canonical_event_type in dict.fromkeys(event_types):
        canonical_fact: dict[str, Any] = next(
            (fact for fact in data.get("signal_facts") or []
             if fact.get("event_type") == canonical_event_type), {})
        published_action = str(base["finalAction"])
        published_state = state_map.get(published_action, str(base.get("state") or "NO_TRADE"))
        event_id = hashlib.sha256(
            f"{decision_id}|{canonical_event_type}|{candle_time}".encode()).hexdigest()[:32]
        events.append({
            "eventId": event_id, "event_type": canonical_event_type,
            "eventVersion": 1, "snapshotId": str(data.get("version") or ""),
            "positionId": str(canonical_fact.get("positionId") or
                              (data.get("position_management") or {}).get("position_id") or ""),
            "eventTimeUtc": candle_time or evaluated_at, "generatedAtUtc": evaluated_at,
            "previousState": str(previous.get("state") or "WAIT"),
            "currentState": published_state, "transitionReason": base["humanSummary"],
            "marketState": str(assistant.get("regime") or ""),
            "finalDecision": base["finalAction"], "finalAction": base["finalAction"],
            "canEnter": bool(base.get("canEnter")), "currentPrice": current_price,
            "entryZone": base.get("entryZone"), "chaseLimit": base.get("chaseLimit"),
            "stopLoss": base.get("invalidationPrice"), "targets": base.get("targets") or [],
            "candleCloseTime": candle_time,
            "latestClosedCandlePrice": (data.get("normalized_analysis") or {}).get(
                "lastClosedCandlePrice"),
            "calculatedAt": str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat()),
            "dataVersion": int(data.get("version") or 0), "direction": (
                "LONG" if published_action == "ENTER_LONG" else
                "SHORT" if published_action == "ENTER_SHORT" else "NONE"),
            "setupId": scenario_id, "decisionId": decision_id,
            "decisionVersion": version, "notificationSeverity": base["notificationSeverity"],
            "primaryReason": base["primaryReason"], "humanSummary": base["humanSummary"],
            "scenarioVersion": base.get("selectedScenarioVersion"),
            "lineageId": base.get("selectedLineageId"),
            "qualityScore": base.get("qualityScore"), "qualityGrade": base.get("qualityGrade"),
            "effectiveRR": base.get("effectiveRR"),
            "signalFacts": list(data.get("signal_facts") or []),
            "marketBehavior": behavior,
            "marketBehaviorState": data.get("market_behavior_engine") or {},
            "marketDirection": base.get("marketDirection"),
            "entrySignal": base.get("entrySignal"),
            "marketBias": base.get("marketBias"),
            "dataHealth": base.get("dataHealth"),
            "entryConfirmation": base.get("entryConfirmation"),
            "scenarioValidity": base.get("scenarioValidity"),
            "executionAllowed": base.get("executionAllowed"),
            "candidateInvalidated": base.get("candidateInvalidated"),
            "scenarioInvalidated": base.get("scenarioInvalidated"),
            "marketBiasChanged": base.get("marketBiasChanged"),
            "defenseState": base.get("defenseState"),
            "defenseLevel": base.get("defenseLevel"),
            "defenseSide": base.get("defenseSide"),
            "confirmationBuffer": base.get("confirmationBuffer"),
            "primaryTriggerId": scenario_id,
            "notificationEligible": True,
            **{key: canonical_fact.get(key) for key in (
                "tradePlanId", "tradeThesis", "warningLevel", "hardInvalidation",
                "emergencyStop", "reclaimDeadline", "reasonCode", "closedPrice",
                "previousBehavior", "behaviorConfidence", "marketBias",
                "breakLifecycle", "positionProfitDecision", "triggerLevel",
                "opportunityId", "defenseSide", "confirmationBuffer",
                "dataIncidentId", "dataHealthEventKey", "previousDataHealth",
                "currentDataHealth", "closedBarTimestamp")
               if canonical_fact.get(key) is not None},
        })
        if canonical_fact.get("fakeBreakoutRecovery"):
            events[-1]["fakeBreakoutRecovery"] = canonical_fact["fakeBreakoutRecovery"]
            events[-1]["nextAction"] = canonical_fact["fakeBreakoutRecovery"].get(
                "nextAction") or {}
    base["events"] = events
    return base, events
