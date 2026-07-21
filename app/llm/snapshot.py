"""V2 緊湊 JSON 快照:AI 的唯一輸入(程式算好所有數字,AI 禁止再計算)。

Token 節約:
- 鍵名縮短、數字四捨五入、清單截斷(候選價位取最近 + STRONG,證據各取 3 條)。
- fingerprint:去除易變欄位(精確價格→ATR 分桶、倒數分鐘→小時桶)後之 SHA-256,
  相同指紋在 llm_cache_minutes 內直接重用舊結果,不再花 token。
"""
from __future__ import annotations

import hashlib
import json


def _ema_stack(ind: dict) -> str:
    e20, e50, e200 = ind.get("ema20"), ind.get("ema50"), ind.get("ema200")
    if None in (e20, e50, e200):
        return "NA"
    if e20 > e50 > e200:
        return "20>50>200"
    if e20 < e50 < e200:
        return "20<50<200"
    return "MIXED"


def _tf_block(structures: dict, ind: dict, tf: str) -> dict:
    rep = structures.get(tf)
    i = ind.get(tf, {})
    return {
        "trend": rep.trend if rep else "NA",
        "ema": _ema_stack(i),
        "macd_h": round(i["macd_hist"], 2) if i.get("macd_hist") is not None else None,
        "rsi": round(i["rsi14"], 1) if i.get("rsi14") is not None else None,
        "adx": round(i["adx"], 1) if i.get("adx") is not None else None,
        "hi": rep.last_swing_high if rep else None,
        "lo": rep.last_swing_low if rep else None,
    }


def build_snapshot(*, price: float, atr15: float, state: str, quality_status: str,
                   ev, ind: dict, structures: dict, levels: list, fvgs: list,
                   bias, position: dict | None, cross: dict,
                   no_signal: bool, event_lockout: bool) -> dict:
    """組緊湊快照。levels=CandidateLevel list、fvgs=FvgZone list、bias=規則引擎輸出。"""
    # 候選價位:全部 STRONG + 距現價最近的 WEAK,合計上限 16
    strong = [lv for lv in levels if lv.strength == "STRONG"]
    weak = sorted((lv for lv in levels if lv.strength != "STRONG"),
                  key=lambda lv: abs(lv.mid - price))
    picked = (strong + weak)[:16]
    lv_rows = [{"id": lv.level_id, "k": lv.kind, "lo": round(lv.price_low, 2),
                "hi": round(lv.price_high, 2), "s": lv.strength} for lv in picked]
    fvg_rows = [{"id": z.fvg_id, "tf": z.timeframe, "d": z.direction,
                 "lo": round(z.price_low, 2), "hi": round(z.price_high, 2)} for z in fvgs]

    return {
        "sym": "XAUUSD",
        "price": round(price, 2),
        "atr15": round(atr15, 2),
        "state": state,
        "quality": quality_status,
        "event": {"impact": ev.event_impact, "time_risk": ev.time_risk,
                  "lockout": bool(event_lockout), "next": ev.next_event,
                  "mins": ev.minutes_remaining},
        "tf": {tf: _tf_block(structures, ind, tf) for tf in ("1D", "4H", "1H", "15M")},
        "levels": lv_rows,
        "fvg": fvg_rows,
        "bias": {"bull": bias.bull_pct, "bear": bias.bear_pct,
                 "bull_ev": bias.bull_evidence[:3], "bear_ev": bias.bear_evidence[:3],
                 "chase": bias.chase_flags},
        "cross": cross,
        "position": position or {"has": False},
        "gates": {"no_signal": bool(no_signal), "event_lockout": bool(event_lockout),
                  "quality": quality_status},
    }


def fingerprint_of(snapshot: dict) -> str:
    """輸入指紋:精確價→ATR 半桶、事件倒數→小時桶、跨市場粗化。"""
    atr = snapshot.get("atr15") or 1.0
    bucket = max(atr / 2, 0.5)
    ev = snapshot.get("event", {})
    cross = snapshot.get("cross", {}) or {}
    reduced = {
        "state": snapshot.get("state"),
        "quality": snapshot.get("quality"),
        "price_b": round((snapshot.get("price") or 0) / bucket),
        "tf": snapshot.get("tf"),
        "levels": [(r["id"], r["s"]) for r in snapshot.get("levels", [])],
        "fvg": [r["id"] for r in snapshot.get("fvg", [])],
        "bias": (snapshot.get("bias", {}).get("bull"), snapshot.get("bias", {}).get("bear")),
        "event": (ev.get("impact"), ev.get("time_risk"), ev.get("lockout"),
                  (ev.get("mins") // 60) if isinstance(ev.get("mins"), int) else None),
        "cross": {k: (round(v) if isinstance(v, (int, float)) else v)
                  for k, v in cross.items()},
        "pos": (snapshot.get("position", {}).get("has"),
                snapshot.get("position", {}).get("side")),
        "gates": snapshot.get("gates"),
    }
    blob = json.dumps(reduced, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
