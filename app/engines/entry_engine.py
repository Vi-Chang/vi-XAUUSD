"""Executable, no-lookahead entry lifecycle built from closed 5M/15M candles."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Literal

import pandas as pd

EntryStatus = Literal[
    "NO_SETUP",
    "SETUP_WATCH",
    "ENTRY_READY",
    "ENTRY_TRIGGERED",
    "INVALIDATED",
    "EXITED",
]
Direction = Literal["LONG", "SHORT", "NONE"]


@dataclass(frozen=True)
class EntryPlan:
    status: EntryStatus = "NO_SETUP"
    setup_id: str = ""
    direction: Direction = "NONE"
    zone_low: float | None = None
    zone_high: float | None = None
    trigger_timeframe: str = ""
    trigger_condition: str = ""
    suggested_entry: float | None = None
    stop_loss: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    take_profit_3: float | None = None
    risk_reward: float | None = None
    confidence_score: int = 0
    created_at: str = ""
    expires_at: str = ""
    cancel_condition: str = ""
    missing_condition: str = ""
    notified_states: tuple[str, ...] = ()
    quote_time: str = ""
    last_closed_candle_time: str = ""
    calculated_at: str = ""
    source_price: float | None = None
    market_state: str = ""
    version: int = 0


@dataclass(frozen=True)
class EntryEvaluation:
    plan: EntryPlan
    should_notify: bool = False
    message: str = ""


def _stable_id(direction: str, level: float, candle_time: str) -> str:
    seed = f"XAUUSD|{direction}|15M|{level:.2f}|{candle_time}"
    return f"XAU-{direction[0]}-{hashlib.sha256(seed.encode()).hexdigest()[:12]}"


def _zone_mid(zone: dict) -> float:
    return (float(zone["price_low"]) + float(zone["price_high"])) / 2


def _targets(data: dict, scenario: dict, direction: str, entry: float) -> list[float]:
    resolved = scenario.get("resolved_prices") or {}
    values = []
    for target_id in scenario.get("target_ids") or []:
        zone = resolved.get(target_id)
        if (
            not isinstance(zone, dict)
            or zone.get("price_low") is None
            or zone.get("price_high") is None
        ):
            continue
        # Use the near edge: conservative reward, consistent with live R/R.
        value = float(zone["price_low"] if direction == "LONG" else zone["price_high"])
        if (direction == "LONG" and value > entry) or (
            direction == "SHORT" and value < entry
        ):
            values.append(value)
    if len(values) < 2:
        key_levels = data.get("key_levels") or {}
        names = (
            ("strong_support_zones", "weak_support_zones")
            if direction == "SHORT"
            else ("strong_resistance_zones", "weak_resistance_zones")
        )
        for name in names:
            for zone in key_levels.get(name) or []:
                if zone.get("price_low") is None or zone.get("price_high") is None:
                    continue
                value = float(
                    zone["price_high"] if direction == "SHORT" else zone["price_low"]
                )
                if (direction == "LONG" and value > entry) or (
                    direction == "SHORT" and value < entry
                ):
                    values.append(value)
    return sorted(set(values), reverse=direction == "SHORT")[:2]


def ordered_profit_targets(
    direction: str, entry: float | None, *targets: float | None
) -> tuple[float | None, float | None, float | None]:
    """Return unique, profit-side targets from nearest to farthest.

    Structural targets and R-multiple targets come from different calculators.  This
    is the single boundary that prevents their labels from implying an impossible
    TP1/TP2/TP3 execution order.
    """
    if entry is None:
        return None, None, None
    valid = {
        round(float(value), 2)
        for value in targets
        if isinstance(value, (int, float))
        and ((direction == "LONG" and float(value) > entry)
             or (direction == "SHORT" and float(value) < entry))
    }
    ordered = sorted(valid, reverse=direction == "SHORT")[:3]
    return tuple((ordered + [None, None, None])[:3])


def validate_executable_plan(plan: EntryPlan) -> tuple[bool, str]:
    """Fail closed when a plan claims executability but its fields disagree."""
    if plan.status != "ENTRY_TRIGGERED":
        return True, ""
    if plan.missing_condition:
        return False, "進場條件仍有缺項"
    required = (plan.suggested_entry, plan.stop_loss, plan.take_profit_1)
    if not all(isinstance(value, (int, float)) for value in required):
        return False, "進場價、停損或第一止盈不完整"
    if plan.risk_reward is None or plan.risk_reward < 1.5:
        return False, "風險報酬比未達 1.5"
    targets = [value for value in (
        plan.take_profit_1, plan.take_profit_2, plan.take_profit_3
    ) if isinstance(value, (int, float))]
    expected = sorted(set(targets), reverse=plan.direction == "SHORT")
    if targets != expected:
        return False, "止盈價未依獲利方向排列"
    return True, ""


def _closed_frame(
    m5: pd.DataFrame | None, m15: pd.DataFrame | None
) -> tuple[str, pd.DataFrame | None]:
    for timeframe, frame in (("5M", m5), ("15M", m15)):
        if frame is not None and len(frame) >= 2:
            if "is_closed" in frame.columns:
                frame = frame[frame["is_closed"]]
            if len(frame) >= 2:
                return timeframe, frame
    return "", None


def reversal_evidence(
    direction: str, frame: pd.DataFrame, zone_low: float, zone_high: float
) -> tuple[bool, str, float]:
    """Evaluate only completed candles; returns (confirmed, reason, trigger close)."""
    previous, bar = frame.iloc[-2], frame.iloc[-1]
    o, h, low, close = (float(bar[k]) for k in ("open", "high", "low", "close"))
    ph, pl = (float(previous[k]) for k in ("high", "low"))
    candle_range = max(h - low, 1e-9)
    body = abs(close - o)
    touched = h >= zone_low and low <= zone_high
    if not touched:
        return False, "價格尚未進入進場觀察區", close
    if direction == "SHORT":
        checks = [
            (close < o and body / candle_range >= 0.45, "明確反轉空 K 收盤"),
            (
                h - max(o, close) >= max(body * 1.5, candle_range * 0.35)
                and close < zone_high,
                "長上影且收回壓力區下方",
            ),
            (h > zone_high and close < zone_low, "假突破後重新跌回區域"),
            (h < ph and close < pl, "形成更低高點並跌破觸發 K 低點"),
        ]
    else:
        checks = [
            (close > o and body / candle_range >= 0.45, "明確反轉多 K 收盤"),
            (
                min(o, close) - low >= max(body * 1.5, candle_range * 0.35)
                and close > zone_low,
                "長下影且重新站回支撐",
            ),
            (low < zone_low and close > zone_high, "假跌破後收回區域"),
            (low > pl and close > ph, "形成更高低點並突破觸發 K 高點"),
        ]
    matched = next((reason for ok, reason in checks if ok), "")
    return bool(matched), matched or "尚缺已收盤反轉 K、影線收回或高低點結構確認", close


def _build_candidate(data: dict, direction: str, *, now: datetime) -> EntryPlan:
    normalized = data.get("normalized_analysis") or {}
    from app.engines.short_alert_state import validate_alert_zones

    zone_error = validate_alert_zones(normalized)
    if zone_error:
        return EntryPlan(direction=direction, missing_condition=zone_error)
    scenario = (
        data.get("short_scenario" if direction == "SHORT" else "long_scenario") or {}
    )
    atr = float(normalized.get("atr15") or 0)
    support = next(
        (
            x
            for x in normalized.get("confirmationLevels", [])
            if x.get("kind") == "support" and x.get("timeframe") == "15M"
        ),
        None,
    )
    if not support or atr <= 0:
        return EntryPlan(
            direction=direction,
            missing_condition="缺少有效 15M 支撐區或 ATR，無法計算完整進場計畫",
        )
    level, buffer = float(support["price"]), float(support.get("buffer") or 0)
    zone_low, zone_high = level - buffer, level + buffer
    evaluation_entry = zone_low if direction == "SHORT" else zone_high
    invalidation = normalized.get("invalidationLevel")
    stop = (
        max(float(invalidation), zone_high + atr * 0.10)
        if direction == "SHORT" and isinstance(invalidation, (int, float))
        else zone_high + atr * 0.10
        if direction == "SHORT"
        else min(float(invalidation), zone_low - atr * 0.10)
        if isinstance(invalidation, (int, float))
        else zone_low - atr * 0.10
    )
    targets = _targets(data, scenario, direction, evaluation_entry)
    if len(targets) < 2:
        return EntryPlan(
            direction=direction,
            zone_low=round(zone_low, 2),
            zone_high=round(zone_high, 2),
            stop_loss=round(stop, 2),
            missing_condition="缺少兩個位於獲利方向的有效支撐／壓力目標",
        )
    risk = stop - evaluation_entry if direction == "SHORT" else evaluation_entry - stop
    reward = (
        evaluation_entry - targets[0]
        if direction == "SHORT"
        else targets[0] - evaluation_entry
    )
    if risk <= 0 or reward <= 0:
        return EntryPlan(
            direction=direction, missing_condition="進場、停損與第一止盈方向異常"
        )
    rr = round(reward / risk, 2)
    from app.config import get_settings

    settings = get_settings()
    min_rr = max(1.5, float(settings.setup_min_rr1))
    if rr < min_rr:
        return EntryPlan(
            direction=direction,
            zone_low=round(zone_low, 2),
            zone_high=round(zone_high, 2),
            suggested_entry=round(evaluation_entry, 2),
            stop_loss=round(stop, 2),
            take_profit_1=round(targets[0], 2),
            take_profit_2=round(targets[1], 2),
            risk_reward=rr,
            missing_condition=f"預估風險報酬比僅 {rr:.2f}，低於最低 {min_rr:.2f}",
        )
    closed_at = str(normalized.get("lastClosedCandleTimestamp") or now.isoformat())
    expires = (
        datetime.fromisoformat(closed_at)
        + timedelta(minutes=15 * (max(1, settings.tactical_setup_expiry_bars) + 1))
    ).isoformat()
    setup_id = _stable_id(direction, level, closed_at)
    return EntryPlan(
        status="SETUP_WATCH",
        setup_id=setup_id,
        direction=direction,
        zone_low=round(zone_low, 2),
        zone_high=round(zone_high, 2),
        suggested_entry=round(evaluation_entry, 2),
        stop_loss=round(stop, 2),
        take_profit_1=round(targets[0], 2),
        take_profit_2=round(targets[1], 2),
        risk_reward=rr,
        confidence_score=max(
            0, min(100, int(normalized.get("entryQualityScore") or 50))
        ),
        created_at=now.isoformat(),
        expires_at=expires,
        cancel_condition=(
            f"15M 收盤站回 {stop:.2f} 上方"
            if direction == "SHORT"
            else f"15M 收盤跌破 {stop:.2f}"
        ),
        missing_condition="價格尚未進入觀察區，且尚缺已收盤反轉 K 線確認",
        quote_time=str(normalized.get("marketDataTimestamp") or ""),
        last_closed_candle_time=closed_at,
        calculated_at=now.isoformat(),
        source_price=float(normalized.get("currentPrice") or 0),
        market_state=str(normalized.get("marketStateCode") or ""),
        version=int(data.get("version") or 0),
    )


def format_entry_message(plan: EntryPlan) -> str:
    direction = "做多" if plan.direction == "LONG" else "做空"
    if plan.status == "SETUP_WATCH":
        return (
            f"【目前狀態】{direction}準備中\n"
            f"【等待價區】{plan.zone_low:.2f}–{plan.zone_high:.2f}\n"
            f"【尚缺條件】{plan.missing_condition}\n"
            f"【成立後進場】預計於 {plan.suggested_entry:.2f} 附近，須等收盤觸發\n"
            f"【提前失效】{plan.cancel_condition}"
        )
    if plan.status == "ENTRY_READY":
        return (
            f"【目前狀態】{direction}準備中，價格已抵達觀察區\n"
            f"【等待價區】{plan.zone_low:.2f}–{plan.zone_high:.2f}\n"
            f"【尚缺條件】{plan.missing_condition}\n"
            f"【成立後進場】觸發 K 收盤約 {plan.suggested_entry:.2f}\n"
            f"【提前失效】{plan.cancel_condition}"
        )
    if plan.status == "ENTRY_TRIGGERED":
        risk = abs((plan.suggested_entry or 0) - (plan.stop_loss or 0))
        sign = 1 if plan.direction == "LONG" else -1
        tp1 = (plan.suggested_entry or 0) + sign * risk
        tp2 = (plan.suggested_entry or 0) + sign * risk * 2
        tp3 = (plan.suggested_entry or 0) + sign * risk * 3
        return (
            f"【可進場方向】{direction}\n"
            f"【進場區間】{plan.zone_low:.2f}–{plan.zone_high:.2f}\n"
            f"【觸發條件】{plan.trigger_timeframe} {plan.trigger_condition}\n"
            f"【建議進場】{plan.suggested_entry:.2f}\n【停損】{plan.stop_loss:.2f}\n"
            f"【TP1（1R）】{tp1:.2f}\n【TP2（2R）】{tp2:.2f}\n【TP3（3R）】{tp3:.2f}\n"
            f"【風險報酬比】{plan.risk_reward:.2f}\n"
            f"【信心分數】{plan.confidence_score}%\n【有效期限】{plan.expires_at}\n"
            f"【取消條件】{plan.cancel_condition}"
        )
    return (
        f"【目前狀態】{direction}計畫{plan.status}\n【取消條件】{plan.cancel_condition}"
    )


def evaluate_entry_engine(
    data: dict,
    previous: EntryPlan | None = None,
    *,
    m5_closed: pd.DataFrame | None = None,
    m15_closed: pd.DataFrame | None = None,
    now: datetime | None = None,
) -> EntryEvaluation:
    now = now or datetime.now(timezone.utc)
    previous = previous or EntryPlan()
    normalized = data.get("normalized_analysis") or {}
    price = float(normalized.get("currentPrice") or 0)
    closed_price = normalized.get("lastClosedCandlePrice")
    tradeable = normalized.get("marketDataStatus") == "GOOD" and normalized.get(
        "consistencyValid", True
    )

    short_ok = normalized.get("supportState") in (
        "confirmed_breakdown",
        "retest_rejected",
    )
    long_ok = (
        normalized.get("supportState") == "failed_breakdown"
        or (
            (normalized.get("tradingDecision") or {}).get("marketAssessment") or {}
        ).get("reversalState")
        == "reversal_confirmed"
    )
    current_direction = "SHORT" if short_ok else "LONG" if long_ok else ""

    if (
        previous.status in ("SETUP_WATCH", "ENTRY_READY")
        and tradeable
        and current_direction
    ):
        from app.config import get_settings

        moved = (
            previous.source_price is not None
            and previous.source_price > 0
            and abs(price - previous.source_price) / previous.source_price
            >= get_settings().setup_stale_deviation_pct
        )
        new_candle = bool(
            normalized.get("lastClosedCandleTimestamp")
            and normalized.get("lastClosedCandleTimestamp")
            != previous.last_closed_candle_time
        )
        direction_changed = current_direction != previous.direction
        if moved or new_candle or direction_changed:
            refreshed = _build_candidate(data, current_direction, now=now)
            if refreshed.status != "NO_SETUP":
                previous = refreshed

    if previous.status in ("SETUP_WATCH", "ENTRY_READY", "ENTRY_TRIGGERED"):
        expired = bool(
            previous.expires_at and now >= datetime.fromisoformat(previous.expires_at)
        )
        invalid = (
            isinstance(closed_price, (int, float))
            and previous.stop_loss is not None
            and (
                (previous.direction == "SHORT" and closed_price > previous.stop_loss)
                or (previous.direction == "LONG" and closed_price < previous.stop_loss)
            )
        )
        if previous.status == "ENTRY_TRIGGERED" and previous.stop_loss is not None:
            exited = (
                previous.direction == "SHORT" and price >= previous.stop_loss
            ) or (previous.direction == "LONG" and price <= previous.stop_loss)
            if exited:
                plan = replace(previous, status="EXITED")
                return EntryEvaluation(
                    plan,
                    "EXITED" not in previous.notified_states,
                    format_entry_message(plan),
                )
        if expired or invalid:
            reason = "計畫已超過有效期限" if expired else previous.cancel_condition
            plan = replace(previous, status="INVALIDATED", missing_condition=reason)
            return EntryEvaluation(
                plan,
                "INVALIDATED" not in previous.notified_states,
                format_entry_message(plan),
            )

        if not tradeable:
            return EntryEvaluation(previous)
        if previous.status == "ENTRY_TRIGGERED":
            return EntryEvaluation(previous)

        timeframe, frame = _closed_frame(m5_closed, m15_closed)
        if frame is None:
            return EntryEvaluation(previous)
        triggered, evidence, trigger_price = reversal_evidence(
            previous.direction, frame, previous.zone_low, previous.zone_high
        )
        bar = frame.iloc[-1]
        touched = (
            float(bar["high"]) >= previous.zone_low
            and float(bar["low"]) <= previous.zone_high
        )
        if triggered:
            risk = (
                previous.stop_loss - trigger_price
                if previous.direction == "SHORT"
                else trigger_price - previous.stop_loss
            )
            reward = (
                trigger_price - previous.take_profit_1
                if previous.direction == "SHORT"
                else previous.take_profit_1 - trigger_price
            )
            rr = round(reward / risk, 2) if risk > 0 else 0
            from app.config import get_settings

            if rr >= max(1.5, float(get_settings().setup_min_rr1)):
                r3 = trigger_price + (
                    1 if previous.direction == "LONG" else -1
                ) * risk * 3
                tp1, tp2, tp3 = ordered_profit_targets(
                    previous.direction,
                    trigger_price,
                    previous.take_profit_1,
                    previous.take_profit_2,
                    r3,
                )
                plan = replace(
                    previous,
                    status="ENTRY_TRIGGERED",
                    trigger_timeframe=timeframe,
                    trigger_condition=evidence,
                    suggested_entry=round(trigger_price, 2),
                    risk_reward=rr,
                    confidence_score=min(100, previous.confidence_score + 15),
                    take_profit_1=tp1,
                    take_profit_2=tp2,
                    take_profit_3=tp3,
                    missing_condition="",
                )
                valid, validation_error = validate_executable_plan(plan)
                if not valid:
                    plan = replace(
                        plan,
                        status="ENTRY_READY",
                        missing_condition=f"一致性檢查未通過：{validation_error}",
                    )
                return EntryEvaluation(
                    plan,
                    plan.status not in previous.notified_states,
                    format_entry_message(plan),
                )
        if touched and previous.status == "SETUP_WATCH":
            plan = replace(
                previous,
                status="ENTRY_READY",
                trigger_timeframe=timeframe,
                missing_condition=evidence,
            )
            return EntryEvaluation(
                plan,
                "ENTRY_READY" not in previous.notified_states,
                format_entry_message(plan),
            )
        return EntryEvaluation(previous)

    direction = current_direction
    if not direction:
        return EntryEvaluation(
            EntryPlan(missing_condition="15M 尚未確認空方結構失守、空方失效或支撐止跌")
        )
    if not tradeable:
        return EntryEvaluation(
            EntryPlan(
                direction=direction,
                missing_condition="行情資料不完整或分析一致性檢查未通過",
            )
        )
    candidate = _build_candidate(data, direction, now=now)
    if candidate.status == "NO_SETUP":
        return EntryEvaluation(candidate)
    return EntryEvaluation(candidate, True, format_entry_message(candidate))
