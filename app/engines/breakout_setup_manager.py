"""Immutable breakout/retest setup ledger; prevents trigger-level mixing."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

SETUP_VERSION = "confirmation-ladder-v3"
TERMINAL = {"MISSED_ENTRY", "INVALIDATED", "EXPIRED", "PULLBACK_INVALIDATED"}


def _chase_factor(data: dict) -> float:
    score = int((data.get("decision") or {}).get("signal_score") or
                (data.get("normalized_analysis") or {}).get("entryQualityScore") or 50)
    return 1.0 if score >= 80 else 0.8 if score >= 65 else 0.6


def assert_trigger_frozen(previous: dict, current: dict) -> None:
    """CI/runtime invariant: one setup id can never move its primary trigger."""
    if (previous.get("setupId") == current.get("setupId")
            and previous.get("triggerLocked")
            and float(previous.get("primaryTrigger") or previous.get("breakoutTrigger"))
            != float(current.get("primaryTrigger") or current.get("breakoutTrigger"))):
        raise AssertionError("MOVING_GOALPOST: primaryTrigger changed for locked setup")


def _level(normalized: dict, kind: str) -> dict | None:
    return next((item for item in normalized.get("confirmationLevels") or []
                 if item.get("kind") == kind and item.get("timeframe") == "15M"
                 and isinstance(item.get("price"), (int, float))), None)


def _pullback_zone(normalized: dict, *, direction: str, trigger: float,
                   atr: float, buffer: float) -> tuple[dict, str]:
    """Find a structural pullback zone only when two independent bases overlap."""
    sign = 1 if direction == "LONG" else -1
    wanted = "support" if direction == "LONG" else "resistance"
    candidates: list[tuple[float, str]] = []
    labels = {"15M": "15 分鐘支撐", "1H": "1 小時支撐", "4H": "4 小時支撐"}
    for item in normalized.get("confirmationLevels") or []:
        if item.get("kind") != wanted or not isinstance(item.get("price"), (int, float)):
            continue
        timeframe = str(item.get("timeframe") or "結構")
        candidates.append((float(item["price"]), labels.get(timeframe, f"{timeframe} 結構")))

    # ATR retracement is independent from the discrete swing/support detector.
    candidates.append((trigger - sign * atr * 0.80, "ATR 波動回撤"))
    max_gap = max(atr * 0.55, buffer * 2.0)
    clusters: list[list[tuple[float, str]]] = []
    for candidate in sorted(candidates, key=lambda item: item[0]):
        target = next((cluster for cluster in clusters
                       if abs(sum(x[0] for x in cluster) / len(cluster) - candidate[0]) <= max_gap), None)
        (target if target is not None else clusters.append([candidate]))
        if target is not None:
            target.append(candidate)
    valid = [cluster for cluster in clusters if len({item[1] for item in cluster}) >= 2]
    if not valid:
        return {}, "回踩區未建立：少於兩項獨立結構依據重疊"
    if direction == "LONG":
        valid = [cluster for cluster in valid if min(item[0] for item in cluster) < trigger] or valid
        chosen = max(valid, key=lambda cluster: sum(item[0] for item in cluster) / len(cluster))
    else:
        valid = [cluster for cluster in valid if max(item[0] for item in cluster) > trigger] or valid
        chosen = min(valid, key=lambda cluster: sum(item[0] for item in cluster) / len(cluster))
    center = sum(item[0] for item in chosen) / len(chosen)
    half_width = max(buffer, atr * 0.16)
    reasons = list(dict.fromkeys(item[1] for item in chosen))
    invalidation = center - sign * max(atr * 0.35, half_width * 1.5)
    return {
        "pullbackEntryZoneLow": round(center - half_width, 2),
        "pullbackEntryZoneHigh": round(center + half_width, 2),
        "pullbackInvalidationPrice": round(invalidation, 2),
        "pullbackZoneReason": reasons,
        "pullbackZoneSummary": "＋".join(reasons) + "重疊的位置",
    }, ""


def _id(symbol: str, direction: str, trigger: float, candle_time: str) -> str:
    seed = f"{symbol}|{direction}|{trigger:.2f}|{candle_time}"
    return f"BO-{hashlib.sha256(seed.encode()).hexdigest()[:16]}"


def _number(value: object) -> float:
    if not isinstance(value, (int, float)):
        raise TypeError("expected numeric market value")
    return float(value)


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
    pullback, pullback_error = _pullback_zone(
        normalized, direction=direction, trigger=trigger, atr=atr, buffer=buffer)
    risk = abs(trigger - stop)
    chase_factor = _chase_factor(data)
    max_chase = trigger + sign * atr * chase_factor
    entry_engine = data.get("entry_engine") or {}
    raw_targets = [entry_engine.get(f"take_profit_{i}") for i in range(1, 4)]
    targets: list[float] = []
    for index, value in enumerate(raw_targets, 1):
        valid = isinstance(value, (int, float)) and sign * (float(value) - trigger) > 0
        targets.append(round(_number(value) if valid else trigger + sign * risk * index, 2))
    candle_time = str(normalized.get("lastClosedCandleTimestamp") or "")
    created = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    setup_id = _id(symbol, direction, trigger, candle_time)
    expires = (datetime.fromisoformat(created.replace("Z", "+00:00"))
               + timedelta(hours=2)).isoformat()
    return {
        "setupId": setup_id, "setupVersion": SETUP_VERSION,
        "scenarioVersion": 1,
        "direction": direction, "createdAt": created,
        "primaryTrigger": round(trigger, 2), "triggerLocked": True,
        "triggerType": ("15M_RECOVERY_CONFIRMATION" if direction == "LONG"
                        else "15M_BREAKDOWN_CONFIRMATION"),
        "confirmationTimeframe": "15M", "primaryTriggerConfirmed": False,
        "confirmedPrice": None,
        "breakoutTrigger": round(trigger, 2), "breakoutConfirmedAt": None,
        "confirmedCandleTime": None,
        "retestZoneLow": round(entry_low, 2), "retestZoneHigh": round(entry_high, 2),
        "entryZoneLow": round(entry_low, 2), "entryZoneHigh": round(entry_high, 2),
        "maxChasePrice": round(max_chase, 2),
        "maxChaseDistance": round(atr * chase_factor, 4),
        "chaseFactor": chase_factor,
        "executionZoneLow": round(min(trigger, max_chase), 2),
        "executionZoneHigh": round(max(trigger, max_chase), 2),
        "atr15": round(atr, 4),
        "stopPrice": round(stop, 2), "tp1": targets[0], "tp2": targets[1],
        "tp3": targets[2], "expiresAt": expires,
        "status": "WAIT_BREAKOUT_OR_PULLBACK", "tradeState": "WATCHING",
        "entryStyle": None, "blockedReason": (
            "同步等待15M收盤突破，或價格回到有效回踩區後止跌確認"),
        "previousSetupId": previous_setup_id or None,
        "triggerChangeReason": reason, "oldTrigger": None, "newTrigger": round(trigger, 2),
        "createdFromCandleTime": candle_time, "pullbackSourceCandleTime": candle_time,
        "entryType": None,
        "invalidation": round(stop, 2),
        "entryZone": {"low": round(min(trigger, max_chase), 2),
                      "high": round(max(trigger, max_chase), 2)},
        "levelRoles": {
            "primaryTrigger": "PRIMARY_TRIGGER", "entryZone": "ENTRY",
            "stopPrice": "INVALIDATION", "tp1": "TP1", "tp2": "TP2", "tp3": "TP3",
        },
        "pullbackZoneError": pullback_error,
        **pullback,
    }, ""


def _rr_ok(setup: dict) -> bool:
    from app.engines.scenario_safety import calculate_risk_reward
    entry = float(setup["entryZoneHigh"] if setup["direction"] == "LONG"
                  else setup["entryZoneLow"])
    rr = calculate_risk_reward(setup["direction"], evaluation_entry_price=entry,
                               stop_loss=float(setup["stopPrice"]),
                               target_price=float(setup["tp1"]))
    return bool(rr["available"] and rr["ratio"] >= 1.5)


def _pullback_rr_ok(setup: dict) -> bool:
    entry = _number(setup.get("pullbackEntryZoneHigh") if setup["direction"] == "LONG"
                    else setup.get("pullbackEntryZoneLow"))
    stop = float(setup.get("pullbackInvalidationPrice") or setup["stopPrice"])
    from app.engines.scenario_safety import calculate_risk_reward
    rr = calculate_risk_reward(setup["direction"], evaluation_entry_price=entry,
                               stop_loss=stop, target_price=float(setup["tp1"]))
    return bool(rr["available"] and rr["ratio"] >= 1.5)


def _htf_valid(normalized: dict, direction: str) -> bool:
    bias = str(normalized.get("trendBias") or "neutral")
    regime = str(normalized.get("marketRegime") or "")
    if direction == "LONG":
        return bias == "bullish" and regime not in {"bearish", "strong_bearish"}
    return bias == "bearish" and regime not in {"bullish", "strong_bullish"}


def _pullback_confirmed(setup: dict, data: dict, closed: float) -> tuple[bool, list[str]]:
    low = float(setup["pullbackEntryZoneLow"])
    high = float(setup["pullbackEntryZoneHigh"])
    direction = setup["direction"]
    evidence: list[str] = []
    candle = data.get("latest_closed_15m") or {}
    candle_low, candle_high = candle.get("low"), candle.get("high")
    candle_open = candle.get("open")
    touched = (isinstance(candle_low, (int, float)) and float(candle_low) <= high
               if direction == "LONG" else
               isinstance(candle_high, (int, float)) and float(candle_high) >= low)
    # When OHLC is unavailable, current/closed location still allows watch state,
    # but cannot manufacture a reversal confirmation.
    if touched and direction == "LONG" and closed >= high:
        evidence.append("15M 收盤重新站回回踩區上緣")
    if touched and direction == "SHORT" and closed <= low:
        evidence.append("15M 收盤重新跌回回踩區下緣")
    if all(isinstance(value, (int, float)) for value in (candle_open, candle_low, candle_high)):
        open_value, low_value, high_value = map(_number, (candle_open, candle_low, candle_high))
        body = abs(closed - open_value)
        if direction == "LONG" and closed > open_value and open_value - low_value > body:
            evidence.append("15M 長下影止跌")
        if direction == "SHORT" and closed < open_value and high_value - open_value > body:
            evidence.append("15M 長上影轉弱")
    return bool(evidence), evidence


def _evaluate_setup(setup: dict, data: dict) -> tuple[dict, list[dict]]:
    normalized = data.get("normalized_analysis") or {}
    now = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    closed = normalized.get("lastClosedCandlePrice")
    candle_time = str(normalized.get("lastClosedCandleTimestamp") or "")
    price = float(normalized.get("currentPrice") or 0)
    result, events = dict(setup), []
    result.setdefault("primaryTrigger", result.get("breakoutTrigger"))
    result.setdefault("triggerLocked", True)
    result.setdefault("primaryTriggerConfirmed", bool(result.get("breakoutConfirmedAt")))
    result.setdefault("tradeState", "READY" if result.get("breakoutConfirmedAt") else "WATCHING")
    if result.get("setupVersion") != SETUP_VERSION:
        frozen_trigger = float(result["primaryTrigger"])
        sign = 1 if result.get("direction") == "LONG" else -1
        atr = max(float(result.get("atr15") or normalized.get("atr15") or 0), .01)
        factor = _chase_factor(data)
        chase = frozen_trigger + sign * atr * factor
        result.update(
            setupVersion=SETUP_VERSION,
            triggerType=("15M_RECOVERY_CONFIRMATION" if sign == 1
                         else "15M_BREAKDOWN_CONFIRMATION"),
            confirmationTimeframe="15M", maxChaseDistance=round(atr * factor, 4),
            chaseFactor=factor, maxChasePrice=round(chase, 2),
            executionZoneLow=round(min(frozen_trigger, chase), 2),
            executionZoneHigh=round(max(frozen_trigger, chase), 2),
            invalidation=result.get("stopPrice"),
            entryZone={"low": round(min(frozen_trigger, chase), 2),
                       "high": round(max(frozen_trigger, chase), 2)},
            levelRoles={"primaryTrigger": "PRIMARY_TRIGGER", "entryZone": "ENTRY",
                        "stopPrice": "INVALIDATION", "tp1": "TP1",
                        "tp2": "TP2", "tp3": "TP3"},
        )
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
    if not _htf_valid(normalized, direction):
        result.update(status="PULLBACK_INVALIDATED",
                      blockedReason="1H／4H 背景方向已不再支持這個回踩劇本")
        return result, [_event(result, old, result["status"], "PULLBACK_INVALIDATED")]
    if old in {"WAIT_BREAKOUT_CONFIRMATION", "WAIT_BREAKOUT_OR_PULLBACK",
               "WAIT_PULLBACK_CONFIRMATION", "BREAKOUT_CONFIRMED"}:
        atr = max(float(normalized.get("atr15") or 0), 0.01)
        buffer = max((float(result["entryZoneHigh"]) - float(result["entryZoneLow"])) / 2,
                     atr * 0.10)
        refreshed, _ = _pullback_zone(
            normalized, direction=direction, trigger=trigger, atr=atr, buffer=buffer)
        old_center = ((float(result.get("pullbackEntryZoneLow") or 0)
                       + float(result.get("pullbackEntryZoneHigh") or 0)) / 2)
        new_center = ((float(refreshed.get("pullbackEntryZoneLow") or 0)
                       + float(refreshed.get("pullbackEntryZoneHigh") or 0)) / 2)
        source_changed = candle_time and candle_time != str(
            result.get("pullbackSourceCandleTime") or result.get("createdFromCandleTime") or "")
        if refreshed and source_changed and abs(new_center - old_center) >= atr * 0.15:
            result.update(refreshed)
            result["pullbackSourceCandleTime"] = candle_time
            events.append(_event(result, old, old, "PULLBACK_ZONE_UPDATED"))
    pullback_low = result.get("pullbackEntryZoneLow")
    pullback_high = result.get("pullbackEntryZoneHigh")
    has_pullback = isinstance(pullback_low, (int, float)) and isinstance(pullback_high, (int, float))
    if has_pullback and isinstance(closed, (int, float)):
        invalid = float(result.get("pullbackInvalidationPrice") or result["stopPrice"])
        broken = float(closed) < invalid if direction == "LONG" else float(closed) > invalid
        if broken and old not in {"BREAKOUT_ENTRY_READY", "PULLBACK_ENTRY_READY"}:
            result.update(status="PULLBACK_INVALIDATED",
                          blockedReason=f"15M 收盤已越過回踩失效價 {invalid:.2f}")
            events.append(_event(result, old, result["status"], "PULLBACK_INVALIDATED"))
            return result, events
        intrabar_breached = price < invalid if direction == "LONG" else price > invalid
        if intrabar_breached:
            result.update(
                status="PULLBACK_BREACH_PENDING_CLOSE",
                blockedReason=(f"即時價格已越過回踩失效價 {invalid:.2f}，但15M尚未收盤；"
                               "暫停這個回踩機會，等待收盤判定"),
            )
            if old != result["status"]:
                events.append(_event(result, old, result["status"],
                                     "PULLBACK_BREACH_PENDING_CLOSE"))
            return result, events
    confirmed = isinstance(closed, (int, float)) and (
        (direction == "LONG" and float(closed) > trigger)
        or (direction == "SHORT" and float(closed) < trigger))
    waiting = old in {"WAIT_BREAKOUT_CONFIRMATION", "WAIT_BREAKOUT_OR_PULLBACK",
                      "WAIT_PULLBACK_CONFIRMATION", "PULLBACK_BREACH_PENDING_CLOSE"}
    pullback_ready = False
    evidence: list[str] = []
    if waiting and has_pullback and isinstance(closed, (int, float)):
        in_pullback = _number(pullback_low) <= price <= _number(pullback_high)
        pullback_ready, evidence = _pullback_confirmed(result, data, float(closed))
        if in_pullback and pullback_ready and _pullback_rr_ok(result) and normalized.get("marketDataStatus") == "GOOD":
            result.update(status="PULLBACK_ENTRY_READY", entryType="PULLBACK",
                          pullbackConfirmationEvidence=evidence, blockedReason="")
            events.append(_event(result, old, result["status"], "PULLBACK_ENTRY_READY"))
            return result, events
    if waiting and confirmed:
        result.update(status="BREAKOUT_CONFIRMED", breakoutConfirmedAt=now,
                      confirmedCandleTime=candle_time,
                      primaryTriggerConfirmed=True, confirmedAt=now,
                      confirmedPrice=float(closed), tradeState="READY",
                      blockedReason="突破已由15M收盤確認，評估突破進場或回踩")
        events.append(_event(result, old, result["status"], "BREAKOUT_CONFIRMED"))
        old = "BREAKOUT_CONFIRMED"
    if old == "BREAKOUT_CONFIRMED":
        within_chase = (price <= float(result["maxChasePrice"]) if direction == "LONG"
                        else price >= float(result["maxChasePrice"]))
        in_entry = float(result["entryZoneLow"]) <= price <= float(result["entryZoneHigh"])
        if (within_chase or in_entry) and _rr_ok(result) and normalized.get("marketDataStatus") == "GOOD":
            move = abs(price - trigger)
            aggressive = move <= float(result.get("maxChaseDistance") or 0) * 0.45
            result.update(status="BREAKOUT_ENTRY_READY", entryType="BREAKOUT",
                          tradeState="ENTER", entryStyle=("AGGRESSIVE" if aggressive else "STANDARD"),
                          immediateEntry=True, entryReadyAt=now, blockedReason="")
        else:
            result.update(status="WAIT_RETEST", tradeState="MISSED", blockedReason=(
                f"突破已確認但超過追價上限；改等回踩 "
                f"{_number(pullback_low):.2f}–{_number(pullback_high):.2f} 止跌確認"
                if has_pullback else "突破已確認但超過追價上限；等待新結構"))
    elif old in {"BREAKOUT_ENTRY_READY", "PULLBACK_ENTRY_READY"}:
        if result.get("tradeState") == "ENTER":
            result.update(tradeState="MANAGE", managementState="HOLD",
                          blockedReason="訊號已確認，後續關鍵價用於持倉管理，不再新增進場門檻")
            events.append(_event(result, "ENTER", "MANAGE", "HOLD"))
    elif old == "WAIT_RETEST":
        result["tradeState"] = "MISSED"
        in_retest = float(result["retestZoneLow"]) <= price <= float(result["retestZoneHigh"])
        retest_holds = in_retest and isinstance(closed, (int, float)) and (
            (direction == "LONG" and float(closed) >= trigger)
            or (direction == "SHORT" and float(closed) <= trigger))
        if retest_holds and _rr_ok(result) and normalized.get("marketDataStatus") == "GOOD":
            result.update(status="PULLBACK_ENTRY_READY", entryType="PULLBACK",
                          tradeState="ENTER", entryStyle="RETEST", entryReadyAt=now,
                          blockedReason="")
    elif waiting and not confirmed:
        in_pullback = has_pullback and _number(pullback_low) <= price <= _number(pullback_high)
        result.update(status=("WAIT_PULLBACK_CONFIRMATION" if in_pullback
                              else "WAIT_BREAKOUT_OR_PULLBACK"),
                      blockedReason=("價格已進入回踩觀察區；等待15M止跌確認"
                                     if in_pullback else
                                     "同步等待15M收盤突破，或價格回到有效回踩區"))
    if result["status"] != old:
        events.append(_event(result, old, result["status"], result["status"]))
    if (not result.get("primaryTriggerConfirmed") and trigger
            and result.get("tradeState") in {"WATCHING", "ARMED"}):
        distance = abs(price - trigger)
        result["tradeState"] = "ARMED" if distance <= max(float(result.get("atr15") or 0) * .5, .01) else "WATCHING"
    assert_trigger_frozen(setup, result)
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
    # A new structure level may update targets/management context, but it cannot
    # become another mandatory confirmation while the current setup is alive.
    may_create = not latest or latest["status"] in TERMINAL
    if trigger is not None and (not latest or float(latest["breakoutTrigger"]) != trigger) and may_create:
        reason = "NEW_CONFIRMED_15M_STRUCTURE" if latest else "INITIAL_15M_STRUCTURE"
        created, error = build_breakout_setup(
            data, direction=direction,
            previous_setup_id=str(latest.get("setupId") or "") if latest else "",
            reason=reason)
        if created:
            if latest:
                created["oldTrigger"] = latest["breakoutTrigger"]
                created["scenarioVersion"] = int(latest.get("scenarioVersion") or 1) + 1
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
