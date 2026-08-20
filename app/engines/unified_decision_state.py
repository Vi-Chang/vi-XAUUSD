"""Single authoritative, transition-driven decision state for web and push."""

from __future__ import annotations

from dataclasses import asdict, dataclass


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
    action = str(decision.get("action") or "WATCH")
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

    confidence = int(
        entry.get("confidence_score") or decision.get("evidence_score") or 0
    )
    trigger = (
        resistance
        if direction == "LONG"
        else support
        if direction == "SHORT"
        else resistance
    )
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
        f"若持有空單：防守 {short_plan.get('defense_price'):.2f}，依序分批止盈"
        if isinstance(short_plan.get("defense_price"), (int, float))
        else "若持有空單：等待最新 15M 防守價"
    )
    current = UnifiedDecision(
        state=state,
        direction=direction,
        action=action,
        confidence=confidence,
        reason=reason,
        flat_action=flat_action,
        long_manage=long_manage,
        short_manage=short_manage,
        next_trigger=trigger,
        confirmation=(
            f"等 15 分鐘收盤站上 {resistance:.2f}"
            if direction != "SHORT" and resistance is not None
            else f"等 15 分鐘收盤跌破 {support:.2f}"
            if support is not None
            else "等待下一根 15 分鐘 K 線收盤"
        ),
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
    if tracker.get("events") and any(
        e.get("event_type") == "TP1" for e in tracker["events"]
    ):
        event_types.append("FIRST_TARGET_REACHED")
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
    }
    for event_type in dict.fromkeys(event_types):
        events.append(
            {
                "event_type": event_type,
                "topic": f"final-decision:{old_state}:{state}:{event_type}:{candle_time}:{price:.2f}",
                "message": (
                    f"【狀態變化】{old_state} → {state}\n【現價】{price:.2f}\n"
                    f"【觸發原因】{event_reasons[event_type]}\n【未持倉】{flat_action}\n"
                    f"【已持倉】{long_manage}；{short_manage}\n【資料時間】{quote_time}"
                ),
            }
        )
    out = asdict(current)
    out["last_event"] = (
        event_types[-1] if event_types else previous.get("last_event", "")
    )
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
