"""Single authoritative, transition-driven decision state for web and push."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from app.config import get_settings
from app.engines.entry_engine import EntryPlan, validate_executable_plan
from app.engines.trigger_lifecycle import resolve_next_trigger


@dataclass(frozen=True)
class UnifiedDecision:
    state: str = "WAIT"
    direction: str = "NONE"
    action: str = "等待"
    confidence: int = 0
    reason: str = "等待新的市場資料"
    flat_action: str = "等待明確價位與已收盤 K 線確認"
    long_manage: str = "若持有多單：依最新防守價管理風險"
    short_manage: str = "若持有空單：依最新防守價管理風險"
    next_trigger: float | None = None
    confirmation: str = "等待 15 分鐘 K 線收盤確認"
    quote_time: str = ""
    last_closed_candle_time: str = ""
    calculated_at: str = ""
    source_price: float = 0.0
    market_state: str = ""
    version: int = 0
    last_event: str = ""


def _level(normalized: dict, kind: str) -> float | None:
    item = next(
        (
            value
            for value in normalized.get("confirmationLevels", [])
            if value.get("kind") == kind and value.get("timeframe") == "15M"
        ),
        None,
    )
    return (
        float(item["price"])
        if item and isinstance(item.get("price"), (int, float))
        else None
    )


def evaluate_unified_decision(
    data: dict, previous: dict | None = None
) -> tuple[dict, list[dict]]:
    previous = previous or {}
    normalized = data.get("normalized_analysis") or {}
    entry = data.get("entry_engine") or {}
    decision = data.get("market_decision") or data.get("decision") or {}
    price = float(normalized.get("currentPrice") or 0)
    quote_time = str(
        normalized.get("marketDataTimestamp") or data.get("snapshot_ts") or ""
    )
    candle_time = str(normalized.get("lastClosedCandleTimestamp") or "")
    calculated = str(data.get("timestamp_utc") or "")
    support, resistance = (
        _level(normalized, "support"),
        _level(normalized, "resistance"),
    )
    stale = normalized.get("marketDataStatus") != "GOOD" or not normalized.get(
        "consistencyValid", True
    )
    status, direction = (
        str(entry.get("status") or "NO_SETUP"),
        str(entry.get("direction") or "NONE"),
    )
    if status == "ENTRY_TRIGGERED":
        try:
            executable, entry_error = validate_executable_plan(EntryPlan(**entry))
        except (TypeError, ValueError):
            executable, entry_error = False, "進場計畫欄位格式錯誤"
        if not executable:
            status = "ENTRY_READY"
            entry = {
                **entry,
                "status": status,
                "missing_condition": f"一致性檢查未通過：{entry_error}",
            }
    action = str(decision.get("action") or "WATCH")
    directional = data.get("directional_alert") or {}
    false_breakout = directional.get("event_type") == "FALSE_BREAKOUT"
    state = "WAIT"
    if stale:
        state = "DATA_STALE"
    elif status == "ENTRY_TRIGGERED":
        state = "LONG_READY" if direction == "LONG" else "SHORT_READY"
    elif status in ("ENTRY_READY", "SETUP_WATCH"):
        state = "LONG_WATCH" if direction == "LONG" else "SHORT_WATCH"
    elif status in ("INVALIDATED", "EXITED"):
        state = "INVALIDATED"
    elif any(
        (data.get(key) or {}).get("lifecycle_status")
        in ("MISSED_ENTRY_WAIT_RETEST", "EXPIRED")
        for key in ("long_scenario", "short_scenario")
    ):
        state = "MISSED_ENTRY"
    elif action in ("PREPARE_LONG", "LONG"):
        state, direction = "LONG_WATCH", "LONG"
    elif action in ("PREPARE_SHORT", "SHORT"):
        state, direction = "SHORT_WATCH", "SHORT"
    recovery_continues = (
        previous.get("state") == "BULLISH_RECOVERY"
        and (
            support is None
            or float(normalized.get("lastClosedCandlePrice") or price) >= support
        )
        and state in ("WAIT", "INVALIDATED")
    )
    if false_breakout or recovery_continues:
        state, direction = "BULLISH_RECOVERY", "LONG"

    # Executable decisions must survive realistic friction, not only chart-mid RR.
    settings = get_settings()
    quote = data.get("current_price") or {}
    spread = max(0.0, float(quote.get("spread") or 0))
    execution_cost = spread + max(0.0, settings.execution_slippage_usd) \
        + max(0.0, settings.execution_fees_usd)
    suggested = entry.get("suggested_entry")
    stop = entry.get("stop_loss")
    tp1 = entry.get("take_profit_1")
    net_rr = None
    if all(isinstance(value, (int, float)) for value in (suggested, stop, tp1)):
        risk = abs(float(suggested) - float(stop))
        reward = abs(float(tp1) - float(suggested))
        if risk > 0:
            net_rr = max(0.0, reward - execution_cost) / (risk + execution_cost)
            if state.endswith("READY") and (
                net_rr < settings.setup_min_rr1
                or execution_cost / risk > settings.execution_max_cost_risk_ratio
            ):
                state = "LONG_WATCH" if direction == "LONG" else "SHORT_WATCH"
                entry = {**entry, "missing_condition":
                         f"扣除點差與滑價後賺賠比僅 {net_rr:.2f}，等待更好的價格"}

    confidence = int(
        entry.get("confidence_score") or decision.get("evidence_score") or 0
    )
    event_status = str(normalized.get("eventDataStatus") or "FAILED")
    market_mode = str(normalized.get("marketRegime") or "range")
    if event_status != "GOOD":
        confidence = min(confidence, 55)
    if market_mode == "range" and state.endswith("READY"):
        confidence = min(confidence, 65)
    latest_closed_raw = normalized.get("lastClosedCandlePrice")
    latest_closed = (float(latest_closed_raw)
                     if isinstance(latest_closed_raw, (int, float)) else None)
    reason = str(
        entry.get("missing_condition") or decision.get("reason") or "等待條件一致"
    )
    flat_action = reason
    if state == "DATA_STALE":
        direction, action, confidence = "NONE", "暫停交易", 0
        reason = flat_action = "行情或 K 線資料已過期，等待資料恢復"
    elif state == "MISSED_ENTRY":
        action = "禁止追價"
        flat_action = "原進場區已錯過；等待價格回踩新的確認區"
    elif state == "INVALIDATED":
        action = "舊劇本已失效"
        flat_action = "舊劇本已取消，依最新結構等待新劇本"
    elif state == "BULLISH_RECOVERY":
        action = "行情轉強，等待多方確認"
        reason = "空方劇本已失效，價格重新站回關鍵位"
        flat_action = "暫不追價，等待回踩新支撐或 15M 收盤確認"
    elif state.endswith("READY"):
        action = "可依完整風控計畫評估"
        flat_action = f"{entry.get('trigger_timeframe') or '15M'} 收盤條件已完成"
    elif state.endswith("WATCH"):
        action = "觀察中"
    else:
        direction, action = "NONE", "等待"

    tracker = data.get("virtual_profit_tracker") or {}
    tp1_reached = any(
        event.get("event_type") == "TP1" for event in tracker.get("events") or []
    )
    if tracker.get("active"):
        state = "LONG_MANAGE" if tracker.get("direction") == "LONG" else "SHORT_MANAGE"
        action = "管理既有條件式部位"
        flat_action = "未持倉：原進場時機已過，禁止追價；等待新的回踩或結構確認"
    if tp1_reached:
        action = "第一目標已到，管理獲利"
        flat_action = "未持倉：禁止追價，等待新的回踩確認區"
    exit_plans = (data.get("hypothetical_exit_advisor") or {}).get("plans") or {}
    long_plan, short_plan = exit_plans.get("LONG") or {}, exit_plans.get("SHORT") or {}
    long_manage = (
        f"若持有多單：防守 {long_plan.get('defense_price'):.2f}，依序分批止盈"
        if isinstance(long_plan.get("defense_price"), (int, float))
        else "若持有多單：等待最新 15M 防守價"
    )
    short_manage = (
        f"若持有空單：{short_plan.get('defense_price'):.2f} 防守條件已觸發，依風控規則處理"
        if isinstance(short_plan.get("defense_price"), (int, float))
        and price > float(short_plan["defense_price"])
        else f"若持有空單：防守 {short_plan.get('defense_price'):.2f}，依序分批止盈"
        if isinstance(short_plan.get("defense_price"), (int, float))
        else "若持有空單：等待最新 15M 防守價"
    )
    if (isinstance(long_plan.get("defense_price"), (int, float))
            and price < float(long_plan["defense_price"])):
        long_manage = (f"若持有多單：{long_plan['defense_price']:.2f} "
                       "防守條件已觸發，依風控規則處理")
    trigger_result = resolve_next_trigger(
        resistance=resistance, support=support, latest_closed=latest_closed,
        direction=direction, state=state)
    pending_trigger = trigger_result["next"]
    current = UnifiedDecision(
        state=state,
        direction=direction,
        action=action,
        confidence=confidence,
        reason=reason,
        flat_action=flat_action,
        long_manage=long_manage,
        short_manage=short_manage,
        next_trigger=(pending_trigger.get("level") if pending_trigger else None),
        confirmation=trigger_result["label"],
        quote_time=quote_time,
        last_closed_candle_time=candle_time,
        calculated_at=calculated,
        source_price=price,
        market_state=str(
            normalized.get("marketStateCode") or data.get("market_state") or ""
        ),
        version=int(data.get("version") or previous.get("version") or 0),
    )
    events: list[dict] = []
    old_state, old_price = (
        previous.get("state", "WAIT"),
        float(previous.get("source_price") or price),
    )
    event_types: list[str] = ["STATE_CHANGED"] if old_state != state else []
    previous_completed = {item.get("code") for item in previous.get("completed_events", [])}
    completed_events = []
    if (isinstance(long_plan.get("defense_price"), (int, float))
            and price < float(long_plan["defense_price"])):
        completed_events.append({"code": "LONG_DEFENSE_TRIGGERED",
                                 "level": long_plan["defense_price"]})
    if (isinstance(short_plan.get("defense_price"), (int, float))
            and price > float(short_plan["defense_price"])):
        completed_events.append({"code": "SHORT_DEFENSE_TRIGGERED",
                                 "level": short_plan["defense_price"]})
    event_types.extend(item["code"] for item in completed_events
                       if item["code"] not in previous_completed)
    transition_chain: list[tuple[str, str, str]] = []
    if false_breakout and old_state not in ("FALSE_BREAKOUT", "BULLISH_RECOVERY"):
        transition_chain = [
            (old_state, "SHORT_INVALIDATED", "SHORT_INVALIDATED"),
            ("SHORT_INVALIDATED", "FALSE_BREAKOUT", "FALSE_BREAKOUT"),
            ("FALSE_BREAKOUT", "BULLISH_RECOVERY", "BULLISH_RECOVERY"),
        ]
        event_types = []
    if state == "DATA_STALE" and old_state != state:
        event_types.append("DATA_STALE")
    if old_price < price and price - old_price >= max(
        5.0, float(normalized.get("atr15") or 0) * 0.5
    ):
        event_types.append("PRICE_REBOUND")
    if support is not None and old_price < support <= price:
        event_types.append("KEY_LEVEL_RECLAIMED")
    if resistance is not None and old_price <= resistance < price:
        event_types.append("AWAIT_CLOSE_CONFIRMATION")
    if (
        candle_time
        and candle_time != previous.get("last_closed_candle_time")
        and state.endswith(("WATCH", "READY"))
    ):
        event_types.append("CANDLE_CLOSE_CONFIRMED")
    tracker_event_map = {
        "TP1": "FIRST_TARGET_REACHED",
        "TP2": "SECOND_TARGET_REACHED",
        "TP3": "THIRD_TARGET_REACHED",
        "TRAILING_EXIT": "PROTECTION_EXIT_REACHED",
    }
    event_types.extend(
        tracker_event_map[event.get("event_type")]
        for event in tracker.get("events") or []
        if event.get("event_type") in tracker_event_map
    )
    exit_event_map = {
        "EXIT_APPROACHING": "EXIT_APPROACHING",
        "EXIT_ZONE_REACHED": "EXIT_ZONE_REACHED",
        "EXIT_NOW": "EXIT_NOW",
    }
    event_types.extend(
        exit_event_map[event.get("event_type")]
        for event in ((data.get("hypothetical_exit_advisor") or {}).get("events") or [])
        if event.get("event_type") in exit_event_map
    )
    directional_type = str(directional.get("event_type") or "")
    if directional_type and directional_type != "FALSE_BREAKOUT":
        event_types.append(directional_type)
    breakout_event = ((data.get("breakout_alert") or {}).get("event") or {}).get(
        "event_type"
    )
    if breakout_event:
        event_types.append(str(breakout_event))
    event_reasons = {
        "STATE_CHANGED": f"決策狀態由 {old_state} 更新為 {state}",
        "DATA_STALE": "報價或已收盤 K 線資料已超過允許時效",
        "PRICE_REBOUND": f"價格由 {old_price:.2f} 反彈至 {price:.2f}",
        "KEY_LEVEL_RECLAIMED": (
            f"價格收復關鍵位 {support:.2f}" if support is not None else "價格收復關鍵位"
        ),
        "AWAIT_CLOSE_CONFIRMATION": (
            f"價格穿越 {resistance:.2f}，等待 15M 收盤確認"
            if resistance is not None
            else "價格穿越局部高點，等待 15M 收盤確認"
        ),
        "FIRST_TARGET_REACHED": "第一目標已到，進入條件式獲利管理",
        "SECOND_TARGET_REACHED": "第二目標已到，進一步鎖定獲利",
        "THIRD_TARGET_REACHED": "第三目標已到，可全數平倉或啟動移動停利",
        "PROTECTION_EXIT_REACHED": "15M 收盤觸及最新獲利保護價",
        "EXIT_APPROACHING": "價格接近條件式出場區",
        "EXIT_ZONE_REACHED": "價格已進入條件式出場區",
        "EXIT_NOW": "反向收盤已突破防守價，建議立即降低風險",
        "CANDLE_CLOSE_CONFIRMED": "新的 15M K 線已收盤，決策完成重新確認",
        "INTRABAR_BREACH": "價格盤中測試關鍵位，尚未收盤確認",
        "BREAKDOWN_CONFIRMED": "15M 收盤確認跌破關鍵位",
        "RETEST_REJECTED": "反彈回測關鍵位失敗",
        "BEARISH_CONTINUATION": "空方結構延續並形成新的低點",
        "SHORT_ENTRY_READY": "空方進場條件與風險報酬比已達標",
        "PENDING_BREAKOUT": "價格突破候選壓力，等待收盤確認",
        "BREAKOUT_CONFIRMED": "15M 收盤確認突破壓力",
        "BULLISH_CONTINUATION": "連續收盤站穩突破位，多方延續",
        "BREAKOUT_RETEST": "價格回踩突破區，尚未破壞多方結構",
        "BREAKOUT_FAILED": "價格收盤跌回突破區且結構轉弱",
        "SHORT_INVALIDATED": "空方劇本失效，停止沿用原空方進場區",
        "FALSE_BREAKOUT": "15M 收盤重新站回失守位，確認為假跌破",
        "BULLISH_RECOVERY": "價格收復關鍵位，行情由偏空轉為多方恢復",
        "LONG_DEFENSE_TRIGGERED": "多單防守條件已觸發",
        "SHORT_DEFENSE_TRIGGERED": "空單防守條件已觸發",
    }
    ordinary = [(old_state, state, kind) for kind in dict.fromkeys(event_types)]
    for previous_state, current_state, event_type in transition_chain or ordinary:
        entry_zone = (
            {"low": entry.get("zone_low"), "high": entry.get("zone_high")}
            if isinstance(entry.get("zone_low"), (int, float))
            and isinstance(entry.get("zone_high"), (int, float))
            else None
        )
        targets = [
            value
            for value in (
                entry.get("take_profit_1"),
                entry.get("take_profit_2"),
                entry.get("take_profit_3"),
            )
            if isinstance(value, (int, float))
        ]
        seed = (
            f"{data.get('symbol', 'XAUUSD')}|{previous_state}|{current_state}|"
            f"{event_type}|{candle_time}|"
            f"{pending_trigger.get('level') if pending_trigger else ''}"
        )
        event_id = hashlib.sha256(seed.encode()).hexdigest()[:32]
        events.append(
            {
                "eventId": event_id,
                "event_type": event_type,
                "previousState": previous_state,
                "currentState": current_state,
                "transitionReason": event_reasons[event_type],
                "marketState": current.market_state,
                "finalDecision": current_state,
                "currentPrice": price,
                "entryZone": entry_zone,
                "stopLoss": entry.get("stop_loss"),
                "targets": targets,
                "triggerReason": event_reasons[event_type],
                "candleCloseTime": candle_time,
                "calculatedAt": calculated,
                "dataVersion": current.version,
                "flatAction": flat_action,
                "longManage": long_manage,
                "shortManage": short_manage,
                "confirmation": current.confirmation,
                "decisionBasisCandleCloseTime": candle_time,
                "latestClosedCandlePrice": latest_closed,
                "nextTriggerCondition": pending_trigger,
                "completedTriggers": trigger_result["completed"],
                "triggerLevel": (pending_trigger.get("level") if pending_trigger else
                                 (trigger_result["completed"][-1]["level"]
                                  if trigger_result["completed"] else None)),
                "longDefensePrice": long_plan.get("defense_price"),
                "shortDefensePrice": short_plan.get("defense_price"),
                "spread": spread,
                "executionCosts": {
                    "spread": spread,
                    "slippage": settings.execution_slippage_usd,
                    "fees": settings.execution_fees_usd,
                    "netRiskReward": round(net_rr, 3) if net_rr is not None else None,
                },
                "topic": f"decision-event:{event_id}",
                "message": (
                    f"【狀態變化】{previous_state} → {current_state}\n【現價】{price:.2f}\n"
                    f"【觸發原因】{event_reasons[event_type]}\n【未持倉】{flat_action}\n"
                    f"【已持倉】{long_manage}；{short_manage}\n【資料時間】{quote_time}"
                ),
            }
        )
    out = asdict(current)
    out["triggers"] = trigger_result["triggers"]
    out["completed_triggers"] = trigger_result["completed"]
    out["next_trigger_condition"] = pending_trigger
    out["completed_events"] = completed_events
    out["last_event"] = (
        events[-1]["event_type"] if events else previous.get("last_event", "")
    )
    out["latest_event"] = events[-1] if events else previous.get("latest_event", {})
    out["events"] = events
    return out, events


def enforce_scenario_consistency(final_state: str, long_scenario, short_scenario):
    """Only the one authoritative READY direction may remain executable."""
    if final_state != "LONG_READY" and long_scenario.status in ("PREPARE", "TRIGGERED"):
        long_scenario = long_scenario.model_copy(update={"status": "WATCH"})
    if final_state != "SHORT_READY" and short_scenario.status in (
        "PREPARE",
        "TRIGGERED",
    ):
        short_scenario = short_scenario.model_copy(update={"status": "WATCH"})
    return long_scenario, short_scenario


def assign_event_data_version(event: dict, version: int) -> dict:
    """Finalize identity only after the durable AnalysisRun version is allocated."""
    updated = {**event, "dataVersion": version}
    seed = f"{event.get('eventId', '')}|{version}"
    updated["eventId"] = hashlib.sha256(seed.encode()).hexdigest()[:32]
    updated["topic"] = f"decision-event:{updated['eventId']}"
    return updated
