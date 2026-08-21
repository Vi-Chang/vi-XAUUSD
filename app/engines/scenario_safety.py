"""Single source of truth for scenario ordering, lifecycle and cost-aware R/R."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

Direction = Literal["LONG", "SHORT"]


def stable_setup_id(*, symbol: str, direction: Direction, timeframe: str,
                    trigger_level: str, breakout_at: str) -> str:
    seed = f"{symbol}|{direction}|{timeframe}|{trigger_level}|{breakout_at}"
    return f"{symbol[:3]}-{direction[0]}-{hashlib.sha256(seed.encode()).hexdigest()[:12]}"


@dataclass(frozen=True)
class PriceZone:
    low: float
    high: float

    @property
    def mid(self) -> float:
        return round((self.low + self.high) / 2, 2)


@dataclass(frozen=True)
class StructuralStop:
    """A real confirmed swing selected by timeframe priority."""
    level: Any
    timeframe: Literal["15M", "1H", "4H"]
    source_kind: str

    @property
    def mid(self) -> float:
        return self.level.mid

    @property
    def level_id(self) -> str:
        return self.level.level_id


def conservative_entry(direction: Direction, zone: PriceZone) -> float:
    return zone.high if direction == "LONG" else zone.low


def select_structural_stop(direction: Direction, *, entry: PriceZone,
                           levels: list[Any]) -> StructuralStop | None:
    boundary = entry.low if direction == "LONG" else entry.high
    side = "LOW" if direction == "LONG" else "HIGH"
    for timeframe in ("15M", "1H", "4H"):
        kind = f"SWING_{side}_{timeframe}"
        candidates = [
            level for level in levels
            if level.kind == kind
            and ((direction == "LONG" and level.mid < entry.low)
                 or (direction == "SHORT" and level.mid > entry.high))
        ]
        if candidates:
            level = min(candidates, key=lambda item: abs(item.mid - boundary))
            return StructuralStop(level=level, timeframe=timeframe, source_kind=kind)
    return None


def validate_price_structure(
    direction: Direction, *, entry: PriceZone, planned_entry: float,
    stop_loss: float, targets: list[PriceZone], tick_size: float = 0.01,
) -> list[str]:
    eps = max(tick_size, 0.0) / 2
    if not targets:
        return ["缺少目標區，價格結構無效"]
    if planned_entry < entry.low - eps or planned_entry > entry.high + eps:
        return ["賺賠比計算基準價不在進場區內"]
    if direction == "LONG":
        if stop_loss >= entry.low - eps:
            return ["停損落在進場區內或其上方，價格結構無效"]
        prior = entry.high
        for index, target in enumerate(targets, 1):
            if target.low <= prior + eps or target.high < target.low:
                return [f"目標{index}與前一區間重疊或順序錯誤"]
            prior = target.high
    else:
        if stop_loss <= entry.high + eps:
            return ["停損落在進場區內或其下方，價格結構無效"]
        prior = entry.low
        for index, target in enumerate(targets, 1):
            if target.high >= prior - eps or target.high < target.low:
                return [f"目標{index}與前一區間重疊或順序錯誤"]
            prior = target.low
    return []


def calculate_risk_reward(
    direction: Direction, *, evaluation_entry_price: float, stop_loss: float,
    target_price: float, spread: float = 0.0, slippage: float = 0.0,
    fees: float = 0.0,
) -> dict:
    """Return traceable cost-adjusted reward/risk; all costs are price units."""
    half_spread = max(spread, 0.0) / 2
    friction = half_spread + max(slippage, 0.0) + max(fees, 0.0)
    if direction == "LONG":
        effective_entry = evaluation_entry_price + friction
        effective_stop = stop_loss - friction
        effective_target = target_price - friction
        risk = effective_entry - effective_stop
        reward = effective_target - effective_entry
    else:
        effective_entry = evaluation_entry_price - friction
        effective_stop = stop_loss + friction
        effective_target = target_price + friction
        risk = effective_stop - effective_entry
        reward = effective_entry - effective_target
    if risk <= 0 or reward <= 0:
        return {"available": False, "ratio": None,
                "reason": "有效風險或有效獲利距離不是正值"}
    return {
        "available": True, "ratio": round(reward / risk, 2), "reason": "",
        "evaluationEntryPrice": round(evaluation_entry_price, 2),
        "effectiveEntryPrice": round(effective_entry, 2),
        "stopLoss": round(stop_loss, 2), "effectiveStopLoss": round(effective_stop, 2),
        "targetPrice": round(target_price, 2), "effectiveTargetPrice": round(effective_target, 2),
        "spread": round(max(spread, 0.0), 3), "slippage": round(max(slippage, 0.0), 3),
        "fees": round(max(fees, 0.0), 3), "riskDistance": round(risk, 3),
        "rewardDistance": round(reward, 3),
    }


def lifecycle_status(
    direction: Direction, *, current_price: float, entry: PriceZone,
    first_target: PriceZone, structure_valid: bool, confirmations_passed: bool,
) -> str:
    if not structure_valid:
        return "INVALID"
    if direction == "LONG":
        if current_price >= first_target.low:
            return "EXPIRED"
        if current_price > entry.high:
            return "CONFIRMED_WAIT_RETEST" if confirmations_passed else "WAITING_FOR_CONFIRMATION"
        if current_price < entry.low:
            return "WAITING_FOR_ENTRY"
    else:
        if current_price <= first_target.high:
            return "EXPIRED"
        if current_price < entry.low:
            return "CONFIRMED_WAIT_RETEST" if confirmations_passed else "WAITING_FOR_CONFIRMATION"
        if current_price > entry.high:
            return "WAITING_FOR_ENTRY"
    return "READY" if confirmations_passed else "BREAKOUT_PENDING"
