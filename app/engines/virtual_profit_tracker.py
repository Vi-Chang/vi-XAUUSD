"""Conditional profit-taking tracker for every formally triggered entry."""

from __future__ import annotations


def evaluate_virtual_profit(
    entry_plan: dict,
    previous: dict | None,
    *,
    current_price: float,
    closed_price: float | None,
    latest_structure_protection: float | None,
    candle_close_time: str,
) -> tuple[dict, list[dict]]:
    previous = previous or {}
    if entry_plan.get("status") != "ENTRY_TRIGGERED" and not previous.get("active"):
        return previous, []
    if not previous.get("active"):
        entry, stop = entry_plan.get("suggested_entry"), entry_plan.get("stop_loss")
        if not isinstance(entry, (int, float)) or not isinstance(stop, (int, float)):
            return {}, []
        risk = abs(float(entry) - float(stop))
        if risk <= 0:
            return {}, []
        direction = str(entry_plan.get("direction"))
        sign = 1 if direction == "LONG" else -1
        previous = {
            "active": True,
            "setup_id": entry_plan.get("setup_id", ""),
            "direction": direction,
            "entry": float(entry),
            "original_stop": float(stop),
            "risk": risk,
            "tp1": float(entry) + sign * risk,
            "tp2": float(entry) + sign * risk * 2,
            "tp3": float(entry) + sign * risk * 3,
            "protection": float(stop),
            "notified": [],
            "peak": float(entry),
        }
    state = dict(previous)
    direction, sign = state["direction"], (1 if state["direction"] == "LONG" else -1)
    state["peak"] = (
        max(state["peak"], current_price)
        if sign > 0
        else min(state["peak"], current_price)
    )
    notified, events = list(state.get("notified", [])), []
    for level, percent, protection in (
        (1, "30%", state["entry"]),
        (2, "再平倉30%～50%", state["tp1"]),
        (3, "全部平倉或啟動15M移動停利", state["tp2"]),
    ):
        target = state[f"tp{level}"]
        reached = current_price >= target if sign > 0 else current_price <= target
        key = f"TP{level}"
        if reached and key not in notified:
            notified.append(key)
            state["protection"] = protection
            events.append(
                {
                    "event_type": key,
                    "topic": f"virtual-profit:{state['setup_id']}:{key}",
                    "message": f"若你有在 {state['entry']:.2f} 附近進場：已到 {level}R "
                    f"({target:.2f})，建議{percent}；保護價調整至 {protection:.2f}。",
                }
            )
    if latest_structure_protection is not None and "TP3" in notified:
        state["protection"] = (
            max(state["protection"], latest_structure_protection)
            if sign > 0
            else min(state["protection"], latest_structure_protection)
        )
    protection_hit = closed_price is not None and (
        (sign > 0 and closed_price < state["protection"])
        or (sign < 0 and closed_price > state["protection"])
    )
    if protection_hit and "TRAILING_EXIT" not in notified:
        notified.append("TRAILING_EXIT")
        state["active"] = False
        events.append(
            {
                "event_type": "TRAILING_EXIT",
                "topic": f"virtual-profit:{state['setup_id']}:TRAILING_EXIT:{candle_close_time}",
                "message": f"若你有在 {state['entry']:.2f} 附近進場：15M 收盤跌破／突破最新保護價 "
                f"{state['protection']:.2f}，建議剩餘部位獲利平倉。",
            }
        )
    state["notified"] = notified
    return state, events
