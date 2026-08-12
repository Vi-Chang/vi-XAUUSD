"""Deterministic entry timing gates for prepared trade scenarios."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.engines.key_levels import CandidateLevel, nearest_zone


@dataclass(frozen=True)
class EntryGate:
    blocked: bool = False
    triggered: bool = False
    reason: str = ""


def evaluate_entry_gate(direction: str, *, price: float, atr15: float,
                        levels: list[CandidateLevel], entry_zone_id: str | None,
                        previous_action: str | None, m15_df: pd.DataFrame | None,
                        opposing_zone_atr_mult: float,
                        breakout_buffer_atr_mult: float) -> EntryGate:
    """Enforce an opposing-zone hard gate and closed-candle retest trigger."""
    up = direction == "LONG"
    atr = max(float(atr15), 1e-9)
    opposing = nearest_zone(levels, price, "RES_ZONE" if up else "SUP_ZONE", "STRONG")
    if opposing is not None:
        edge = opposing.price_low if up else opposing.price_high
        distance = edge - price if up else price - edge
        close = (float(m15_df.iloc[-1]["close"])
                 if m15_df is not None and not m15_df.empty else None)
        breakout_ok = close is not None and (
            close > opposing.price_high + breakout_buffer_atr_mult * atr if up
            else close < opposing.price_low - breakout_buffer_atr_mult * atr)
        if 0 <= distance < opposing_zone_atr_mult * atr and not breakout_ok:
            return EntryGate(blocked=True,
                             reason=f"前方強{'阻力' if up else '支撐'}過近且尚未收盤有效突破，禁止進場。")

    if previous_action != f"PREPARE_{direction}" or not entry_zone_id:
        return EntryGate(reason="需先完成同方向 PREPARE，下一根收盤確認後才可觸發。")
    entry = next((lv for lv in levels if lv.level_id == entry_zone_id), None)
    if entry is None or m15_df is None or m15_df.empty:
        return EntryGate(reason="缺少已收盤 15M K 或進場區域，維持準備狀態。")

    bar = m15_df.iloc[-1]
    open_, high, low, close = (float(bar[k]) for k in ("open", "high", "low", "close"))
    touched = low <= entry.price_high if up else high >= entry.price_low
    held = (close > entry.price_high and close > open_) if up else (
        close < entry.price_low and close < open_)
    if touched and held:
        return EntryGate(triggered=True, reason="15M 已收盤回踩確認，進場條件成立。")
    return EntryGate(reason="等待 15M 回踩進場區並以同方向實體 K 收盤確認。")
