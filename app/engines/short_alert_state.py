"""Persistent 15M bearish lifecycle shared by web and push notifications."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

ShortAlertStatus = Literal[
    "NEUTRAL", "SHORT_WATCH", "BEARISH_WATCH", "SHORT_ENTRY_READY",
    "SHORT_INVALIDATED",
]
BearishEvent = Literal[
    "", "INTRABAR_BREACH", "BREAKDOWN_CONFIRMED", "RETEST_REJECTED",
    "BEARISH_CONTINUATION", "SHORT_ENTRY_READY", "FALSE_BREAKOUT",
]


@dataclass(frozen=True)
class ShortAlertState:
    status: ShortAlertStatus = "NEUTRAL"
    level: float | None = None
    invalidation_level: float | None = None
    zone_low: float | None = None
    zone_high: float | None = None
    created_at: str = ""
    last_closed_candle: str = ""
    last_closed_price: float | None = None
    last_event: str = ""
    generation: int = 0


@dataclass(frozen=True)
class AlertEvaluation:
    state: ShortAlertState
    should_notify: bool
    event_type: BearishEvent = ""
    topic: str = ""
    message: str = ""
    blocked_reason: str = ""


def _support_level(normalized: dict) -> dict | None:
    return next((x for x in normalized.get("confirmationLevels", [])
                 if x.get("kind") == "support" and x.get("timeframe") == "15M"), None)


def validate_alert_zones(normalized: dict) -> str:
    """Return a reason when current structural zones cannot support advice."""
    atr = float(normalized.get("atr15") or 0)
    levels = normalized.get("confirmationLevels") or []
    for level in levels:
        price, buffer = level.get("price"), level.get("buffer")
        if not isinstance(price, (int, float)) or not isinstance(buffer, (int, float)) or buffer < 0:
            return "區域上下界異常"
        if atr <= 0 or buffer * 2 > atr * 1.5:
            return "區域寬度超過 1.5 倍 15M ATR"
    support = next((x for x in levels if x.get("kind") == "support"), None)
    resistance = next((x for x in levels if x.get("kind") == "resistance"), None)
    if support and resistance:
        if float(support["price"]) >= float(resistance["price"]):
            return "支撐與壓力上下界異常"
        s_low, s_high = support["price"] - support["buffer"], support["price"] + support["buffer"]
        r_low, r_high = resistance["price"] - resistance["buffer"], resistance["price"] + resistance["buffer"]
        overlap = max(0.0, min(s_high, r_high) - max(s_low, r_low))
        smaller = min(s_high - s_low, r_high - r_low)
        if smaller > 0 and overlap / smaller >= 0.5:
            return "支撐與壓力區嚴重重疊"
    return ""


def _message(event: BearishEvent, *, price: float, level: float, closed: str,
             atr: float, entry_plan: dict | None = None) -> str:
    entry_plan = entry_plan or {}
    facts = {
        "INTRABAR_BREACH": f"價格盤中跌破／測試 15M 關鍵位 {level:.2f}，現價 {price:.2f}。",
        "BREAKDOWN_CONFIRMED": f"15M 已收盤跌破關鍵位 {level:.2f}，現價 {price:.2f}。",
        "RETEST_REJECTED": f"價格反彈測試 {level:.2f} 後再次收在其下，回測失敗。",
        "BEARISH_CONTINUATION": f"跌破 {level:.2f} 後再創局部收盤低點，現價 {price:.2f}。",
        "SHORT_ENTRY_READY": "空方結構延續，反彈進場條件與風險報酬比已達標。",
        "FALSE_BREAKOUT": f"15M 已收盤重新站回失守位 {level:.2f}，原跌破失效。",
    }
    states = {
        "INTRABAR_BREACH": "空方觀察", "BREAKDOWN_CONFIRMED": "空方觀察",
        "RETEST_REJECTED": "回測受阻", "BEARISH_CONTINUATION": "空方延續",
        "SHORT_ENTRY_READY": "空單條件完成", "FALSE_BREAKOUT": "空方觀察取消",
    }
    actions = {
        "INTRABAR_BREACH": "等待 15M 收盤，不因盤中刺破直接做空。",
        "BREAKDOWN_CONFIRMED": "原多方計畫失效；等待反彈回測，不在低位追空。",
        "RETEST_REJECTED": "維持空方觀察，等待完整進場條件與風險報酬比達標。",
        "BEARISH_CONTINUATION": ("空方方向延續，但目前避免追空；等待反彈後再評估。"
                                 if atr > 0 and level - price > atr else
                                 "空方方向延續，依進場引擎等待合格價區。"),
        "SHORT_ENTRY_READY": "可依下列計畫評估空單，不可超出有效期限追價。",
        "FALSE_BREAKOUT": "取消空方觀察，等待新的 15M 結構。",
    }
    confirmation = ("否，尚未有 15M 收盤確認。" if event == "INTRABAR_BREACH"
                    else f"是，確認 K 棒時間 {closed}。")
    extra = ""
    if event == "BREAKDOWN_CONFIRMED":
        extra = "\n【持倉風險】若持有多單：建議出場或降低風險；查無持倉也保留此提示。"
    elif event == "SHORT_ENTRY_READY":
        extra = (f"\n【進場區】{entry_plan.get('zone_low'):.2f}–{entry_plan.get('zone_high'):.2f}"
                 f"\n【停損】{entry_plan.get('stop_loss'):.2f}"
                 f"\n【分批止盈】{entry_plan.get('take_profit_1'):.2f}／"
                 f"{entry_plan.get('take_profit_2'):.2f}"
                 f"\n【風險報酬比】{entry_plan.get('risk_reward'):.2f}")
    invalidation = (f"15M 收盤重新站回 {level:.2f} 上方，取消空方判斷。"
                    if event != "FALSE_BREAKOUT" else "等待新的已收盤結構重新建立方向。")
    return (f"【事件】{event}\n【事實】{facts[event]}\n【確認】{confirmation}\n"
            f"【狀態】{states[event]}\n【動作】{actions[event]}{extra}\n"
            f"【失效條件】{invalidation}")


def evaluate_short_alert(normalized: dict, previous: ShortAlertState | None = None,
                         *, entry_plan: dict | None = None,
                         now: datetime | None = None) -> AlertEvaluation:
    previous = previous or ShortAlertState()
    # Compatibility with deployments that persisted the old status name.
    if previous.status == "SHORT_CONFIRMED":  # type: ignore[comparison-overlap]
        previous = replace(previous, status="BEARISH_WATCH")
    now = now or datetime.now(timezone.utc)
    blocked = validate_alert_zones(normalized)
    if blocked:
        return AlertEvaluation(previous, False, blocked_reason=blocked)
    support = _support_level(normalized)
    if not support and previous.level is None:
        return AlertEvaluation(previous, False, blocked_reason="缺少有效 15M 支撐位")
    if support:
        level = float(support["price"])
    else:
        assert previous.level is not None
        level = float(previous.level)
    buffer = float(support.get("buffer") or 0) if support else 0.0
    price = float(normalized.get("currentPrice") or level)
    atr = float(normalized.get("atr15") or 0)
    closed = str(normalized.get("lastClosedCandleTimestamp") or "")
    closed_price_raw = normalized.get("lastClosedCandlePrice")
    closed_price = float(closed_price_raw) if isinstance(closed_price_raw, (int, float)) else None
    support_state = normalized.get("supportState")
    entry_plan = entry_plan or {}
    bearish_active = previous.status in ("BEARISH_WATCH", "SHORT_ENTRY_READY")
    reclaimed = (bearish_active and bool(closed) and closed_price is not None
                 and previous.invalidation_level is not None
                 and closed_price > previous.invalidation_level)
    false_breakout = support_state == "failed_breakdown" or reclaimed
    short_ready = (bearish_active and entry_plan.get("direction") == "SHORT"
                   and entry_plan.get("status") in ("ENTRY_READY", "ENTRY_TRIGGERED")
                   and all(isinstance(entry_plan.get(key), (int, float)) for key in
                           ("zone_low", "zone_high", "stop_loss", "take_profit_1",
                            "take_profit_2", "risk_reward"))
                   and float(entry_plan.get("risk_reward") or 0) >= 1.5)
    confirmed = support_state in ("confirmed_breakdown", "retest_rejected") and bool(closed)
    newer_level = (bearish_active and previous.level is not None
                   and level < previous.level - max(buffer, 0.01))
    lower_close = (bearish_active and support_state == "confirmed_breakdown"
                   and closed_price is not None and previous.last_closed_price is not None
                   and closed_price < previous.last_closed_price - max(buffer, 0.01))

    event: BearishEvent = ""
    status: ShortAlertStatus = previous.status
    if false_breakout:
        event, status = "FALSE_BREAKOUT", "SHORT_INVALIDATED"
        level = float(previous.level or level)
    elif short_ready:
        event, status = "SHORT_ENTRY_READY", "SHORT_ENTRY_READY"
    elif support_state == "retest_rejected" and bearish_active:
        event, status = "RETEST_REJECTED", "BEARISH_WATCH"
    elif confirmed and (newer_level or lower_close):
        event, status = "BEARISH_CONTINUATION", "BEARISH_WATCH"
    elif confirmed and not bearish_active:
        event, status = "BREAKDOWN_CONFIRMED", "BEARISH_WATCH"
    elif support_state == "intrabar_breach" and not bearish_active:
        event, status = "INTRABAR_BREACH", "SHORT_WATCH"
    elif not bearish_active and previous.status not in ("SHORT_INVALIDATED", "SHORT_WATCH"):
        return AlertEvaluation(ShortAlertState(), False)

    same_event = bool(event and previous.last_event == event and previous.last_closed_candle == closed)
    if not event or same_event:
        state = replace(previous, last_closed_candle=closed or previous.last_closed_candle,
                        last_closed_price=(closed_price if closed_price is not None
                                           else previous.last_closed_price))
        return AlertEvaluation(state, False)

    same_level = previous.level is not None and abs(previous.level - level) <= max(buffer, 0.01)
    generation = previous.generation + (1 if not same_level or previous.status == "SHORT_INVALIDATED" else 0)
    selected_buffer = buffer or ((previous.zone_high or level) - (previous.zone_low or level)) / 2
    state = ShortAlertState(
        status=status, level=level, invalidation_level=level,
        zone_low=level - selected_buffer, zone_high=level + selected_buffer,
        created_at=previous.created_at if same_level and previous.created_at else now.isoformat(),
        last_closed_candle=closed, last_closed_price=closed_price,
        last_event=event, generation=generation)
    candle_key = closed or str(normalized.get("marketDataTimestamp") or "intrabar")
    topic = f"bearish:{event}:{level:.2f}:{candle_key}"
    return AlertEvaluation(state, True, event_type=event, topic=topic,
        message=_message(event, price=price, level=level, closed=closed, atr=atr,
                         entry_plan=entry_plan))
