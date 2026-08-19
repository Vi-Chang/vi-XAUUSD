"""Persistent 15M short-direction state machine used by push notifications."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal

ShortAlertStatus = Literal[
    "NEUTRAL", "SHORT_WATCH", "SHORT_CONFIRMED", "SHORT_INVALIDATED"
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
    generation: int = 0


@dataclass(frozen=True)
class AlertEvaluation:
    state: ShortAlertState
    should_notify: bool
    topic: str = ""
    message: str = ""
    blocked_reason: str = ""


def _support_level(normalized: dict) -> dict | None:
    return next((x for x in normalized.get("confirmationLevels", [])
                 if x.get("kind") == "support" and x.get("timeframe") == "15M"), None)


def validate_alert_zones(normalized: dict) -> str:
    """Return a reason when the current structural zones cannot support advice."""
    atr = float(normalized.get("atr15") or 0)
    levels = normalized.get("confirmationLevels") or []
    for level in levels:
        price, buffer = level.get("price"), level.get("buffer")
        if not isinstance(price, (int, float)) or not isinstance(buffer, (int, float)) \
                or buffer < 0:
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


def _message(status: ShortAlertStatus, *, price: float, level: float,
             closed: str, atr: float) -> str:
    confirmed = status == "SHORT_CONFIRMED"
    reclaimed = status == "SHORT_INVALIDATED"
    if confirmed:
        fact = f"15M 已收盤跌破 {level:.2f}，現價 {price:.2f}。"
        confirmation = f"是，確認 K 棒時間 {closed}。"
        state = "空方成立"
        action = ("避免追空；等待反彈至失守位附近受阻再評估。"
                  if atr > 0 and level - price > atr else "等反彈確認，不在低位追空。")
    elif reclaimed:
        fact = f"15M 已收盤重新站回失守位 {level:.2f}，現價 {price:.2f}。"
        confirmation = f"是，確認 K 棒時間 {closed}。"
        state, action = "空方失效", "觀望，等待新的 15M 結構成立。"
    else:
        fact = f"價格盤中跌破／測試 15M 關鍵位 {level:.2f}，現價 {price:.2f}。"
        confirmation = "否，尚未有 15M 收盤確認。"
        state, action = "觀察", "觀望，等待 15M 收盤；不可只因 5 分鐘波動放大做空。"
    invalidation = f"15M 收盤重新站回 {level:.2f} 上方才取消空方判斷。"
    return (f"【事實】{fact}\n【確認】{confirmation}\n【狀態】{state}\n"
            f"【動作】{action}\n【失效條件】{invalidation}")


def evaluate_short_alert(normalized: dict, previous: ShortAlertState | None = None,
                         *, now: datetime | None = None) -> AlertEvaluation:
    previous = previous or ShortAlertState()
    now = now or datetime.now(timezone.utc)
    blocked = validate_alert_zones(normalized)
    if blocked:
        return AlertEvaluation(previous, False, blocked_reason=blocked)
    support = _support_level(normalized)
    if not support:
        return AlertEvaluation(previous, False, blocked_reason="缺少有效 15M 支撐位")
    level = float(support["price"])
    buffer = float(support.get("buffer") or 0)
    price = float(normalized.get("currentPrice") or level)
    atr = float(normalized.get("atr15") or 0)
    closed = str(normalized.get("lastClosedCandleTimestamp") or "")
    support_state = normalized.get("supportState")
    confirmed_now = support_state in ("confirmed_breakdown", "retest_rejected") and bool(closed)
    closed_price = normalized.get("lastClosedCandlePrice")
    reclaimed_now = (previous.status == "SHORT_CONFIRMED" and bool(closed)
                     and isinstance(closed_price, (int, float))
                     and previous.invalidation_level is not None
                     and float(closed_price) > previous.invalidation_level)
    watch_now = support_state == "intrabar_breach"

    # A confirmed short remains confirmed until a CLOSED 15M candle reclaims its loss level.
    if previous.status == "SHORT_CONFIRMED" and not reclaimed_now:
        newer_lower = confirmed_now and previous.level is not None and level < previous.level - max(buffer, 0.01)
        if not newer_lower:
            return AlertEvaluation(replace(previous, last_closed_candle=closed or previous.last_closed_candle), False)

    target: ShortAlertStatus = (
        "SHORT_INVALIDATED" if reclaimed_now and previous.status == "SHORT_CONFIRMED" else
        "SHORT_CONFIRMED" if confirmed_now else
        "SHORT_WATCH" if watch_now else "NEUTRAL")
    if target == "NEUTRAL":
        retained = previous if previous.status in ("SHORT_CONFIRMED", "SHORT_INVALIDATED") \
            else ShortAlertState()
        return AlertEvaluation(retained, False)

    same_level = previous.level is not None and abs(previous.level - level) <= max(buffer, 0.01)
    changed = target != previous.status or not same_level
    if not changed:
        return AlertEvaluation(replace(previous, last_closed_candle=closed or previous.last_closed_candle), False)

    selected_level = previous.level if target == "SHORT_INVALIDATED" and previous.level is not None else level
    selected_buffer = ((previous.zone_high - previous.zone_low) / 2
                       if target == "SHORT_INVALIDATED" and previous.zone_high is not None
                       and previous.zone_low is not None else buffer)
    new_generation = previous.generation + (1 if not same_level or previous.status == "SHORT_INVALIDATED" else 0)
    created = previous.created_at if same_level and previous.created_at else now.isoformat()
    state = ShortAlertState(
        status=target, level=selected_level, invalidation_level=selected_level,
        zone_low=selected_level - selected_buffer, zone_high=selected_level + selected_buffer,
        created_at=created, last_closed_candle=closed,
        generation=new_generation)
    topic = f"short-structure:{target}:{selected_level:.2f}:g{new_generation}"
    return AlertEvaluation(state, True, topic=topic,
        message=_message(target, price=price, level=selected_level, closed=closed, atr=atr))
