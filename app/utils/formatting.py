"""輸出層價格格式(P3)— 全站唯一 formatter,禁止各處自行處理。

原則:內部計算保持全精度;只在「對外輸出」(API 回應、UI、通知、log 訊息)
套用 fmt_price。黃金報價固定 2 位小數。
"""
from __future__ import annotations

PRICE_DECIMALS = 2


def fmt_price(x: float | None) -> float | None:
    """對外輸出的價位一律經此格式化(None 安全)。"""
    if x is None:
        return None
    return round(float(x), PRICE_DECIMALS)
