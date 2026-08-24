"""Action-oriented lifecycle derived from the canonical decision on every evaluation."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone


LIFECYCLE_EVENTS = {
    "BIAS_CHANGE", "SETUP_FORMING", "ENTRY_APPROACHING", "ENTRY_READY",
    "PRICE_RAN_AWAY", "WAIT_RETRACE", "RETRACE_APPROACHING",
    "RETRACE_ZONE_ENTERED", "ENTRY_INVALIDATED", "SETUP_WEAKENING",
    "REENTRY_AVAILABLE", "TARGET_UPDATED", "TP_APPROACHING", "TP_HIT",
    "TRAILING_STOP_UPDATE", "EXIT_WARNING", "EXIT_NOW", "NEW_STRUCTURE",
    "NO_TRADE",
}


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _zone(decision: dict) -> tuple[float | None, float | None]:
    zone = decision.get("entryZone") or {}
    return _num(zone.get("low")), _num(zone.get("high"))


def _signature(state: dict) -> str:
    stable = (
        state.get("eventType"), state.get("setupId"), state.get("direction"),
        state.get("bias"), state.get("entryLow"), state.get("entryHigh"),
        state.get("invalidation"), tuple(state.get("targets") or []),
        state.get("rrBucket"), state.get("trigger"), state.get("chaseLimit"),
    )
    return hashlib.sha256(repr(stable).encode()).hexdigest()[:24]


def evaluate_signal_lifecycle(
    decision: dict, previous: dict | None = None, *, live_quote: bool = False
) -> tuple[dict, list[dict]]:
    """Evaluate lifecycle independently of whether Telegram will notify.

    Live quotes may create proximity/retrace events. ENTRY_READY remains solely
    controlled by the canonical closed-candle/risk gates (``canEnter``).
    """
    previous = previous or {}
    price = _num(decision.get("currentPrice")) or 0.0
    low, high = _zone(decision)
    direction = str(decision.get("direction") or "NONE")
    action = str(decision.get("finalAction") or decision.get("finalDecision") or "WAIT")
    can_enter = bool(decision.get("canEnter")) and action in {"ENTER_LONG", "ENTER_SHORT"}
    stop = _num(decision.get("invalidationPrice") or decision.get("stopLoss"))
    targets = [float(x) for x in (decision.get("targets") or []) if isinstance(x, (int, float))]
    rr = _num(decision.get("effectiveRR"))
    setup_id = str(decision.get("selectedSetupId") or decision.get("setupId") or "")
    bias = str(decision.get("marketState") or decision.get("marketRegime") or direction)
    trigger = _num(decision.get("triggerLevel") or decision.get("canonicalNextTrigger"))
    chase = _num(decision.get("chaseLimit"))
    stale = str(decision.get("primaryReason") or "") == "DATA_STALE"
    blocked = {str(x) for x in (decision.get("blockingReasons") or [])}

    event_type = "NO_TRADE"
    reason = str(decision.get("humanSummary") or "目前沒有可執行交易")
    if stale:
        event_type, reason = "NO_TRADE", "行情資料延遲，暫停進場判斷"
    elif can_enter:
        event_type, reason = "ENTRY_READY", "收盤確認、價格位置及風控條件均已通過"
    elif action == "MANAGE_POSITION":
        event_type = "EXIT_WARNING" if blocked else "TRAILING_STOP_UPDATE"
    elif low is not None and high is not None:
        in_zone = low <= price <= high
        width = max(high - low, 0.01)
        approach = max(width, _num(decision.get("atr15")) or 3.0)
        directional_chase = ((direction == "LONG" and chase is not None and price > chase)
                             or (direction == "SHORT" and chase is not None and price < chase))
        if directional_chase:
            event_type, reason = "PRICE_RAN_AWAY", "方向成立但價格已離開可執行範圍，等待回踩"
        elif in_zone:
            event_type = "RETRACE_ZONE_ENTERED" if not can_enter else "ENTRY_READY"
            reason = "價格已進入觀察區，仍需已收盤 K 棒與風控確認"
        elif min(abs(price - low), abs(price - high)) <= approach:
            event_type = "RETRACE_APPROACHING" if live_quote else "ENTRY_APPROACHING"
            reason = "價格接近可執行區，尚未構成進場確認"
        elif "RISK_REWARD_TOO_LOW" in blocked or (rr is not None and rr < 1.5):
            event_type, reason = "SETUP_WEAKENING", "方向仍在，但目前風險報酬不足"
        else:
            event_type, reason = "WAIT_RETRACE", "等待價格回到合理區域"
    elif setup_id:
        event_type, reason = "SETUP_FORMING", "交易劇本正在形成，尚未建立可執行區"

    previous_bias = str(previous.get("bias") or "")
    if previous_bias and bias and previous_bias != bias and event_type not in {
            "ENTRY_READY", "EXIT_NOW", "ENTRY_INVALIDATED"}:
        event_type, reason = "BIAS_CHANGE", f"市場背景由 {previous_bias} 轉為 {bias}"
    state = {
        "eventType": event_type, "setupId": setup_id, "direction": direction,
        "bias": bias, "currentPrice": price, "entryLow": low, "entryHigh": high,
        "invalidation": stop, "targets": targets, "rrBucket": (
            None if rr is None else round(rr, 1)), "effectiveRR": rr,
        "trigger": trigger, "chaseLimit": chase, "reason": reason,
        "evaluatedAt": str(decision.get("calculatedAt") or datetime.now(timezone.utc).isoformat()),
        "candleCloseTime": str(decision.get("sourceCandleCloseTime") or
                               decision.get("candleCloseTime") or ""),
        "liveQuote": live_quote,
        "sourceTimestamps": dict(decision.get("sourceTimestamps") or {}),
    }
    state["signature"] = _signature(state)
    if state["signature"] == previous.get("signature"):
        return state, []
    event_id = hashlib.sha256(
        f"{setup_id}|{event_type}|{state['signature']}".encode()).hexdigest()[:32]
    event = {
        "eventId": event_id, "event_type": event_type, "eventVersion": 1,
        "setupId": setup_id, "previousState": str(previous.get("eventType") or "NO_TRADE"),
        "currentState": event_type, "transitionReason": reason,
        "direction": direction, "currentPrice": price,
        "entryZone": ({"low": low, "high": high} if low is not None and high is not None else None),
        "stopLoss": stop, "targets": targets, "effectiveRR": rr,
        "triggerLevel": trigger, "chaseLimit": chase,
        "candleCloseTime": state["candleCloseTime"], "calculatedAt": state["evaluatedAt"],
        "generatedAtUtc": state["evaluatedAt"], "finalDecision": action,
        "finalAction": action, "canEnter": can_enter,
        "sourceTimestamps": state["sourceTimestamps"],
    }
    return state, [event]
