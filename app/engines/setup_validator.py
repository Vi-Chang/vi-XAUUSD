"""Setup Invariant 驗證層(BUGFIX spec R2)。

在 setup 進入 UI 與決策評分之前強制驗證:
- LONG:sl < entry < tp1 < tp2 < tp3;SHORT 相反。
- rr1 >= setup_min_rr1(預設 1.5)。
- 所有價位為正且落在現價 ±setup_price_band_pct(預設 5%)內(防幻覺價位)。
任一違反 → INVALID:絕不顯示錯誤價位,決策降級「暫無有效方案」。
"""
from __future__ import annotations

import logging

from app.config import get_settings

logger = logging.getLogger(__name__)


def validate_prices(direction: str, *, entry: float | None, sl: float | None,
                    tps: list[float], current_price: float) -> list[str]:
    """回傳違規清單(空 = 通過)。只驗證「已存在」的欄位組合:
    entry+sl 存在即驗方向次序;tps 存在即驗排序;完整組合才驗 rr1。
    """
    s = get_settings()
    reasons: list[str] = []
    up = direction == "LONG"
    present = [("entry", entry), ("sl", sl)] + [(f"tp{i+1}", t) for i, t in enumerate(tps)]

    # 價位為正 + 現價 ±band 內
    band = s.setup_price_band_pct
    for name, px in present:
        if px is None:
            continue
        if px <= 0:
            reasons.append(f"{name}={px} 非正數")
        elif current_price > 0 and abs(px - current_price) / current_price > band:
            reasons.append(f"{name}={px} 落在現價 {current_price} ±{band:.0%} 之外(疑似幻覺價位)")

    # 方向次序
    if entry is not None and sl is not None:
        if up and sl >= entry:
            reasons.append(f"多單 SL({sl}) >= Entry({entry}),邏輯不可能成立")
        if not up and sl <= entry:
            reasons.append(f"空單 SL({sl}) <= Entry({entry}),邏輯不可能成立")
    if entry is not None and tps:
        seq = [entry, *tps]
        ordered = all(a < b for a, b in zip(seq, seq[1:])) if up else \
                  all(a > b for a, b in zip(seq, seq[1:]))
        if not ordered:
            reasons.append(f"目標價次序錯亂:entry={entry}, tps={tps}({direction})")

    # rr1 下限(完整組合才可算)
    if entry is not None and sl is not None and tps and abs(entry - sl) > 0:
        rr1 = abs(tps[0] - entry) / abs(entry - sl)
        if rr1 < s.setup_min_rr1:
            reasons.append(f"rr1={rr1:.2f} < 下限 {s.setup_min_rr1}")
    return reasons


def log_invalid(direction: str, payload: dict, reasons: list[str]) -> None:
    """INVALID 事件寫 log(含完整 setup 物件與原因,供統計失效頻率)。"""
    logger.warning("SETUP_INVALID direction=%s reasons=%s setup=%s",
                   direction, reasons, payload)
