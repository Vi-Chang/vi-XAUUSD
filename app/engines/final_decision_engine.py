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
from app.engines.entry_location import classify_entry_location, stop_is_valid
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
    level_sources: dict[str, dict] = field(default_factory=dict)
    lifecycle_state: str = "SETUP"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _zone(item: dict) -> tuple[float, float] | None:
    low = _number(item.get("entryZoneLow"))
    high = _number(item.get("entryZoneHigh"))
    return (low, high) if low is not None and high is not None else None


def _lifecycle(status: str) -> str:
    if "READY" in status:
        return "ENTRY_READY"
    if "CONFIRMED" in status:
        return "CONFIRMED"
    if "TRIGGER" in status:
        return "TRIGGERED"
    if "WATCH" in status or "APPROACH" in status or "ARMED" in status:
        return "ARMED"
    if "MISSED" in status:
        return "MISSED"
    if "EXPIRED" in status:
        return "EXPIRED"
    if "INVALID" in status:
        return "INVALIDATED"
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
    setup_ledgers: list[dict] = []
    continuation = data.get("trend_continuation_engine") or {}
    setup_ledgers.extend(continuation.get("candidates") or [])
    breakout = data.get("breakout_setup_manager") or {}
    setup_ledgers.extend(breakout.get("setups") or [])
    active = breakout.get("activeSetup") or {}
    if active:
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
    seen: set[tuple[str, str]] = set()
    for item in setup_ledgers:
        scenario = str(item.get("setupId") or item.get("setup_id") or "")
        status = str(item.get("status") or "SETUP")
        key = (scenario, status)
        if not scenario or key in seen:
            continue
        seen.add(key)
        raw_direction = str(item.get("direction") or "NEUTRAL").upper()
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
        candidates.append(SignalCandidate(
            source=str(item.get("type") or "scenario_engine"), timeframe="15M",
            direction=direction, strength=int(item.get("signalScore") or 50),
            confidence=int(item.get("confidence") or item.get("signalScore") or 50),
            reason_codes=[str(x) for x in reasons + missing], trigger_price=trigger,
            invalidation_price=invalidation, entry_zone=_zone(item),
            chase_limit=_number(item.get("maxChasePrice")),
            targets=tuple(value for value in (
                _number(item.get("tp1")), _number(item.get("tp2")),
                _number(item.get("tp3"))) if value is not None),
            expires_at=str(item.get("expiresAt") or "") or None,
            scenario_id=scenario, scenario_version=version, lineage_id=lineage,
            setup_type=str(item.get("type") or "OTHER"),
            risk_reward=_number(item.get("riskReward")), lifecycle_state=_lifecycle(status),
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
    }
    return messages.get(reason, "現在沒有足夠優勢，先等待新的市場條件。")


def evaluate_final_decision(data: dict, previous: dict | None = None) -> tuple[dict, list[dict]]:
    """Apply the only permission decision, in strict safety priority order."""
    previous = previous or {}
    base, _legacy_events = evaluate_unified_decision(data, previous)
    candidates = collect_signal_candidates(data)
    assistant = data.get("decision_assistant") or {}
    health = evaluate_data_health(data)
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
        executable = (item.lifecycle_state == "ENTRY_READY" and in_zone
                      and stop_is_valid(item.direction, current_price,
                                        item.invalidation_price))
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
    secondary: list[str] = []
    fact_types = {str(item.get("event_type") or "")
                  for item in data.get("signal_facts") or [] if isinstance(item, dict)}
    risk_gate = "PASS"
    if not health["healthy"]:
        action: FinalAction = "NO_TRADE"
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
    elif (selected and selected.lifecycle_state == "ENTRY_READY"
          and entry_location == "IN_EXECUTABLE_ZONE" and valid_stop):
        if rr is None or rr < settings.decision_assistant_min_rr:
            action, primary, risk_gate = "NO_TRADE", "RR_TOO_LOW", "RISK_BLOCK"
        elif raw_score < 50:
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
                 "NO_TRADE": "NO_TRADE", "MANAGE_POSITION": "MANAGE_POSITION",
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
    base.update({
        "engineVersion": "final-decision-v1", "decisionId": decision_id,
        "decisionVersion": version, "decisionSignature": signature,
        "decisionChanged": changed, "finalAction": action, "state": state_map[action],
        "canEnter": action in {"ENTER_LONG", "ENTER_SHORT"},
        "primaryReason": primary, "secondaryReasons": secondary,
        "noTradeReason": primary if action == "NO_TRADE" else "",
        "humanSummary": _summary(primary, action), "riskGate": risk_gate,
        "rawScore": raw_score, "calibratedProbability": _calibrated_probability(raw_score, data),
        "signalCandidates": [asdict(item) for item in candidates],
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
            key: selected.scenario_version for key, value in {
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
        "notificationSeverity": severity,
    })
    ready_directions = {
        candidate.direction for candidate in candidates
        if candidate.lifecycle_state == "ENTRY_READY"
        and candidate.direction in {"LONG", "SHORT"}
        and candidate.entry_zone is not None
        and candidate.entry_zone[0] <= current_price <= candidate.entry_zone[1]
        and stop_is_valid(candidate.direction, current_price,
                          candidate.invalidation_price)
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
    # Event publication must use the post-validation canonical result. Keeping
    # the pre-validation locals could emit ENTRY_READY after fail-closed.
    action = cast(FinalAction, str(base.get("finalAction") or "NO_TRADE"))
    primary = str(base.get("primaryReason") or "SYSTEM_DECISION_CONFLICT")
    events: list[dict] = []
    event_types: list[str] = []
    if changed:
        if primary == "DATA_STALE":
            event_types.append("DATA_STALE")
        elif str(previous.get("primaryReason") or "") == "DATA_STALE":
            event_types.append("DATA_RECOVERED")
        if action in {"ENTER_LONG", "ENTER_SHORT"}:
            event_types.append("ENTRY_READY")
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
    }
    # Lifecycle facts are state transitions in their own right. They must not
    # disappear merely because the high-level ENTER/WAIT/MANAGE action stayed
    # unchanged during the same evaluation cycle.
    event_types.extend(sorted(fact_types & meaningful_facts))
    if candle_time and candle_time != str(previous.get("sourceCandleCloseTime") or ""):
        event_types.append("CANDLE_FINALIZED")
    for canonical_event_type in dict.fromkeys(event_types):
        canonical_fact = next((fact for fact in data.get("signal_facts") or []
                               if fact.get("event_type") == canonical_event_type), {})
        published_action = str(base["finalAction"])
        published_state = state_map.get(published_action, str(base.get("state") or "NO_TRADE"))
        event_id = hashlib.sha256(
            f"{decision_id}|{canonical_event_type}|{candle_time}".encode()).hexdigest()[:32]
        events.append({
            "eventId": event_id, "event_type": canonical_event_type,
            "eventVersion": 1, "snapshotId": str(data.get("version") or ""),
            "positionId": str((data.get("position_management") or {}).get("position_id") or ""),
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
            "notificationEligible": True,
            **{key: canonical_fact.get(key) for key in (
                "tradePlanId", "tradeThesis", "warningLevel", "hardInvalidation",
                "emergencyStop", "reclaimDeadline", "reasonCode", "closedPrice",
                "previousBehavior", "behaviorConfidence", "marketBias")
               if canonical_fact.get(key) is not None},
        })
    base["events"] = events
    return base, events
