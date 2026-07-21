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


from app.utils.formatting import fmt_price as _fmt  # P3:全站共用 formatter


def validate_prices_detailed(direction: str, *, entry: float | None, sl: float | None,
                             tps: list[float], current_price: float) -> list[dict]:
    """回傳違規清單 [{"severity": "FATAL"|"REJECT", "msg": str}](空 = 通過)。

    嚴重度分級(P1):
    - FATAL:程式錯誤等級 —— 方向次序矛盾、非正數、幻覺價位。到這層代表上游壞了。
    - REJECT:條件不足的正常狀況 —— rr1 低於下限,「沒有優勢就等待」。
    """
    s = get_settings()
    out: list[dict] = []
    up = direction == "LONG"
    present = [("entry", entry), ("sl", sl)] + [(f"tp{i+1}", t) for i, t in enumerate(tps)]

    band = s.setup_price_band_pct
    for name, px in present:
        if px is None:
            continue
        if px <= 0:
            out.append({"severity": "FATAL", "msg": f"{name}={_fmt(px)} 非正數"})
        elif current_price > 0 and abs(px - current_price) / current_price > band:
            out.append({"severity": "FATAL",
                        "msg": f"{name}={_fmt(px)} 落在現價 {_fmt(current_price)} "
                               f"±{band:.0%} 之外(疑似幻覺價位)"})

    fatal_ordering = False
    if entry is not None and sl is not None:
        if up and sl >= entry:
            fatal_ordering = True
            out.append({"severity": "FATAL",
                        "msg": f"多單 SL({_fmt(sl)}) >= Entry({_fmt(entry)}),邏輯不可能成立"})
        if not up and sl <= entry:
            fatal_ordering = True
            out.append({"severity": "FATAL",
                        "msg": f"空單 SL({_fmt(sl)}) <= Entry({_fmt(entry)}),邏輯不可能成立"})
    if entry is not None and tps:
        seq = [entry, *tps]
        ordered = all(a < b for a, b in zip(seq, seq[1:])) if up else \
                  all(a > b for a, b in zip(seq, seq[1:]))
        if not ordered:
            out.append({"severity": "FATAL",
                        "msg": f"目標價次序錯亂:entry={_fmt(entry)}, "
                               f"tps={[_fmt(t) for t in tps]}({direction})"})

    # rr1 下限:FATAL 存在時不得計算/顯示 rr —— 用錯誤 SL 算出的數字沒有意義
    if fatal_ordering:
        out.append({"severity": "FATAL", "msg": "因停損計算錯誤,風報比無法計算"})
    elif entry is not None and sl is not None and tps and abs(entry - sl) > 0:
        rr1 = abs(tps[0] - entry) / abs(entry - sl)
        if rr1 < s.setup_min_rr1:
            out.append({"severity": "REJECT",
                        "msg": f"rr1={rr1:.2f} < 下限 {s.setup_min_rr1}"})
    return out


def validate_prices(direction: str, *, entry: float | None, sl: float | None,
                    tps: list[float], current_price: float) -> list[str]:
    """相容介面:回傳違規訊息清單(空 = 通過)。"""
    return [r["msg"] for r in validate_prices_detailed(
        direction, entry=entry, sl=sl, tps=tps, current_price=current_price)]


def has_fatal(detailed: list[dict]) -> bool:
    return any(r["severity"] == "FATAL" for r in detailed)


def stop_side_ok(direction: str, entry: float, sl: float) -> bool:
    """產生端不變式(P1):BUY 須 SL<Entry;SELL 須 SL>Entry。"""
    return sl < entry if direction == "LONG" else sl > entry


def log_invalid(direction: str, payload: dict, reasons: list[str],
                fatal: bool = False) -> None:
    """INVALID 事件寫 log(含完整 setup 物件與原因,供統計失效頻率)。

    FATAL(攔截器接到方向矛盾等程式錯誤)→ ERROR:代表上游產生端已經出錯。
    """
    if fatal:
        logger.error("SETUP_INVALID_FATAL direction=%s reasons=%s setup=%s",
                     direction, reasons, payload)
    else:
        logger.warning("SETUP_INVALID direction=%s reasons=%s setup=%s",
                       direction, reasons, payload)
