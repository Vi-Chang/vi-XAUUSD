"""Immutable-at-creation conditional position-management plans."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

CALCULATION_VERSION = "trade-plan-v1"
DEFAULT_TP_PERCENTAGES = (30, 30, 40)
logger = logging.getLogger(__name__)


def migrate_legacy_virtual_profit(legacy: dict | None, *, symbol: str,
                                  calculated_at: str) -> dict:
    """Idempotently preserve an active pre-v1 virtual position across deployment."""
    legacy = legacy or {}
    if not legacy.get("active") or not legacy.get("setup_id"):
        return {"plans": {}, "activePlans": [], "errors": [],
                "calculationVersion": CALCULATION_VERSION}
    entry = {
        "setup_id": legacy.get("setup_id"), "direction": legacy.get("direction"),
        "suggested_entry": legacy.get("entry"), "stop_loss": legacy.get("original_stop"),
        "take_profit_1": legacy.get("tp1"), "take_profit_2": legacy.get("tp2"),
        "take_profit_3": legacy.get("tp3"),
    }
    plan, error = build_trade_plan(entry, symbol=symbol, created_at=calculated_at)
    if error:
        return {"plans": {}, "activePlans": [], "errors": [error],
                "calculationVersion": CALCULATION_VERSION}
    completed = [f"TAKE_PROFIT_{i}" for i in range(1, 4)
                 if f"TP{i}" in set(legacy.get("notified") or [])]
    plan.update({
        "trailingStopPrice": float(legacy.get("protection") or plan["initialStop"]),
        "completedEvents": completed, "migrationSource": "virtual_profit_v0",
    })
    return {"plans": {plan["tradePlanId"]: plan}, "activePlans": [plan],
            "errors": [], "calculationVersion": CALCULATION_VERSION}


def _valid_target(direction: str, entry: float, value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    target = float(value)
    return target if ((direction == "LONG" and target > entry) or
                      (direction == "SHORT" and target < entry)) else None


def build_trade_plan(entry_plan: dict, *, symbol: str, created_at: str) -> tuple[dict, str]:
    """Build one fixed plan, preferring strategy targets then filling with R levels."""
    required = ("setup_id", "direction", "suggested_entry", "stop_loss")
    missing = [name for name in required if entry_plan.get(name) in (None, "")]
    if missing:
        return {}, f"缺少 {', '.join(missing)} 資料"
    direction = str(entry_plan["direction"])
    if direction not in ("LONG", "SHORT"):
        return {}, "缺少有效 direction 資料"
    entry, stop = float(entry_plan["suggested_entry"]), float(entry_plan["stop_loss"])
    risk = abs(entry - stop)
    if risk <= 0:
        return {}, "進場價與防守價距離無效"
    sign = 1 if direction == "LONG" else -1
    structural = [_valid_target(direction, entry, entry_plan.get(f"take_profit_{i}"))
                  for i in range(1, 4)]
    targets: list[float] = []
    basis: list[str] = []
    for index in range(3):
        value = structural[index]
        if value is None or (targets and sign * (value - targets[-1]) <= 0):
            value = entry + sign * risk * (index + 1)
            basis.append(f"TP{index + 1}=參考進場±{index + 1}R")
        else:
            basis.append(f"TP{index + 1}=既有策略結構目標")
        targets.append(round(value, 2))
    setup_id = str(entry_plan["setup_id"])
    seed = f"{symbol}|{setup_id}|{direction}|{entry:.2f}|{stop:.2f}"
    plan_id = f"TP-{hashlib.sha256(seed.encode()).hexdigest()[:16]}"
    expires = str(entry_plan.get("expires_at") or "")
    if not expires:
        try:
            expires = (datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                       + timedelta(hours=12)).isoformat()
        except ValueError:
            expires = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    return {
        "tradePlanId": plan_id, "setupId": setup_id, "symbol": symbol,
        "direction": direction, "referenceEntry": entry,
        "entryZoneLow": float(entry_plan.get("zone_low") or entry),
        "entryZoneHigh": float(entry_plan.get("zone_high") or entry),
        "initialStop": stop, "riskDistance": round(risk, 4),
        "tp1Price": targets[0], "tp1Percent": DEFAULT_TP_PERCENTAGES[0],
        "tp2Price": targets[1], "tp2Percent": DEFAULT_TP_PERCENTAGES[1],
        "tp3Price": targets[2], "tp3Percent": DEFAULT_TP_PERCENTAGES[2],
        "earlyExitCondition": (
            "最新已收盤 15M 跌破追蹤支撐" if direction == "LONG"
            else "最新已收盤 15M 站回追蹤壓力"
        ),
        "trailingStopPrice": stop, "createdAt": created_at,
        "expiresAt": expires, "status": "ACTIVE", "completedEvents": [],
        "calculationBasis": basis, "calculationVersion": CALCULATION_VERSION,
        "lastEvent": "", "currentR": 0.0,
    }, ""


def _reached(direction: str, price: float, level: float) -> bool:
    return price >= level if direction == "LONG" else price <= level


def evaluate_trade_plans(
    entry_plan: dict, previous: dict | None, *, symbol: str, current_price: float,
    closed_price: float | None, latest_structure_protection: float | None,
    candle_close_time: str, calculated_at: str,
) -> tuple[dict, list[dict]]:
    """Persist progress and emit each tradePlanId/event/target only once."""
    state = dict(previous or {})
    plans = {key: dict(value) for key, value in (state.get("plans") or {}).items()}
    errors = list(state.get("errors") or [])
    if entry_plan.get("status") == "ENTRY_TRIGGERED":
        candidate, error = build_trade_plan(entry_plan, symbol=symbol,
                                             created_at=calculated_at)
        if error:
            if error not in errors:
                errors.append(error)
                logger.error("trade plan was not created: %s", error)
        elif candidate["tradePlanId"] not in plans:
            plans[candidate["tradePlanId"]] = candidate

    events: list[dict] = []
    for plan in plans.values():
        if plan.get("status") != "ACTIVE":
            continue
        try:
            expired = bool(plan.get("expiresAt")) and (
                datetime.fromisoformat(str(plan["expiresAt"]).replace("Z", "+00:00"))
                <= datetime.fromisoformat(calculated_at.replace("Z", "+00:00")))
        except ValueError:
            expired = False
        if expired:
            plan["status"] = "EXPIRED"
            continue
        event_count_before = len(events)
        direction = str(plan["direction"])
        sign = 1 if direction == "LONG" else -1
        completed = list(plan.get("completedEvents") or [])
        plan["currentR"] = round(
            sign * (current_price - float(plan["referenceEntry"]))
            / float(plan["riskDistance"]), 2)
        stop_hit = _reached("SHORT" if direction == "LONG" else "LONG",
                            current_price, float(plan["initialStop"]))
        if stop_hit and "STOP_TRIGGERED" not in completed:
            completed.append("STOP_TRIGGERED")
            plan["status"] = "STOPPED"
            events.append(_event(plan, "STOP_TRIGGERED", current_price,
                                 candle_close_time, target_index=0,
                                 percent=100, next_level=None))
        if plan["status"] != "ACTIVE":
            plan["completedEvents"] = completed
            continue
        for index in range(1, 4):
            event_type = f"TAKE_PROFIT_{index}"
            target = float(plan[f"tp{index}Price"])
            if _reached(direction, current_price, target) and event_type not in completed:
                completed.append(event_type)
                protection = (float(plan["referenceEntry"]) if index == 1
                              else float(plan[f"tp{index - 1}Price"]))
                plan["trailingStopPrice"] = protection
                next_level = (float(plan[f"tp{index + 1}Price"])
                              if index < 3 else None)
                events.append(_event(
                    plan, event_type, current_price, candle_close_time,
                    target_index=index, percent=int(plan[f"tp{index}Percent"]),
                    next_level=next_level))
        if "TAKE_PROFIT_3" in completed and latest_structure_protection is not None:
            old = float(plan["trailingStopPrice"])
            updated = (max(old, latest_structure_protection) if direction == "LONG"
                       else min(old, latest_structure_protection))
            if updated != old:
                plan["trailingStopPrice"] = round(updated, 2)
                key = f"TRAILING_STOP_UPDATE:{updated:.2f}"
                if key not in completed:
                    completed.append(key)
                    events.append(_event(plan, "TRAILING_STOP_UPDATE", current_price,
                                         candle_close_time, target_index=3,
                                         percent=0, next_level=updated))
        early_hit = isinstance(closed_price, (int, float)) and (
            (direction == "LONG" and closed_price < float(plan["trailingStopPrice"]))
            or (direction == "SHORT" and closed_price > float(plan["trailingStopPrice"]))
        )
        if early_hit and "EARLY_EXIT" not in completed:
            completed.append("EARLY_EXIT")
            plan["status"] = "EXITED"
            events.append(_event(plan, "EARLY_EXIT", current_price,
                                 candle_close_time, target_index=0,
                                 percent=100, next_level=None,
                                 closed_price=float(closed_price)))
        plan["completedEvents"] = completed
        if len(events) > event_count_before:
            plan["lastEvent"] = events[-1]["event_type"]
    active = [p for p in plans.values() if p.get("status") == "ACTIVE"]
    return {"plans": plans, "activePlans": active, "errors": errors,
            "calculationVersion": CALCULATION_VERSION}, events


def _event(plan: dict, event_type: str, price: float, candle_time: str, *,
           target_index: int, percent: int, next_level: float | None,
           closed_price: float | None = None) -> dict:
    return {
        "event_type": event_type, "tradePlanId": plan["tradePlanId"],
        "setupId": plan["setupId"], "side": plan["direction"],
        "targetIndex": target_index, "price": price,
        "targetPrice": (plan.get(f"tp{target_index}Price") if target_index else None),
        "percent": percent, "remainingPercent": max(0, 100 - sum(
            int(plan[f"tp{i}Percent"]) for i in range(1, target_index + 1))),
        "newProtectionPrice": plan["trailingStopPrice"], "nextLevel": next_level,
        "closedPrice": closed_price, "candle_close_time": candle_time,
        "earlyExitCondition": plan["earlyExitCondition"],
        "topic": f"trade-plan:{plan['tradePlanId']}:{event_type}:{target_index}",
    }
