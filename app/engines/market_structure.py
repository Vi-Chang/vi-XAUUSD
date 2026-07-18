"""市場結構引擎(spec 六、七)— 確定性程式邏輯,只使用已收線 K 棒。

輸出的每個結構事件都記錄:發生時間、週期、價格、確認用的已收線 K 棒、
是否仍有效、結構失效價位。參數全部可調(config)並受 Golden Dataset 驗收。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from app.engines.indicators import atr as atr_fn


@dataclass
class SwingPoint:
    kind: str                  # SWING_HIGH / SWING_LOW
    index: int
    time: datetime
    price: float
    label: str | None = None   # HH / HL / LH / LL
    confirming_candles: list[datetime] = field(default_factory=list)


@dataclass
class StructureEvent:
    event_type: str            # BOS_UP/BOS_DOWN/CHOCH_UP/CHOCH_DOWN/FAILED_BREAKOUT/FAILED_BREAKDOWN
    time: datetime
    price: float               # 被突破/跌破的結構價位
    timeframe: str
    confirming_candles: list[datetime]
    invalidation_price: float | None
    still_valid: bool = True
    provisional: bool = False  # 事件太新、假突破觀察期未滿


@dataclass
class StructureReport:
    timeframe: str
    swings: list[SwingPoint]
    events: list[StructureEvent]
    trend: str                 # UP / DOWN / RANGE / UNKNOWN
    last_swing_high: float | None
    last_swing_low: float | None
    range_high: float | None
    range_low: float | None


def detect_swings(df: pd.DataFrame, *, left: int = 2, right: int = 2,
                  min_atr_mult: float = 0.5, min_move_pct: float = 0.0008,
                  atr_series: pd.Series | None = None) -> list[SwingPoint]:
    """偵測 Swing High/Low(spec 六:不得只用單根最高最低)。

    條件:
    - 左側 `left` 根、右側 `right` 根皆不高於(低於)樞紐 K 棒 → 候選。
    - 與前一個相反方向 swing 的距離 >= max(min_atr_mult × ATR, min_move_pct × price),
      否則視為雜訊剔除。
    - 序列強制 High/Low 交替;連續同向取更極端者。
    """
    if len(df) < left + right + 1:
        return []
    atr_s = atr_series if atr_series is not None else atr_fn(df)
    highs, lows, times = df["high"].values, df["low"].values, list(df.index)
    n = len(df)

    raw: list[SwingPoint] = []
    for i in range(left, n - right):
        win_h = highs[i - left: i + right + 1]
        win_l = lows[i - left: i + right + 1]
        confirm = [times[j] for j in range(i + 1, i + right + 1)]
        # 窗內最大/最小;等值(Equal Highs/Lows)取第一次觸及的 K 棒
        if highs[i] == win_h.max() and not (highs[i - left:i] == highs[i]).any():
            raw.append(SwingPoint("SWING_HIGH", i, times[i], float(highs[i]),
                                  confirming_candles=confirm))
        if lows[i] == win_l.min() and not (lows[i - left:i] == lows[i]).any():
            raw.append(SwingPoint("SWING_LOW", i, times[i], float(lows[i]),
                                  confirming_candles=confirm))
    raw.sort(key=lambda s: s.index)

    # 交替 + 最小距離過濾
    out: list[SwingPoint] = []
    for sp in raw:
        if not out:
            out.append(sp)
            continue
        prev = out[-1]
        if prev.kind == sp.kind:
            better = (sp.price > prev.price) if sp.kind == "SWING_HIGH" else (sp.price < prev.price)
            if better:
                out[-1] = sp
            continue
        min_dist = max(min_atr_mult * float(atr_s.iloc[sp.index] or 0),
                       min_move_pct * sp.price)
        if abs(sp.price - prev.price) < min_dist:
            continue
        out.append(sp)

    # 標記 HH/HL/LH/LL
    last_high: float | None = None
    last_low: float | None = None
    for sp in out:
        if sp.kind == "SWING_HIGH":
            if last_high is not None:
                sp.label = "HH" if sp.price > last_high else "LH"
            last_high = sp.price
        else:
            if last_low is not None:
                sp.label = "HL" if sp.price > last_low else "LL"
            last_low = sp.price
    return out


def _trend_from_labels(swings: list[SwingPoint]) -> str:
    labels = [s.label for s in swings if s.label][-4:]
    if not labels:
        return "UNKNOWN"
    ups = sum(1 for x in labels if x in ("HH", "HL"))
    downs = sum(1 for x in labels if x in ("LH", "LL"))
    if ups >= 3 and downs <= 1:
        return "UP"
    if downs >= 3 and ups <= 1:
        return "DOWN"
    return "RANGE"


def detect_events(df: pd.DataFrame, swings: list[SwingPoint], timeframe: str, *,
                  fail_confirm_bars: int = 3, min_break_atr_mult: float = 0.1,
                  atr_series: pd.Series | None = None) -> list[StructureEvent]:
    """以收盤價掃描 BOS / CHoCH / 假突破(spec 六、七)。

    - BOS:順勢收盤突破最近確認 swing;CHoCH:逆勢收盤突破。
    - 突破幅度須 >= min_break_atr_mult × ATR(排除貼線雜訊)。
    - 假突破:突破後 1..fail_confirm_bars 根已收線內收盤收回 → FAILED_BREAKOUT/BREAKDOWN,
      原 BOS/CHoCH 標記失效。觀察期未滿(資料末端)標 provisional。
    """
    events: list[StructureEvent] = []
    if not swings:
        return events
    atr_s = atr_series if atr_series is not None else atr_fn(df)
    closes, times = df["close"].values, list(df.index)
    n = len(df)

    swing_iter = iter(swings)
    next_swing = next(swing_iter, None)
    active_high: SwingPoint | None = None
    active_low: SwingPoint | None = None
    trend = "UNKNOWN"
    labels_seen: list[SwingPoint] = []

    for i in range(n):
        # 引入「已確認」的 swing(右側確認棒都已收線:i 越過 index+right 即確認)
        while next_swing is not None and next_swing.confirming_candles and \
                times[i] >= next_swing.confirming_candles[-1]:
            if next_swing.kind == "SWING_HIGH":
                active_high = next_swing
            else:
                active_low = next_swing
            labels_seen.append(next_swing)
            trend = _trend_from_labels(labels_seen)
            next_swing = next(swing_iter, None)

        min_break = min_break_atr_mult * float(atr_s.iloc[i] or 0)
        c = float(closes[i])

        if active_high is not None and c > active_high.price + min_break:
            etype = "BOS_UP" if trend in ("UP", "RANGE", "UNKNOWN") else "CHOCH_UP"
            events.append(StructureEvent(
                etype, times[i], active_high.price, timeframe,
                confirming_candles=[times[i]],
                invalidation_price=active_low.price if active_low else None))
            trend = "UP"
            labels_seen = labels_seen[-2:]
            active_high = None
        if active_low is not None and c < active_low.price - min_break:
            etype = "BOS_DOWN" if trend in ("DOWN", "RANGE", "UNKNOWN") else "CHOCH_DOWN"
            events.append(StructureEvent(
                etype, times[i], active_low.price, timeframe,
                confirming_candles=[times[i]],
                invalidation_price=active_high.price if active_high else None))
            trend = "DOWN"
            labels_seen = labels_seen[-2:]
            active_low = None

    # 假突破後處理:突破事件後 fail_confirm_bars 根內收回
    time_to_idx = {t: i for i, t in enumerate(times)}
    for ev in list(events):
        idx = time_to_idx[ev.time]
        look_end = idx + fail_confirm_bars
        if ev.event_type.endswith("_UP"):
            reverted = [j for j in range(idx + 1, min(look_end, n - 1) + 1)
                        if closes[j] < ev.price]
            if reverted:
                j = reverted[0]
                ev.still_valid = False
                events.append(StructureEvent(
                    "FAILED_BREAKOUT", times[j], ev.price, timeframe,
                    confirming_candles=[times[k] for k in range(idx, j + 1)],
                    invalidation_price=None))
            elif look_end > n - 1:
                ev.provisional = True
        elif ev.event_type.endswith("_DOWN"):
            reverted = [j for j in range(idx + 1, min(look_end, n - 1) + 1)
                        if closes[j] > ev.price]
            if reverted:
                j = reverted[0]
                ev.still_valid = False
                events.append(StructureEvent(
                    "FAILED_BREAKDOWN", times[j], ev.price, timeframe,
                    confirming_candles=[times[k] for k in range(idx, j + 1)],
                    invalidation_price=None))
            elif look_end > n - 1:
                ev.provisional = True
    events.sort(key=lambda e: e.time)
    return events


def analyze_structure(df: pd.DataFrame, timeframe: str, *, left: int = 2, right: int = 2,
                      min_atr_mult: float = 0.5, min_move_pct: float = 0.0008,
                      fail_confirm_bars: int = 3,
                      min_break_atr_mult: float = 0.1) -> StructureReport:
    """完整結構分析。輸入 df 必須「只含已收線 K 棒」(呼叫端負責過濾)。"""
    atr_s = atr_fn(df) if len(df) > 1 else pd.Series(dtype=float)
    swings = detect_swings(df, left=left, right=right, min_atr_mult=min_atr_mult,
                           min_move_pct=min_move_pct, atr_series=atr_s)
    events = detect_events(df, swings, timeframe, fail_confirm_bars=fail_confirm_bars,
                           min_break_atr_mult=min_break_atr_mult, atr_series=atr_s)
    trend = _trend_from_labels(swings)
    # 有效 BOS/CHoCH 覆蓋 label 趨勢(結構突破優先)
    valid_breaks = [e for e in events if e.still_valid and not e.provisional
                    and e.event_type.startswith(("BOS", "CHOCH"))]
    if valid_breaks:
        trend = "UP" if valid_breaks[-1].event_type.endswith("_UP") else "DOWN"

    sw_highs = [s for s in swings if s.kind == "SWING_HIGH"]
    sw_lows = [s for s in swings if s.kind == "SWING_LOW"]
    recent_h = [s.price for s in sw_highs[-3:]]
    recent_l = [s.price for s in sw_lows[-3:]]
    return StructureReport(
        timeframe=timeframe, swings=swings, events=events, trend=trend,
        last_swing_high=sw_highs[-1].price if sw_highs else None,
        last_swing_low=sw_lows[-1].price if sw_lows else None,
        range_high=max(recent_h) if recent_h else None,
        range_low=min(recent_l) if recent_l else None,
    )
