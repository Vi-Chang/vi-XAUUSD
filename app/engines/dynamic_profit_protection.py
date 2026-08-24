"""Per-position take-profit and profit-protection decisions."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pandas as pd

from app.config import get_settings
from app.engines.indicators import atr, macd, stochastic


def _num(value, default=None):
    return float(value) if isinstance(value, (int, float)) else default


def _position_class(position: dict) -> str:
    explicit = str(position.get("position_class") or "").upper()
    if explicit in {"CORE", "ADD_ON", "BREAKOUT", "SCALP"}:
        return explicit
    setup = str(position.get("strategy_type") or position.get("setup_type") or "").upper()
    return "BREAKOUT" if "BREAKOUT" in setup else "ADD_ON" if "ADD" in setup else "CORE"


def _positions(data: dict, trade_plans: dict) -> list[dict]:
    management = data.get("position_management") or {}
    explicit = list(management.get("positions") or [])
    if not explicit and management.get("has_position"):
        explicit = [{
            "position_id": management.get("position_id") or "actual-position",
            "side": management.get("position_side"), "entry_price": management.get("entry_price"),
            "position_size": management.get("position_size"),
            "position_class": management.get("position_class"),
        }]
    if explicit:
        return explicit
    # Conditional management remains available for triggered virtual plans.
    return [{"position_id": plan.get("tradePlanId"), "side": plan.get("direction"),
             "entry_price": plan.get("referenceEntry"), "position_size": plan.get("positionSize"),
             "position_class": plan.get("positionClass"), "virtual": True,
             "trade_plan_id": plan.get("tradePlanId")}
            for plan in trade_plans.get("activePlans") or []]


def _plan_for(position: dict, trade_plans: dict) -> dict:
    plans = list((trade_plans.get("plans") or {}).values())
    wanted = str(position.get("trade_plan_id") or position.get("tradePlanId") or "")
    return next((plan for plan in plans if plan.get("tradePlanId") == wanted),
                plans[0] if len(plans) == 1 else {})


def evaluate_dynamic_profit(
    *, data: dict, frame: pd.DataFrame | None, trade_plans: dict,
    break_state: dict, previous: dict | None = None
) -> tuple[dict, list[dict]]:
    previous = previous or {}
    settings = get_settings()
    price = _num((data.get("normalized_analysis") or {}).get("currentPrice"), 0.0)
    old_positions = {str(x.get("position_id")): x for x in previous.get("positions") or []}
    atr15 = max(_num((data.get("normalized_analysis") or {}).get("atr15"), 0.0), .01)
    stoch_k = 50.0
    macd_expanding = False
    rapid = False
    upper_rejection = lower_rejection = False
    if frame is not None and len(frame) >= 4:
        sample = frame.tail(6)
        atr15 = max(float(atr(frame).iloc[-1]), atr15)
        stoch_k = float(stochastic(frame)["stoch_k"].iloc[-1])
        hist = macd(frame["close"].astype(float))["macd_hist"]
        macd_expanding = float(hist.iloc[-1]) > float(hist.iloc[-2]) > 0
        move_atr = abs(float(sample["close"].iloc[-1] - sample["close"].iloc[0])) / atr15
        directional = int((sample["close"].diff().dropna() > 0).sum())
        rapid = (move_atr >= settings.rapid_extension_atr_threshold
                 and max(directional, len(sample) - 1 - directional) >= 4)
        last = sample.iloc[-1]
        span = max(float(last["high"] - last["low"]), .01)
        upper_rejection = (float(last["high"] - max(last["open"], last["close"])) / span >= .35)
        lower_rejection = (float(min(last["open"], last["close"]) - last["low"]) / span >= .35)
    indicator_stoch = _num(((data.get("indicator_snapshot") or {}).get("15M") or {}).get("stoch_k"))
    if indicator_stoch is not None:
        stoch_k = indicator_stoch
    now = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    results, events = [], []
    regime = str(break_state.get("market_regime") or "NORMAL")
    for position in _positions(data, trade_plans):
        pid = str(position.get("position_id") or position.get("id") or "")
        side = str(position.get("side") or "LONG").upper()
        entry = _num(position.get("entry_price"))
        if not pid or entry is None:
            continue
        sign = 1 if side == "LONG" else -1
        plan = _plan_for(position, trade_plans)
        targets = [float(x) for x in (plan.get("tp1Price"), plan.get("tp2Price"), plan.get("tp3Price"))
                   if isinstance(x, (int, float))]
        if not targets:
            risk = max(abs(entry - _num(position.get("stop_loss"), entry - sign * atr15)), atr15)
            targets = [entry + sign * risk * x for x in (1, 2, 3)]
        old = old_positions.get(pid) or {}
        old_peak = _num(old.get("peak_price_since_entry"), entry)
        peak = max(old_peak, price) if side == "LONG" else min(old_peak, price)
        current_profit = max(0.0, sign * (price - entry))
        max_profit = max(_num(old.get("max_unrealized_profit"), 0.0), sign * (peak - entry))
        giveback = max(0.0, max_profit - current_profit)
        giveback_ratio = giveback / max_profit if max_profit > 0 else 0.0
        reached = [index + 1 for index, target in enumerate(targets)
                   if (price >= target if side == "LONG" else price <= target)]
        tp_index = max(reached, default=0)
        extension_closed = extension_follow = retest_hold = False
        extreme = stoch_k >= 90 if side == "LONG" else stoch_k <= 10
        very_extreme = stoch_k >= 95 if side == "LONG" else stoch_k <= 5
        if frame is not None and len(frame) >= 3 and tp_index:
            tp = targets[min(tp_index - 1, len(targets) - 1)]
            closes = frame["close"].astype(float).tail(3)
            extension_closed = closes.iloc[-2] > tp if side == "LONG" else closes.iloc[-2] < tp
            extension_follow = closes.iloc[-1] > closes.iloc[-2] if side == "LONG" else closes.iloc[-1] < closes.iloc[-2]
            retest_hold = min(closes.iloc[-2:]) >= tp if side == "LONG" else max(closes.iloc[-2:]) <= tp
        extension_confirmed = bool(extension_closed and extension_follow and retest_hold)
        direction_rejection = upper_rejection if side == "LONG" else lower_rejection
        score = min(100, round(
            30 * bool(tp_index) + 15 * very_extreme + 10 * extreme + 18 * rapid
            + 12 * direction_rejection + 10 * (giveback_ratio >= .20)
            + 5 * (regime == "WHIPSAW")
            - 15 * extension_confirmed))
        pclass = _position_class(position)
        giveback_limits = {"CORE": .35, "ADD_ON": .22, "BREAKOUT": .18, "SCALP": .12}
        allowed = giveback_limits[pclass] * (.7 if regime == "WHIPSAW" else 1.0)
        single = _num(position.get("position_size"), 0.01) <= .01
        hard_stop = _num(plan.get("initialStop"), _num(position.get("stop_loss")))
        structure = _num((data.get("normalized_analysis") or {}).get(
            "structuralInvalidationLevel"), entry)
        structural_exit = _num(plan.get("strategyStop"), structure)
        hard_triggered = bool(hard_stop is not None and
                              ((side == "LONG" and price <= hard_stop) or
                               (side == "SHORT" and price >= hard_stop)))
        if hard_triggered:
            state, action = "HARD_RISK_STOP", "EXIT_NOW"
        elif (not tp_index and break_state.get("state") in {
                "FAILED_BREAKDOWN" if side == "LONG" else "FAILED_BREAKOUT"}):
            state, action = "PROFIT_BUILDING", "HOLD_WITH_CAUTION"
        elif not tp_index:
            state, action = "PROFIT_BUILDING", "HOLD"
        elif extension_confirmed:
            state, action = "EXTENSION_CONFIRMED", "LET_PROFIT_RUN"
        elif score >= settings.take_profit_high_score and (single or pclass in {"BREAKOUT", "SCALP"}):
            state, action = "TAKE_PROFIT", "TAKE_PROFIT"
        elif score >= 55 or giveback_ratio >= allowed:
            state, action = "PROFIT_PROTECTION", "HOLD_WITH_PROTECTION"
        else:
            state, action = "TP_REACHED", "TAKE_PROFIT_WATCH"
        old_protection = _num(old.get("profit_protection_level"),
                              _num(plan.get("trailingStopPrice"), entry))
        candidate_protection = (max(entry, price - atr15 * .75, structure) if side == "LONG"
                                else min(entry, price + atr15 * .75, structure))
        protection = (max(old_protection, candidate_protection) if side == "LONG"
                      else min(old_protection, candidate_protection))
        result = {
            "position_id": pid, "side": side, "position_class": pclass,
            "single_unit_position": single, "reference_entry": entry,
            "current_price": price, "peak_price_since_entry": peak,
            "current_unrealized_profit": round(current_profit, 3),
            "max_unrealized_profit": round(max_profit, 3), "max_profit_at": (
                now if max_profit > _num(old.get("max_unrealized_profit"), 0.0)
                else old.get("max_profit_at")),
            "profit_giveback": round(giveback, 3),
            "profit_giveback_ratio": round(giveback_ratio, 4),
            "max_allowed_giveback": round(allowed, 3),
            "tp_reached": bool(tp_index), "tp_index": tp_index, "targets": targets,
            "stochastic_k": round(stoch_k, 2), "extreme_condition": very_extreme,
            "rapid_extension": rapid, "macd_expanding": macd_expanding,
            "extension_confirmed": extension_confirmed,
            "take_profit_score": score,
            "take_profit_priority": "HIGH" if score >= settings.take_profit_high_score else "MEDIUM" if score >= 45 else "LOW",
            "profit_state": state, "position_action": action,
            "profit_protection_level": round(protection, 2),
            "hard_risk_stop": hard_stop,
            "hard_risk_stop_triggered": hard_triggered,
            "structural_exit_confirmation": structural_exit,
            "market_regime": regime, "updated_at": now,
            "reentry_setup_required": action == "TAKE_PROFIT",
            "reentry_rule": ("建立新的 setup_id，重新通過收盤確認、Executable RR 與追價限制"
                             if action == "TAKE_PROFIT" else None),
        }
        results.append(result)
        old_state = str(old.get("profit_state") or "")
        if state != old_state:
            event_type = ("EXIT_NOW" if state == "HARD_RISK_STOP" else
                          "TP_HIT" if state in {"TP_REACHED", "TAKE_PROFIT"} else
                          "TRAILING_STOP_UPDATE" if state in {"PROFIT_PROTECTION", "EXTENSION_CONFIRMED"}
                          else "PROFIT_STATE_CHANGED")
            seed = f"{pid}|{event_type}|{state}|{tp_index}"
            events.append({"eventId": hashlib.sha256(seed.encode()).hexdigest()[:32],
                           "event_type": event_type, "positionId": pid,
                           "previousState": old_state or "PROFIT_BUILDING",
                           "currentState": state, "positionProfitDecision": result,
                           "currentPrice": price, "targets": targets,
                           "candleCloseTime": str((data.get("normalized_analysis") or {}).get(
                               "lastClosedCandleTimestamp") or ""),
                           "calculatedAt": now, "transitionReason": action})
        if giveback_ratio >= allowed and not old.get("giveback_alerted"):
            events.append({"event_type": "PROFIT_GIVEBACK_ALERT", "positionId": pid,
                           "currentState": state, "positionProfitDecision": result,
                           "currentPrice": price, "calculatedAt": now,
                           "transitionReason": f"獲利已由 {max_profit:.2f} 回吐 {giveback_ratio:.0%}"})
            result["giveback_alerted"] = True
    return {"schemaVersion": "dynamic-profit-v1", "positions": results,
            "updatedAt": now}, events
