"""FVG(Fair Value Gap)偵測 — V2 AI 分析層的程式端指標之一。

規則(經典三根 K 棒定義,只用已收線資料):
- 看多 FVG:第 i 根的 low > 第 i-2 根的 high → 缺口區 [high[i-2], low[i]]。
- 看空 FVG:第 i 根的 high < 第 i-2 根的 low → 缺口區 [high[i], low[i-2]]。
- 缺口大小須 ≥ min_atr_mult × ATR,過小的雜訊缺口忽略。
- 回補判定:之後任一根 K 棒穿越整個缺口(多方缺口被跌破下緣/空方缺口被漲破上緣)
  → filled,不再列入候選。

AI 禁止自行計算 FVG;只能引用本模組給出的 ID(FVG_BULL_15M_01 等)。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class FvgZone:
    fvg_id: str
    direction: str          # BULL / BEAR
    timeframe: str
    price_low: float
    price_high: float
    created_time: str       # 第三根(形成根)開盤時間 ISO

    @property
    def mid(self) -> float:
        return round((self.price_low + self.price_high) / 2, 2)

    def to_dict(self) -> dict:
        from app.utils.formatting import fmt_price
        return {"id": self.fvg_id, "kind": f"FVG_{self.direction}",
                "timeframe": self.timeframe,
                "price_low": fmt_price(self.price_low),
                "price_high": fmt_price(self.price_high),
                "created_time": self.created_time}


def detect_fvg(df: pd.DataFrame, timeframe: str, *, atr: float | None = None,
               min_atr_mult: float = 0.15, lookback: int = 120,
               max_count: int = 3) -> list[FvgZone]:
    """回傳最近 max_count 個「尚未回補」的 FVG(新→舊)。df 需為已收線資料。"""
    if df is None or len(df) < 3:
        return []
    df = df.tail(lookback)
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    times = df.index
    min_gap = (atr or 0.0) * min_atr_mult

    zones: list[FvgZone] = []
    counters = {"BULL": 0, "BEAR": 0}
    for i in range(2, len(df)):
        lo = hi = None
        direction = ""
        if lows[i] > highs[i - 2] and (lows[i] - highs[i - 2]) >= min_gap:
            direction, lo, hi = "BULL", float(highs[i - 2]), float(lows[i])
        elif highs[i] < lows[i - 2] and (lows[i - 2] - highs[i]) >= min_gap:
            direction, lo, hi = "BEAR", float(highs[i]), float(lows[i - 2])
        if not direction:
            continue
        # 回補判定:形成後任一根穿越整個缺口
        later_lows, later_highs = lows[i + 1:], highs[i + 1:]
        if direction == "BULL":
            filled = bool((later_lows <= lo).any()) if len(later_lows) else False
        else:
            filled = bool((later_highs >= hi).any()) if len(later_highs) else False
        if filled:
            continue
        zones.append(FvgZone("", direction, timeframe, round(lo, 4), round(hi, 4),
                             times[i].isoformat()))

    zones = zones[-max_count * 2:][::-1][:max_count * 2]  # 新在前
    out: list[FvgZone] = []
    for z in zones:
        counters[z.direction] += 1
        if counters[z.direction] > max_count:
            continue
        z.fvg_id = f"FVG_{z.direction}_{timeframe}_{counters[z.direction]:02d}"
        out.append(z)
    return out


def detect_fvg_multi(dfs_closed: dict[str, pd.DataFrame], *,
                     atr_by_tf: dict[str, float | None] | None = None,
                     timeframes: tuple[str, ...] = ("15M", "1H", "4H")) -> list[FvgZone]:
    """多週期 FVG 彙整(V2 快照輸入用)。"""
    atr_by_tf = atr_by_tf or {}
    out: list[FvgZone] = []
    for tf in timeframes:
        df = dfs_closed.get(tf)
        if df is not None and len(df) >= 3:
            out.extend(detect_fvg(df, tf, atr=atr_by_tf.get(tf)))
    return out
