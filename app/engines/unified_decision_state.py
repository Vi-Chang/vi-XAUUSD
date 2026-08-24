"""Single authoritative, transition-driven decision state for web and push."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from app.config import get_settings
from app.engines.confidence import (
    GRADING_VERSION,
    get_confidence_grade,
    normalize_signal_score,
)
from app.engines.decision_presentation import build_decision_presentation
from app.engines.entry_engine import EntryPlan, validate_executable_plan
from app.engines.setup_lifecycle import evaluate_setup_lifecycle
from app.engines.trigger_lifecycle import resolve_next_trigger


@dataclass(frozen=True)
class UnifiedDecision:
    state: str = "WAIT"
    direction: str = "NONE"
    action: str = "等待"
    confidence: int | None = None
    signal_score: int | None = None
    confidence_grade: str = "U"
    grading_version: str = GRADING_VERSION
    trade_status: str = "WAIT_CONFIRMATION"
    can_enter: bool = False
    blocked_reason: str = ""
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
    elif status == "ENTRY_READY":
        state = "LONG_WATCH" if direction == "LONG" else "SHORT_WATCH"
    elif status == "SETUP_WATCH":
        state = "LONG_BIAS" if direction == "LONG" else "SHORT_BIAS"
    elif status in ("INVALIDATED", "EXITED"):
        state = "INVALIDATED"
    elif action in ("PREPARE_LONG", "LONG"):
        state, direction = "LONG_BIAS", "LONG"
    elif action in ("PREPARE_SHORT", "SHORT"):
        state, direction = "SHORT_BIAS", "SHORT"
    recovery_continues = (
        previous.get("state") == "LONG_BIAS"
        and (
            support is None
            or float(normalized.get("lastClosedCandlePrice") or price) >= support
        )
        and state in ("WAIT", "INVALIDATED")
    )
    if false_breakout or recovery_continues:
        state, direction = "LONG_BIAS", "LONG"

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
        suggested_value = float(suggested) if isinstance(suggested, (int, float)) else 0.0
        stop_value = float(stop) if isinstance(stop, (int, float)) else 0.0
        tp1_value = float(tp1) if isinstance(tp1, (int, float)) else 0.0
        risk = abs(suggested_value - stop_value)
        reward = abs(tp1_value - suggested_value)
        if risk > 0:
            net_rr = max(0.0, reward - execution_cost) / (risk + execution_cost)
            if state.endswith("READY") and (
                net_rr < settings.setup_min_rr1
                or execution_cost / risk > settings.execution_max_cost_risk_ratio
            ):
                state = "LONG_WATCH" if direction == "LONG" else "SHORT_WATCH"
                entry = {**entry, "missing_condition":
                         f"扣除點差與滑價後賺賠比僅 {net_rr:.2f}，等待更好的價格"}

    signal_score = normalize_signal_score(
        decision.get("signal_score", decision.get("evidence_score"))
    )
    confidence = signal_score
    confidence_grade = get_confidence_grade(signal_score)
    latest_closed_raw = normalized.get("lastClosedCandlePrice")
    latest_closed = (float(latest_closed_raw)
                     if isinstance(latest_closed_raw, (int, float)) else None)
    trigger_result = resolve_next_trigger(
        resistance=resistance, support=support, latest_closed=latest_closed,
        direction=direction, state=state)
    pending_trigger = trigger_result["next"]
    # A displayed closed-candle condition is part of executability, not decoration.
    if state.endswith("READY") and pending_trigger is not None:
        state = "LONG_WATCH" if direction == "LONG" else "SHORT_WATCH"
        entry = {
            **entry,
            "missing_condition": trigger_result["label"],
        }
    setup_lifecycle = previous.get("setup_lifecycle") or {}
    setup_id = str(entry.get("setup_id") or "")
    if setup_id and direction in ("LONG", "SHORT") and not stale:
        zone_low, zone_high = entry.get("zone_low"), entry.get("zone_high")
        fixed_confirmation = (
            setup_lifecycle.get("confirmationPrice")
            if setup_lifecycle.get("setupId") == setup_id
            and isinstance(setup_lifecycle.get("confirmationPrice"), (int, float))
            else resistance if direction == "LONG" else support
        )
        setup_lifecycle = evaluate_setup_lifecycle(
            previous=setup_lifecycle,
            setup_id=setup_id,
            direction=direction,
            confirmation_price=fixed_confirmation,
            latest_closed_price=latest_closed,
            closed_candle_time=candle_time,
            current_price=price,
            entry_zone_low=float(zone_low) if isinstance(zone_low, (int, float)) else None,
            entry_zone_high=float(zone_high) if isinstance(zone_high, (int, float)) else None,
            risk_controls_passed=(
                status == "ENTRY_TRIGGERED"
                and not entry.get("missing_condition")
                and (net_rr is None or net_rr >= settings.setup_min_rr1)
            ),
            calculated_at=calculated,
            invalidated=status in ("INVALIDATED", "EXITED"),
        )
        lifecycle_state = setup_lifecycle["state"]
        state = {
            "WAIT_CONFIRMATION": "LONG_WATCH" if direction == "LONG" else "SHORT_WATCH",
            "CONFIRMED_WAIT_RETEST": "CONFIRMED_WAIT_RETEST",
            "ENTRY_READY": "LONG_READY" if direction == "LONG" else "SHORT_READY",
            "MISSED_ENTRY": "MISSED_ENTRY",
            "INVALIDATED": "INVALIDATED",
        }[lifecycle_state]
    reason = str(
        entry.get("missing_condition") or decision.get("reason") or "等待條件一致"
    )
    flat_action = reason
    if state == "DATA_STALE":
        direction, action = "NONE", "暫停交易"
        reason = flat_action = "行情或 K 線資料已過期，等待資料恢復"
    elif state == "MISSED_ENTRY":
        action = "禁止追價"
        flat_action = "原進場區已錯過；等待價格回踩新的確認區"
    elif state == "CONFIRMED_WAIT_RETEST":
        action = "突破確認完成，等待回踩"
        flat_action = "目前距離理想進場區過遠，暫不追價。"
    elif state == "INVALIDATED":
        action = "舊劇本已失效"
        flat_action = "舊劇本已取消，依最新結構等待新劇本"
    elif state == "LONG_BIAS" and false_breakout:
        action = "行情偏多，但尚未到進場區"
        reason = "空方劇本已失效，價格重新站回關鍵位"
        flat_action = "暫不追價，等待回踩新支撐或 15M 收盤確認"
    elif state.endswith("BIAS"):
        action = "行情偏多，但尚未到進場區" if direction == "LONG" else "行情偏空，但尚未到進場區"
        flat_action = "等待，尚未到進場區，請勿追價。"
    elif state.endswith("READY"):
        action = "可依完整風控計畫評估"
        flat_action = f"{entry.get('trigger_timeframe') or '15M'} 收盤條件已完成"
    elif state.endswith("WATCH"):
        action = "等待確認，尚不可進場"
        flat_action = "等待，尚不可進場，請勿追價。"
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
    trade_manager = data.get("trade_plan_manager") or {}
    active_trade_plans = list(trade_manager.get("activePlans") or [])
    breakout_manager = data.get("breakout_setup_manager") or {}
    breakout_setups = list(breakout_manager.get("setups") or [])
    breakout_events = list(breakout_manager.get("events") or [])
    ready_breakout = next((item for item in reversed(breakout_setups)
                           if item.get("status") in (
                               "ENTRY_READY_BREAKOUT", "ENTRY_READY_RETEST",
                               "BREAKOUT_ENTRY_READY", "PULLBACK_ENTRY_READY")), None)
    waiting_retest = next((item for item in reversed(breakout_setups)
                           if item.get("status") in {"WAIT_RETEST", "WAIT_PULLBACK_CONFIRMATION"}), None)
    pending_breakout = next((item for item in reversed(breakout_setups)
                             if item.get("status") in {"WAIT_BREAKOUT_CONFIRMATION",
                                                       "WAIT_BREAKOUT_OR_PULLBACK",
                                                       "PULLBACK_BREACH_PENDING_CLOSE"}), None)
    continuation = data.get("trend_continuation_engine") or {}
    continuation_selected = continuation.get("selected") or {}
    continuation_live = bool(continuation_selected and not continuation.get("shadowMode", True))
    continuation_events = [item for item in continuation.get("events") or []
                           if item.get("notificationEligible")]
    if continuation_live and not stale:
        direction, state = "LONG", "LONG_READY"
        action = "強勢趨勢順勢進場條件成立"
        flat_action = f"{continuation_selected.get('type')} 已完成完整風控，可依計畫評估進場"
    elif ready_breakout and not stale:
        direction = str(ready_breakout["direction"])
        state = "LONG_READY" if direction == "LONG" else "SHORT_READY"
        action = "回踩進場條件成立" if ready_breakout.get("entryType") == "RETEST" else "突破進場條件成立"
        flat_action = "可以依固定劇本與完整風控評估進場"
    elif waiting_retest and pending_breakout and not stale:
        direction = str(pending_breakout["direction"])
        state = "LONG_WATCH" if direction == "LONG" else "SHORT_WATCH"
        flat_action = (
            f"舊劇本 {waiting_retest['breakoutTrigger']:.2f} 已確認，等待回踩 "
            f"{waiting_retest['retestZoneLow']:.2f}–{waiting_retest['retestZoneHigh']:.2f}；"
            f"新劇本等待15M收盤突破 {pending_breakout['breakoutTrigger']:.2f}")
    regime_state = data.get("regime_state_machine") or {}
    if not regime_state:
        # Direct callers and legacy snapshots still receive a fresh, closed-candle
        # classification; never fall back to a persisted WEAK label.
        from app.engines.regime_state_machine import evaluate_regime_state
        regime_state, _ = evaluate_regime_state(data)
    composite_regime = str(regime_state.get("compositeRegime") or "")
    weak_htf_bullish = composite_regime == "HTF_BULLISH_LTF_WEAKENING"
    recovering_htf_bullish = composite_regime == "HTF_BULLISH_LTF_RECOVERING"
    restored_htf_bullish = composite_regime == "HTF_BULLISH_LTF_BULLISH_RESTORED"
    bearish_confirmed = composite_regime == "BEARISH_CONFIRMED"
    if (weak_htf_bullish and not stale and not state.endswith(("READY", "MANAGE"))):
        state, direction = "SHORT_TERM_WEAK_HTF_BULLISH", "NONE"
        action = "短線回檔，高週期尚未翻空"
        flat_action = "現在不追多，也先不追空；等待15M重新轉強或15M／1H確認轉空"
    elif (recovering_htf_bullish and not stale
          and not state.endswith(("READY", "MANAGE"))):
        state, direction = "SHORT_TERM_RECOVERING", "NONE"
        action = "短線正在恢復，還沒完全轉強"
        flat_action = "先等15M收盤站回重新轉強價；尚未確認前不追價"
    elif (restored_htf_bullish and not stale
          and not state.endswith(("READY", "MANAGE"))):
        state, direction = "SHORT_TERM_BULLISH_RESTORED", "LONG"
        action = "短線重新轉強"
        flat_action = "已撤銷短線轉弱；重新評估突破、回踩、追價上限與賺賠比"
    elif (bearish_confirmed and not stale
          and not state.endswith(("READY", "MANAGE"))):
        state, direction = "BEARISH_CONFIRMED", "SHORT"
        action = "短線已正式轉空"
        flat_action = "15M與1H結構均已確認轉空；重新評估空方進場與風控"
    long_plan, short_plan = exit_plans.get("LONG") or {}, exit_plans.get("SHORT") or {}
    def management_text(plan: dict, side: str) -> str:
        defense = plan.get("defense_price")
        partial, full = plan.get("partial_exit") or {}, plan.get("full_exit") or {}
        if not isinstance(defense, (int, float)):
            return f"若持有{side}：等待最新15M防守價與明確止盈區"
        if all(isinstance(value, (int, float)) for value in (
                partial.get("low"), partial.get("high"), full.get("low"), full.get("high"))):
            return (f"若持有{side}：{float(partial['low']):.2f}–{float(partial['high']):.2f}"
                    f"先平倉30%；{float(full['low']):.2f}–{float(full['high']):.2f}"
                    f"評估剩餘部位；防守價 {float(defense):.2f}")
        return f"若持有{side}：防守價 {float(defense):.2f}；止盈價區尚在重新計算"

    long_manage = management_text(long_plan, "多單")
    short_manage = (
        f"若持有空單：{short_plan.get('defense_price'):.2f} 防守條件已觸發，依風控規則處理"
        if isinstance(short_plan.get("defense_price"), (int, float))
        and price > float(short_plan["defense_price"])
        else management_text(short_plan, "空單")
    )
    if (isinstance(long_plan.get("defense_price"), (int, float))
            and price < float(long_plan["defense_price"])):
        long_manage = (f"若持有多單：{long_plan['defense_price']:.2f} "
                       "防守條件已觸發，依風控規則處理")
    active_long = next((p for p in active_trade_plans if p.get("direction") == "LONG"), None)
    active_short = next((p for p in active_trade_plans if p.get("direction") == "SHORT"), None)
    if active_long:
        long_manage = (
            f"若你持有多單：TP1 {active_long['tp1Price']:.2f} 平倉30%；"
            f"TP2 {active_long['tp2Price']:.2f} 再平倉30%；"
            f"TP3 {active_long['tp3Price']:.2f} 後剩餘40%移動止盈；"
            f"目前防守 {active_long['trailingStopPrice']:.2f}"
        )
    if active_short:
        short_manage = (
            f"若你持有空單：TP1 {active_short['tp1Price']:.2f} 平倉30%；"
            f"TP2 {active_short['tp2Price']:.2f} 再平倉30%；"
            f"TP3 {active_short['tp3Price']:.2f} 後剩餘40%移動止盈；"
            f"目前防守 {active_short['trailingStopPrice']:.2f}"
        )
    from app.engines.confidence import permission_from_state
    permission = permission_from_state(
        state,
        existing_status=str(decision.get("trade_status") or ""),
        existing_reason=str(decision.get("blocked_reason") or ""),
    )
    current = UnifiedDecision(
        state=state,
        direction=direction,
        action=action,
        confidence=confidence,
        signal_score=signal_score,
        confidence_grade=confidence_grade,
        grading_version=GRADING_VERSION,
        trade_status=permission.trade_status,
        can_enter=permission.can_enter,
        blocked_reason=permission.blocked_reason,
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
    if false_breakout and old_state not in ("FALSE_BREAKOUT", "LONG_BIAS"):
        transition_chain = [
            (old_state, "SHORT_INVALIDATED", "SHORT_INVALIDATED"),
            ("SHORT_INVALIDATED", "FALSE_BREAKOUT", "FALSE_BREAKOUT"),
            ("FALSE_BREAKOUT", "LONG_BIAS", "BULLISH_RECOVERY"),
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
    if not (data.get("trade_plan_manager") or {}).get("plans"):
        event_types.extend(
            tracker_event_map[event.get("event_type")]
            for event in tracker.get("events") or []
            if event.get("event_type") in tracker_event_map
        )
    trade_event_map = {
        "TAKE_PROFIT_1": "TAKE_PROFIT_1",
        "TAKE_PROFIT_2": "TAKE_PROFIT_2",
        "TAKE_PROFIT_3": "TAKE_PROFIT_3",
        "EARLY_EXIT": "EARLY_EXIT",
        "TRAILING_STOP_UPDATE": "TRAILING_STOP_UPDATE",
        "STOP_TRIGGERED": "STOP_TRIGGERED",
        "STRUCTURE_INVALIDATED": "STRUCTURE_INVALIDATED",
    }
    trade_events = list(trade_manager.get("events") or [])
    exit_event_map = {
        "EXIT_APPROACHING": "EXIT_APPROACHING",
        "EXIT_ZONE_REACHED": "EXIT_ZONE_REACHED",
        "EXIT_NOW": "EXIT_NOW",
    }
    event_types.extend(
        exit_event_map[event.get("event_type")]
        for event in ((data.get("hypothetical_exit_advisor") or {}).get("events") or [])
        if event.get("event_type") in exit_event_map
        and not any(plan.get("direction") == event.get("side")
                    for plan in active_trade_plans)
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
            f"盤中價格已高於 {resistance:.2f}，等待 15M 收盤站上確認"
            if resistance is not None
            else "盤中價格已高於局部高點，等待 15M 收盤站上確認"
        ),
        "FIRST_TARGET_REACHED": "第一目標已到，進入條件式獲利管理",
        "SECOND_TARGET_REACHED": "第二目標已到，進一步鎖定獲利",
        "THIRD_TARGET_REACHED": "第三目標已到，可全數平倉或啟動移動停利",
        "PROTECTION_EXIT_REACHED": "15M 收盤觸及最新獲利保護價",
        "EXIT_APPROACHING": "價格接近條件式出場區",
        "EXIT_ZONE_REACHED": "價格已到達本次計畫的明確分批處理價區",
        "EXIT_NOW": "15M 收盤已使防守條件成立，建議立即降低風險",
        "CANDLE_CLOSE_CONFIRMED": "新的 15M K 線已收盤，決策完成重新確認",
        "INTRABAR_BREACH": "價格盤中測試關鍵位，尚未收盤確認",
        "BREAKDOWN_CONFIRMED": "15M 收盤確認跌破關鍵位",
        "RETEST_REJECTED": "反彈回測關鍵位失敗",
        "BEARISH_CONTINUATION": "空方結構延續並形成新的低點",
        "SHORT_ENTRY_READY": "空方進場條件與風險報酬比已達標",
        "PENDING_BREAKOUT": "價格突破候選壓力，等待收盤確認",
        "BULLISH_CONTINUATION": "連續收盤站穩突破位，多方延續",
        "BREAKOUT_RETEST": "價格回踩突破區，尚未破壞多方結構",
        "BREAKOUT_FAILED": "價格收盤跌回突破區且結構轉弱",
        "SHORT_INVALIDATED": "空方劇本失效，停止沿用原空方進場區",
        "FALSE_BREAKOUT": "15M 收盤重新站回失守位，確認為假跌破",
        "BULLISH_RECOVERY": "價格收復關鍵位，行情由偏空轉為多方恢復",
        "TRIGGER_CHANGED": "下一個有效確認條件已更新",
        "LONG_DEFENSE_TRIGGERED": "多單防守條件已觸發",
        "SHORT_DEFENSE_TRIGGERED": "空單防守條件已觸發",
        "TAKE_PROFIT_1": "第一止盈價已觸發，建議分批平倉 30%",
        "TAKE_PROFIT_2": "第二止盈價已觸發，建議再平倉 30%",
        "TAKE_PROFIT_3": "第三止盈價已觸發，剩餘 40% 採移動止盈",
        "EARLY_EXIT": "15M 收盤觸發提前退出條件",
        "TRAILING_STOP_UPDATE": "最新 15M 結構已提高移動防守價",
        "STOP_TRIGGERED": "防守／停損價已觸發",
        "STRUCTURE_INVALIDATED": "持倉依據的市場結構已正式失效",
        "NEW_SETUP_CREATED": "新15M市場結構成立，已建立獨立突破劇本",
        "BREAKOUT_CONFIRMED": "固定突破門檻已由15M收盤確認",
        "WAIT_RETEST": "突破已確認但價格離合理進場區較遠，等待回踩",
        "ENTRY_READY_BREAKOUT": "突破確認且仍在最大追價界線內，可以評估突破進場",
        "ENTRY_READY_RETEST": "價格回到固定回踩區且15M確認守住，可以評估回踩進場",
        "WAIT_BREAKOUT_OR_PULLBACK": "同步監控收盤突破與較佳回踩位置",
        "WAIT_PULLBACK_CONFIRMATION": "價格已進入回踩區，等待15M止跌確認",
        "BREAKOUT_ENTRY_READY": "15M收盤突破且仍在最大追價界線內",
        "PULLBACK_ENTRY_READY": "價格回到動態支撐區且15M已確認止跌",
        "PULLBACK_INVALIDATED": "15M收盤跌破回踩失效價，取消多方回踩劇本",
        "PULLBACK_ZONE_UPDATED": "新結構成立，回踩觀察區已更新",
        "PULLBACK_BREACH_PENDING_CLOSE": "即時價格跌穿回踩防守位，暫停進場並等待15M收盤",
        "SETUP_EXPIRED": "突破劇本已到期",
        "ENTRY_READY_SHALLOW_PULLBACK": "強勢趨勢淺回踩已由15M確認",
        "ENTRY_READY_BREAKOUT_RETEST": "固定突破位回踩守住，風控已通過",
        "ENTRY_READY_BULL_FLAG": "強漲後窄幅整理已收盤突破",
        "ENTRY_READY_MOMENTUM_CONTINUATION": "多週期高度一致，動能延續突破成立",
    }
    old_trigger = previous.get("next_trigger")
    new_trigger = pending_trigger.get("level") if pending_trigger else None
    if old_state == state and old_trigger != new_trigger:
        event_types.append("TRIGGER_CHANGED")
    ordinary: list[tuple[str, str, str, dict]] = [
        (old_state, state, kind, {}) for kind in dict.fromkeys(event_types)
    ]
    ordinary.extend(
        (old_state, state, trade_event_map[item["event_type"]], item)
        for item in trade_events if item.get("event_type") in trade_event_map
    )
    ordinary.extend(
        (str(item.get("previousState") or old_state),
         str(item.get("currentState") or state), str(item["event_type"]), item)
        for item in breakout_events if item.get("event_type") in event_reasons
    )
    ordinary.extend(
        (old_state, "LONG_READY", str(item["event_type"]), item)
        for item in continuation_events if item.get("event_type") in event_reasons
    )
    transitions = ([(old, new, kind, {}) for old, new, kind in transition_chain]
                   if transition_chain else ordinary)
    if transition_chain:
        transitions.extend(
            (old_state, state, trade_event_map[item["event_type"]], item)
            for item in trade_events if item.get("event_type") in trade_event_map
        )
        transitions.extend(
            (str(item.get("previousState") or old_state),
             str(item.get("currentState") or state), str(item["event_type"]), item)
            for item in breakout_events if item.get("event_type") in event_reasons
        )
        transitions.extend(
            (old_state, "LONG_READY", str(item["event_type"]), item)
            for item in continuation_events if item.get("event_type") in event_reasons
        )
    for previous_state, current_state, event_type, source_event in transitions:
        trade_event = source_event if source_event.get("tradePlanId") else {}
        continuation_event_item = (
            source_event if str(source_event.get("event_type") or "").startswith(
                "ENTRY_READY_") and source_event.get("setup", {}).get("calculationVersion")
            == "trend-continuation-v1" else {})
        breakout_event_item = (
            source_event if source_event.get("setup") and not continuation_event_item else {})
        entry_zone = (
            continuation_event_item.get("entryZone")
            if continuation_event_item
            else breakout_event_item.get("entryZone")
            if breakout_event_item
            else
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
        if breakout_event_item:
            breakout_setup = breakout_event_item["setup"]
            targets = [breakout_setup["tp1"], breakout_setup["tp2"],
                       breakout_setup["tp3"]]
        if continuation_event_item:
            continuation_setup = continuation_event_item["setup"]
            targets = [continuation_setup["tp1"], continuation_setup["tp2"],
                       continuation_setup["tp3"]]
        seed = (
            f"{data.get('symbol', 'XAUUSD')}|{previous_state}|{current_state}|"
            f"{event_type}|{candle_time}|"
            f"{pending_trigger.get('level') if pending_trigger else ''}|"
            f"{trade_event.get('tradePlanId', '')}|{trade_event.get('targetIndex', '')}"
            f"|{breakout_event_item.get('setupId', '')}"
        )
        event_id = hashlib.sha256(seed.encode()).hexdigest()[:32]
        payload = {
                "eventId": event_id,
                "setupId": (continuation_event_item.get("setupId")
                            or breakout_event_item.get("setupId") or setup_id),
                "tradePlanId": trade_event.get("tradePlanId"),
                "targetIndex": trade_event.get("targetIndex"),
                "positionEvent": trade_event,
                "activeTradePlans": active_trade_plans,
                "breakoutSetupEvent": breakout_event_item,
                "trendContinuationEvent": continuation_event_item,
                "breakoutSetups": breakout_setups,
                "event_type": event_type,
                "previousState": previous_state,
                "currentState": current_state,
                "direction": direction,
                "transitionReason": event_reasons[event_type],
                "marketState": current.market_state,
                "finalDecision": current_state,
                "currentPrice": price,
                "signalScore": signal_score,
                "confidenceGrade": confidence_grade,
                "gradingVersion": GRADING_VERSION,
                "tradeStatus": permission.trade_status,
                "canEnter": permission.can_enter,
                "blockedReason": (breakout_event_item.get("blockedReason")
                                  if breakout_event_item else permission.blocked_reason),
                "entryZone": entry_zone,
                "stopLoss": ((continuation_event_item.get("setup") or {}).get("stopPrice")
                             if continuation_event_item else
                             (breakout_event_item.get("setup") or {}).get("stopPrice")
                             if breakout_event_item else entry.get("stop_loss")),
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
                "missingCondition": entry.get("missing_condition"),
                "cancelCondition": entry.get("cancel_condition"),
                "nextTriggerCondition": pending_trigger,
                "completedTriggers": trigger_result["completed"],
                "triggerLevel": (breakout_event_item.get("triggerPrice")
                                 if breakout_event_item else
                                 pending_trigger.get("level") if pending_trigger else
                                 (trigger_result["completed"][-1]["level"]
                                  if trigger_result["completed"] else None)),
                "setupLifecycle": setup_lifecycle,
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
        payload["presentation"] = build_decision_presentation(payload)
        events.append(payload)
    out = asdict(current)
    out["latest_closed_price"] = latest_closed
    out["presentation"] = build_decision_presentation({
        "state": state,
        "direction": direction,
        "canEnter": current.can_enter,
        "flatAction": flat_action,
        "missingCondition": entry.get("missing_condition"),
        "confirmation": current.confirmation,
        "cancelCondition": entry.get("cancel_condition"),
        "stopLoss": entry.get("stop_loss"),
    })
    out["triggers"] = trigger_result["triggers"]
    out["completed_triggers"] = trigger_result["completed"]
    out["next_trigger_condition"] = pending_trigger
    out["completed_events"] = completed_events
    out["setup_lifecycle"] = setup_lifecycle
    out["trend_continuation"] = continuation
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
