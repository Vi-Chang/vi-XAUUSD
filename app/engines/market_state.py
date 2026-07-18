"""市場狀態分類(spec 九)— 13 種狀態,禁止模糊的「偏多/偏空」。"""
from __future__ import annotations

import pandas as pd

from app.engines.market_structure import StructureReport

STATES = (
    "STRONG_BULL_TREND", "STRONG_BEAR_TREND", "BULLISH_PULLBACK", "BEARISH_REBOUND",
    "RANGE", "COMPRESSION", "BREAKOUT_PENDING_CONFIRMATION",
    "BREAKDOWN_PENDING_CONFIRMATION", "FAILED_BREAKOUT", "FAILED_BREAKDOWN",
    "STRUCTURE_TRANSITION", "EVENT_DRIVEN_VOLATILITY", "INSUFFICIENT_DATA",
)


def classify(*, structures: dict[str, StructureReport],
             indicators_h1: dict, indicators_m15: dict,
             m15_df: pd.DataFrame, event_volatility: bool = False) -> str:
    """以 1H 結構為當日主方向、4H/1D 為背景,依確定性規則分類。

    優先序:資料不足 → 事件波動 → 假突破/假跌破 → 待確認突破 →
    週期衝突(STRUCTURE_TRANSITION)→ 壓縮 → 趨勢/回檔 → 區間。
    """
    h1 = structures.get("1H")
    m15 = structures.get("15M")
    h4 = structures.get("4H")
    d1 = structures.get("1D")
    if h1 is None or m15 is None or m15_df.empty or len(m15_df) < 50:
        return "INSUFFICIENT_DATA"
    if event_volatility:
        return "EVENT_DRIVEN_VOLATILITY"

    # 最近 15M/1H 假突破事件(最後 5 個事件內)
    recent_events = (m15.events[-5:] if m15 else []) + (h1.events[-3:] if h1 else [])
    for ev in reversed(recent_events):
        if ev.event_type == "FAILED_BREAKOUT":
            return "FAILED_BREAKOUT"
        if ev.event_type == "FAILED_BREAKDOWN":
            return "FAILED_BREAKDOWN"

    # 未收線 K 棒正在突破邊界 → 待確認(顯示為 PROVISIONAL,不得當正式確認)
    last = m15_df.iloc[-1]
    if not bool(last.get("is_closed", True)):
        if m15.range_high and float(last["close"]) > m15.range_high:
            return "BREAKOUT_PENDING_CONFIRMATION"
        if m15.range_low and float(last["close"]) < m15.range_low:
            return "BREAKDOWN_PENDING_CONFIRMATION"

    # 觀察期未滿的 provisional 突破事件同樣視為待確認
    for ev in reversed(recent_events):
        if ev.provisional and ev.still_valid:
            return ("BREAKOUT_PENDING_CONFIRMATION" if ev.event_type.endswith("_UP")
                    else "BREAKDOWN_PENDING_CONFIRMATION")

    adx = indicators_h1.get("adx") or 0
    bb_width = indicators_m15.get("bb_width")
    # 壓縮:15M BB 寬 < 近期 20% 分位(以 0.004 為近似門檻,可於回測調整)
    if bb_width is not None and bb_width < 0.004 and adx < 20:
        return "COMPRESSION"

    higher_trend = h4.trend if h4 else (d1.trend if d1 else "UNKNOWN")
    if h1.trend == "UP":
        if higher_trend == "DOWN":
            return "STRUCTURE_TRANSITION"   # 日線空頭背景中的短線多頭結構等衝突
        if indicators_h1.get("macd_hist", 0) is not None and adx >= 20:
            ema20 = indicators_m15.get("ema20")
            close = indicators_m15.get("bb_mid")  # 近似:以 bb_mid(=SMA20)判回檔
            if ema20 and close and m15.trend == "DOWN":
                return "BULLISH_PULLBACK"
            return "STRONG_BULL_TREND"
        return "BULLISH_PULLBACK" if m15.trend == "DOWN" else "STRONG_BULL_TREND"
    if h1.trend == "DOWN":
        if higher_trend == "UP":
            return "STRUCTURE_TRANSITION"
        if m15.trend == "UP":
            return "BEARISH_REBOUND"
        return "STRONG_BEAR_TREND"
    return "RANGE"
