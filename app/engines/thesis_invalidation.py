"""Deterministic, immutable thesis-based position invalidation."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

THESIS_VERSION = "thesis-risk-v1"
STATES = {
    "HEALTHY", "WARNING", "DEFEND", "SOFT_INVALIDATION_PENDING",
    "SOFT_INVALIDATED", "HARD_INVALIDATED", "RECOVERED",
}
TERMINAL_STATES = {"SOFT_INVALIDATED", "HARD_INVALIDATED"}


def _dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def build_trade_thesis(entry_plan: dict, *, created_at: str) -> dict:
    """Freeze warning, confirmation and hard-risk levels before entry."""
    direction = str(entry_plan["direction"])
    entry = float(entry_plan["suggested_entry"])
    strategy_stop = float(entry_plan["stop_loss"])
    atr = max(float(entry_plan.get("atr15") or 0), abs(entry - strategy_stop), 0.01)
    strategy = str(entry_plan.get("strategy_type") or entry_plan.get("setup_type")
                   or "STRUCTURE").upper()
    structural = entry_plan.get("hard_invalidation")
    if structural is None and "SWEEP_RECLAIM" in strategy:
        structural = (entry_plan.get("sweep_low") if direction == "LONG"
                      else entry_plan.get("sweep_high"))
    hard = float(structural) if isinstance(structural, (int, float)) else strategy_stop
    sign = 1 if direction == "LONG" else -1
    if sign * (entry - hard) <= 0:
        raise ValueError("結構硬失效價必須位於進場價的不利方向")
    warning_raw = entry_plan.get("warning_level")
    warning = (float(warning_raw) if isinstance(warning_raw, (int, float))
               else entry - sign * abs(entry - hard) * 0.35)
    if not (sign * (entry - warning) > 0 and sign * (warning - hard) > 0):
        warning = entry - sign * abs(entry - hard) * 0.35
    max_bars = max(1, int(entry_plan.get("reclaim_max_bars") or 1))
    emergency_buffer = max(atr * float(entry_plan.get("emergency_atr") or 0.25),
                           abs(entry - hard) * 0.15)
    emergency = hard - sign * emergency_buffer
    setup_id = str(entry_plan["setup_id"])
    thesis_id = "TH-" + hashlib.sha256(
        f"{setup_id}|{direction}|{strategy}|{entry}|{warning}|{hard}".encode()
    ).hexdigest()[:16]
    allowed_risk = max(float(entry_plan.get("risk_per_trade") or 1.0), 0.0001)
    contract_value = max(float(entry_plan.get("contract_value_per_point") or 1.0), 0.0001)
    spread = max(float(entry_plan.get("spread") or 0), 0)
    slippage = max(float(entry_plan.get("slippage") or atr * 0.02), 0)
    effective_distance = abs(entry - emergency) + spread + slippage
    position_size = allowed_risk / (effective_distance * contract_value)
    evidence = list(entry_plan.get("thesis_evidence") or [])
    description = str(entry_plan.get("thesis_description") or (
        f"{hard:.2f} 結構成立後的 {strategy} {direction} 交易論點"))
    return {
        "thesisId": thesis_id, "setupId": setup_id, "direction": direction,
        "referenceEntry": entry,
        "strategyType": strategy, "thesisType": strategy,
        "thesisDescription": description, "evidence": evidence,
        "createdAt": created_at, "confirmation": str(
            entry_plan.get("trigger_condition") or "已收盤進場條件成立"),
        "warningLevel": round(warning, 4),
        "softInvalidation": {
            "timeframe": "15M", "closeCondition": (
                f"closeBelow({warning:.4f})" if direction == "LONG"
                else f"closeAbove({warning:.4f})"),
            "reclaimWindow": f"{max_bars}x15M", "reclaimCondition": (
                f"closeAbove({warning:.4f})" if direction == "LONG"
                else f"closeBelow({warning:.4f})"),
            "maxBars": max_bars,
        },
        "hardInvalidation": {
            "level": round(hard, 4), "acceptanceBars": 1,
            "condition": (f"15M 收盤跌破 {hard:.2f} 且市場接受於下方"
                          if direction == "LONG" else
                          f"15M 收盤站上 {hard:.2f} 且市場接受於上方"),
        },
        "emergencyStop": round(emergency, 4),
        "riskBudget": allowed_risk, "riskPerTrade": allowed_risk,
        "stopDistance": round(effective_distance, 4),
        "positionSize": round(position_size, 6),
        "contractValuePerPoint": contract_value,
        "positionSizingFormula": "allowedRisk/(stopDistance*contractValuePerPoint)",
        "targets": [float(entry_plan[key]) for key in (
            "take_profit_1", "take_profit_2", "take_profit_3")
            if isinstance(entry_plan.get(key), (int, float))],
        "maeProfile": dict(entry_plan.get("mae_profile") or {
            "status": "INSUFFICIENT_SAMPLE", "source": "setup-specific fallback"}),
        "expectedReclaimTime": f"{max_bars} 根 15M 內",
        "version": THESIS_VERSION,
    }


def initial_invalidation_state(thesis: dict) -> dict:
    return {
        "state": "HEALTHY", "reasonCode": "HOLD_THESIS_HEALTHY",
        "reclaimDeadline": "", "pendingCandleTime": "",
        "lastEvaluatedCandleTime": "", "recoveryQuality": 0,
        "holdJustification": "交易論點完整，固定失效條件尚未觸發",
        "transitionCount": 0,
    }


def _breached(direction: str, price: float, level: float) -> bool:
    return price < level if direction == "LONG" else price > level


def _reclaimed(direction: str, price: float, level: float) -> bool:
    return price > level if direction == "LONG" else price < level


def evaluate_invalidation(
    thesis: dict, previous: dict | None, *, current_price: float,
    closed_price: float | None, candle_close_time: str, atr15: float,
    regime: str = "", data_status: str = "GOOD",
) -> tuple[dict, list[dict]]:
    """Evaluate transitions; intrabar warning never becomes a strategy exit."""
    state = dict(previous or initial_invalidation_state(thesis))
    old = str(state.get("state") or "HEALTHY")
    if old in TERMINAL_STATES:
        return state, []
    direction = str(thesis["direction"])
    warning = float(thesis["warningLevel"])
    hard = float(thesis["hardInvalidation"]["level"])
    emergency = float(thesis["emergencyStop"])
    new_candle = bool(candle_close_time and candle_close_time != state.get(
        "lastEvaluatedCandleTime"))
    now = _dt(candle_close_time) if candle_close_time else datetime.now(timezone.utc)
    events: list[dict] = []
    new, reason = old, str(state.get("reasonCode") or "HOLD_THESIS_HEALTHY")

    monetary_loss = max(0.0, (float(thesis["referenceEntry"]) - current_price)
                        if direction == "LONG" else
                        (current_price - float(thesis["referenceEntry"]))) * float(
                            thesis["positionSize"]) * float(thesis["contractValuePerPoint"])
    emergency_hit = _breached(direction, current_price, emergency)
    max_loss_hit = monetary_loss >= float(thesis["riskBudget"])
    if emergency_hit or max_loss_hit:
        new, reason = "HARD_INVALIDATED", (
            "EXIT_EMERGENCY_STOP" if emergency_hit else
            "EXIT_MAXIMUM_RISK")
    elif data_status in {"STALE", "FAILED"}:
        new, reason = "DEFEND", "POSITION_DATA_RISK"
    elif new_candle and isinstance(closed_price, (int, float)) and _breached(
            direction, float(closed_price), hard):
        new, reason = "HARD_INVALIDATED", "EXIT_HARD_INVALIDATION"
    elif old == "SOFT_INVALIDATION_PENDING" and new_candle:
        if isinstance(closed_price, (int, float)) and _reclaimed(
                direction, float(closed_price), warning):
            new, reason = "RECOVERED", "HOLD_RECOVERED"
            distance = abs(float(closed_price) - warning)
            state["recoveryQuality"] = max(1, min(100, round(
                50 + distance / max(atr15, 0.01) * 50)))
        elif now >= _dt(str(state.get("reclaimDeadline") or candle_close_time)):
            new, reason = "SOFT_INVALIDATED", "EXIT_SOFT_INVALIDATION"
    elif new_candle and isinstance(closed_price, (int, float)) and _breached(
            direction, float(closed_price), warning):
        adverse_trend = ((direction == "LONG" and regime == "TRENDING_BEAR")
                         or (direction == "SHORT" and regime == "TRENDING_BULL"))
        penetration = abs(float(closed_price) - warning) / max(atr15, 0.01)
        if adverse_trend and penetration >= 0.25:
            new, reason = "SOFT_INVALIDATED", "EXIT_SOFT_INVALIDATION_TREND_ACCELERATION"
        else:
            new, reason = "SOFT_INVALIDATION_PENDING", "HOLD_RECLAIM_WINDOW_ACTIVE"
            bars = int(thesis["softInvalidation"]["maxBars"])
            state["reclaimDeadline"] = (now + timedelta(minutes=15 * bars)).isoformat()
            state["pendingCandleTime"] = candle_close_time
    elif _breached(direction, current_price, warning):
        new, reason = "WARNING", "DEFEND_WARNING_BREACH"
    elif old in {"WARNING", "DEFEND", "RECOVERED"} and _reclaimed(
            direction, current_price, warning):
        new, reason = "RECOVERED", "HOLD_RECOVERED"
    else:
        new, reason = "HEALTHY", "HOLD_THESIS_HEALTHY"

    if new_candle:
        state["lastEvaluatedCandleTime"] = candle_close_time
    state.update({
        "state": new, "reasonCode": reason,
        "holdJustification": (
            "失效尚未成立，且仍在固定 reclaim window 內"
            if reason == "HOLD_RECLAIM_WINDOW_ACTIVE" else
            "盤中警戒線遭測試，禁止加碼並等待收盤確認"
            if reason == "DEFEND_WARNING_BREACH" else
            "價格已重新收回警戒線，原交易論點仍有效"
            if reason == "HOLD_RECOVERED" else
            "交易論點完整，固定失效條件尚未觸發"),
        "currentPrice": current_price, "closedPrice": closed_price,
        "remainingRR": None, "regime": regime, "dataStatus": data_status,
    })
    if new != old:
        state["transitionCount"] = int(state.get("transitionCount") or 0) + 1
        event_type = {
            "WARNING": "POSITION_WARNING",
            "DEFEND": "POSITION_DATA_RISK",
            "SOFT_INVALIDATION_PENDING": "SOFT_INVALIDATION_PENDING",
            "SOFT_INVALIDATED": "SOFT_INVALIDATED",
            "HARD_INVALIDATED": "HARD_INVALIDATED",
            "RECOVERED": "POSITION_RECOVERED",
        }.get(new)
        if event_type:
            events.append({
                "event_type": event_type, "setupId": thesis["setupId"],
                "tradePlanId": thesis.get("tradePlanId", ""),
                "positionId": thesis.get("tradePlanId", ""),
                "direction": direction, "previousState": old,
                "currentState": new, "reasonCode": reason,
                "warningLevel": warning, "hardInvalidation": hard,
                "emergencyStop": emergency, "reclaimDeadline": state.get(
                    "reclaimDeadline", ""), "currentPrice": current_price,
                "closedPrice": closed_price, "candleCloseTime": candle_close_time,
                "tradeThesis": thesis,
            })
    targets = list(thesis.get("targets") or [])
    if targets:
        reward = ((targets[0] - current_price) if direction == "LONG"
                  else (current_price - targets[0]))
        downside = abs(current_price - float(thesis["emergencyStop"]))
        state["remainingRR"] = round(max(0.0, reward) / max(downside, 0.0001), 3)
    return state, events


def validate_immutable_thesis(original: dict, candidate: dict) -> None:
    """Anti-moving-goalpost invariant for all pre-entry risk fields."""
    fixed = ("warningLevel", "softInvalidation", "hardInvalidation",
             "emergencyStop", "riskBudget", "positionSize")
    changed = [key for key in fixed if original.get(key) != candidate.get(key)]
    if changed:
        raise ValueError("交易建立後不得修改固定風控：" + ", ".join(changed))
