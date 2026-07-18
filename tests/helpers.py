"""測試用合成 K 棒工具。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd


def zigzag_path(segments: list[tuple[int, float]], start: float = 4000.0) -> list[float]:
    """segments: [(bars, step_per_bar), ...] → 收盤價路徑。"""
    prices = [start]
    for bars, step in segments:
        for _ in range(bars):
            prices.append(prices[-1] + step)
    return prices


def make_df(closes: list[float], *, start_time: datetime | None = None,
            minutes: int = 15, wick: float = 0.3) -> pd.DataFrame:
    """由收盤路徑構造 OHLC DataFrame(全部已收線)。"""
    start_time = start_time or datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
    rows = []
    idx = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        rows.append({"open": o, "high": max(o, c) + wick, "low": min(o, c) - wick,
                     "close": c, "volume": 1000.0, "spread": 0.3, "is_closed": True})
        idx.append(start_time + timedelta(minutes=minutes * i))
        prev = c
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="open_time"))
