"""Immutable breakout/retest setup ledger; prevents trigger-level mixing."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

SETUP_VERSION = "breakout-setup-v1"
TERMINAL = {"MISSED_ENTRY", "INVALIDATED", "EXPIRED"}


def _level(normalized: dict, kind: str) -> dict | None:
    return next((item for item in normalized.get("confirmationLevels") or []
                 if item.get("kind") == kind and item.get("timeframe") == "15M"
                 and isinstance(item.get("price"), (int, float))), None)


def _id(symbol: str, direction: str, trigger: float, candle_time: str) -> str:
    seed = f"{symbol}|{direction}|{trigger:.2f}|{candle_time}"
    return f"BO-{hashlib.sha256(seed.encode()).hexdigest()[:16]}"


def build_breakout_setup(data: dict, *, direction: str, previous_setup_id: str = "",
                         reason: str = "INITIAL_STRUCTURE") -> tuple[dict, str]:
    normalized = data.get("normalized_analysis") or {}
    symbol = str(data.get("symbol") or "XAUUSD")
    trigger_item = _level(normalized, "resistance" if direction == "LONG" else "support")
    opposite = _level(normalized, "support" if direction == "LONG" else "resistance")
    if not trigger_item or not opposite:
        return {}, "缺少 15M 突破門檻或防守結構"
    trigger = float(trigger_item["price"])
    buffer = max(float(trigger_item.get("buffer") or 0),
                 float(normalized.get("atr15") or 0) * 0.10)
    atr = max(float(normalized.get("atr15") or 0), buffer, 0.01)
    sign = 1 if direction == "LONG" else -1
    stop = float(opposite["price"]) - sign * float(opposite.get("buffer") or atr * 0.1)
    entry_low, entry_high = sorted((trigger - buffer, trigger + buffer))
    risk = abs(trigger - stop)
    entry_engine = data.get("entry_engine") or {}
    raw_targets = [entry_engine.get(f"take_profit_{i}") for i in range(1, 4)]
    targets: list[float] = []
    for index, value in enumerate(raw_targets, 1):
        valid = isinstance(value, (int, float)) and sign * (float(value) - trigger) > 0
        targets.append(round(float(value) if valid else trigger + sign * risk * index, 2))
    candle_time = str(normalized.get("lastClosedCandleTimestamp") or "")
    created = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    setup_id = _id(symbol, direction, trigger, candle_time)
    expires = (datetime.fromisoformat(created.replace("Z", "+00:00"))
               + timedelta(hours=2)).isoformat()
    return {
        "setupId": setup_id, "setupVersion": SETUP_VERSION,
        "direction": direction, "createdAt": created,
        "breakoutTrigger": round(trigger, 2), "breakoutConfirmedAt": None,
        "confirmedCandleTime": None,
        "retestZoneLow": round(entry_low, 2), "retestZoneHigh": round(entry_high, 2),
        "entryZoneLow": round(entry_low, 2), "entryZoneHigh": round(entry_high, 2),
        "maxChasePrice": round(trigger + sign * atr * 0.35, 2),
        "stopPrice": round(stop, 2), "tp1": targets[0], "tp2": targets[1],
        "tp3": targets[2], "expiresAt": expires,
        "status": "WAIT_BREAKOUT_CONFIRMATION", "blockedReason": "等待15M收盤確認",
        "previousSetupId": previous_setup_id or None,
        "triggerChangeReason": reason, "oldTrigger": None, "newTrigger": round(trigger, 2),
        "createdFromCandleTime": candle_time, "entryType": None,
    }, ""


def _rr_ok(setup: dict) -> bool:
    risk = abs(float(setup["entryZoneHigh"] if setup["direction"] == "LONG"
                     else setup["entryZoneLow"]) - float(setup["stopPrice"]))
    reward = abs(float(setup["tp1"]) - float(setup["entryZoneHigh"]
                 if setup["direction"] == "LONG" else setup["entryZoneLow"]))
    return risk > 0 and reward / risk >= 1.5


def _evaluate_setup(setup: dict, data: dict) -> tuple[dict, list[dict]]:
    normalized = data.get("normalized_analysis") or {}
    now = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    closed = normalized.get("lastClosedCandlePrice")
    candle_time = str(normalized.get("lastClosedCandleTimestamp") or "")
    price = float(normalized.get("currentPrice") or 0)
    result, events = dict(setup), []
    old = str(result["status"])
    try:
        if datetime.fromisoformat(now.replace("Z", "+00:00")) >= datetime.fromisoformat(
                str(result["expiresAt"]).replace("Z", "+00:00")):
            result.update(status="EXPIRED", blockedReason="交易劇本已到期")
    except ValueError:
        pass
    if result["status"] in TERMINAL:
        return result, ([_event(result, old, result["status"], "SETUP_EXPIRED")]
                        if old != result["status"] else [])
    direction, trigger = result["direction"], float(result["breakoutTrigger"])
    confirmed = isinstance(closed, (int, float)) and (
        (direction == "LONG" and float(closed) > trigger)
        or (direction == "SHORT" and float(closed) < trigger))
    if old == "WAIT_BREAKOUT_CONFIRMATION" and confirmed:
        result.update(status="BREAKOUT_CONFIRMED", breakoutConfirmedAt=now,
                      confirmedCandleTime=candle_time,
                      blockedReason="突破已由15M收盤確認，評估突破進場或回踩")
        events.append(_event(result, old, result["status"], "BREAKOUT_CONFIRMED"))
        old = "BREAKOUT_CONFIRMED"
    if old == "BREAKOUT_CONFIRMED":
        within_chase = (price <= float(result["maxChasePrice"]) if direction == "LONG"
                        else price >= float(result["maxChasePrice"]))
        in_entry = float(result["entryZoneLow"]) <= price <= float(result["entryZoneHigh"])
        if (within_chase or in_entry) and _rr_ok(result) and normalized.get("marketDataStatus") == "GOOD":
            result.update(status="ENTRY_READY_BREAKOUT", entryType="BREAKOUT",
                          blockedReason="")
        else:
            result.update(status="WAIT_RETEST", blockedReason=(
                f"突破已確認；等待回踩 {result['retestZoneLow']:.2f}–"
                f"{result['retestZoneHigh']:.2f} 並由15M確認守住"))
    elif old == "WAIT_RETEST":
        in_retest = float(result["retestZoneLow"]) <= price <= float(result["retestZoneHigh"])
        retest_holds = in_retest and isinstance(closed, (int, float)) and (
            (direction == "LONG" and float(closed) >= trigger)
            or (direction == "SHORT" and float(closed) <= trigger))
        if retest_holds and _rr_ok(result) and normalized.get("marketDataStatus") == "GOOD":
            result.update(status="ENTRY_READY_RETEST", entryType="RETEST",
                          blockedReason="")
    if result["status"] != old:
        events.append(_event(result, old, result["status"], result["status"]))
    return result, events


def evaluate_breakout_setups(data: dict, previous: dict | None = None) -> tuple[dict, list[dict]]:
    state = dict(previous or {})
    setups = [dict(item) for item in state.get("setups") or []]
    events: list[dict] = []
    for index, setup in enumerate(setups):
        setups[index], setup_events = _evaluate_setup(setup, data)
        events.extend(setup_events)
    normalized = data.get("normalized_analysis") or {}
    direction = "LONG" if str(normalized.get("trendBias")) == "bullish" else "SHORT" if str(normalized.get("trendBias")) == "bearish" else ""
    trigger_item = _level(normalized, "resistance" if direction == "LONG" else "support") if direction else None
    latest = next((item for item in reversed(setups) if item["direction"] == direction), None)
    trigger = float(trigger_item["price"]) if trigger_item else None
    may_create = not latest or latest["status"] != "WAIT_BREAKOUT_CONFIRMATION"
    if trigger is not None and (not latest or float(latest["breakoutTrigger"]) != trigger) and may_create:
        reason = "NEW_CONFIRMED_15M_STRUCTURE" if latest else "INITIAL_15M_STRUCTURE"
        created, error = build_breakout_setup(
            data, direction=direction,
            previous_setup_id=str(latest.get("setupId") or "") if latest else "",
            reason=reason)
        if created:
            if latest:
                created["oldTrigger"] = latest["breakoutTrigger"]
            setups.append(created)
            events.append(_event(created, "NONE", created["status"], "NEW_SETUP_CREATED"))
        elif error:
            state["error"] = error
    return {"setups": setups, "activeSetup": setups[-1] if setups else None,
            "historyVersion": SETUP_VERSION, "error": state.get("error", "")}, events


def _event(setup: dict, previous: str, current: str, event_type: str) -> dict:
    return {
        "event_type": event_type, "setupId": setup["setupId"],
        "direction": setup["direction"], "previousState": previous,
        "currentState": current, "triggerPrice": setup["breakoutTrigger"],
        "entryZone": {"low": setup["entryZoneLow"], "high": setup["entryZoneHigh"]},
        "blockedReason": setup.get("blockedReason", ""),
        "setup": setup,
    }


def migrate_legacy_breakout_setup(data: dict, legacy: dict | None) -> dict:
    """Convert the last pre-ledger setup once; never rewrites its trigger later."""
    legacy = legacy or {}
    lifecycle = legacy.get("setupLifecycle") or legacy.get("setup_lifecycle") or {}
    trigger = lifecycle.get("confirmationPrice")
    direction = str(lifecycle.get("direction") or "")
    if not isinstance(trigger, (int, float)) or direction not in {"LONG", "SHORT"}:
        return {}
    setup, _ = build_breakout_setup(data, direction=direction,
                                    reason="MIGRATED_LEGACY_SETUP")
    if not setup:
        return {}
    setup.update(
        setupId=str(lifecycle.get("setupId") or setup["setupId"]),
        breakoutTrigger=round(float(trigger), 2),
        breakoutConfirmedAt=lifecycle.get("confirmedAt"),
        confirmedCandleTime=lifecycle.get("confirmedCandleTime"),
        status=("BREAKOUT_CONFIRMED" if lifecycle.get("confirmedAt")
                else "WAIT_BREAKOUT_CONFIRMATION"),
        blockedReason=("舊劇本已完成突破確認，等待重新評估進場路線"
                       if lifecycle.get("confirmedAt") else "等待15M收盤確認"),
    )
    return {"setups": [setup], "activeSetup": setup,
            "historyVersion": SETUP_VERSION, "migration": "legacy-final-decision-v1"}
