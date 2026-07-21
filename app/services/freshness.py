"""讀取邊界的時效與一致性防線(BUGFIX R2/R4/R5/R6)。

每次把分析結果交給 UI(REST / WebSocket)前:
1. 重跑一次 Invariant 驗證(R5:防 race / 防舊版程式殘留的矛盾價位)。
2. STALE 判定(R4):現價偏離 entry 超過門檻、或生成超過 N 根 15M K 棒。
3. 快照過期(R6):超過 N 根 15M 無新版本 → 全頁警示 + 決策不得維持可執行狀態。
STALE / INVALID 的 setup 一律剝除價位並降級決策,絕不顯示錯誤或過期價位。
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone

from app.config import get_settings
from app.engines.setup_validator import validate_prices

logger = logging.getLogger(__name__)

BAR_MINUTES = 15  # 當前主週期


def _mid_of(level: dict | None) -> float | None:
    if not level:
        return None
    lo, hi = level.get("price_low"), level.get("price_high")
    if lo is None or hi is None:
        return None
    return (lo + hi) / 2


def _scenario_prices(sc: dict) -> tuple[float | None, float | None, list[float]]:
    rp = sc.get("resolved_prices") or {}
    entry = _mid_of(rp.get(sc.get("entry_zone_id")))
    sl = _mid_of(rp.get(sc.get("stop_loss_id")))
    tps = [m for m in (_mid_of(rp.get(t)) for t in sc.get("target_ids") or [])
           if m is not None]
    return entry, sl, tps


def _strip_prices(sc: dict, status: str, reasons: list[str]) -> None:
    sc["status"] = status
    sc["invalid_reasons"] = reasons
    sc["resolved_prices"] = {}
    sc["entry_zone_id"] = None
    sc["stop_loss_id"] = None
    sc["invalidation_id"] = None
    sc["target_ids"] = []
    sc["risk_reward"] = []


def _downgrade_decision(result: dict, reason: str) -> None:
    d = result.get("decision") or {}
    d["action"] = "WATCH"
    d["confidence_grade"] = "X"
    d["evidence_score"] = 0          # 證據分數不得沿用舊值
    d["reason"] = reason
    result["decision"] = d
    result["decision_downgraded"] = True


def annotate_freshness(result: dict, current_mid: float | None = None,
                       now: datetime | None = None) -> dict:
    """回傳附時效標記(且已剝除失效價位)的結果副本。所有讀取路徑必經。"""
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    out = copy.deepcopy(result)

    # 快照年齡
    age_min: float | None = None
    try:
        ts = datetime.fromisoformat(out.get("timestamp_utc", ""))
        age_min = (now - ts).total_seconds() / 60.0
    except (TypeError, ValueError):
        pass
    snapshot_expired = (age_min is not None
                        and age_min > s.snapshot_expiry_bars * BAR_MINUTES)
    out["freshness"] = {
        "version": out.get("version", 0),
        "age_minutes": round(age_min, 1) if age_min is not None else None,
        "snapshot_expired": snapshot_expired,
        "stale_deviation_pct": s.setup_stale_deviation_pct,
    }

    action = (out.get("decision") or {}).get("action", "")
    dominant_dir = ("LONG" if action in ("PREPARE_LONG", "LONG")
                    else "SHORT" if action in ("PREPARE_SHORT", "SHORT") else None)

    dominant_bad_reason: str | None = None
    for key, direction in (("long_scenario", "LONG"), ("short_scenario", "SHORT")):
        sc = out.get(key)
        if not sc:
            continue
        sc.setdefault("stale", False)
        if sc.get("status") not in ("PREPARE", "TRIGGERED", "WATCH"):
            continue
        entry, sl, tps = _scenario_prices(sc)

        # R5:渲染前再驗一次 Invariant(以顯示中的價位;含 TMGM offset 後數字)
        if entry is not None or sl is not None or tps:
            ref_price = current_mid or entry or 0.0
            reasons = validate_prices(direction, entry=entry, sl=sl, tps=tps,
                                      current_price=ref_price)
            if reasons:
                logger.warning("SETUP_INVALID_AT_RENDER dir=%s reasons=%s setup=%s",
                               direction, reasons, sc)
                _strip_prices(sc, "INVALID", reasons)
                if direction == dominant_dir:
                    dominant_bad_reason = "暫無有效方案:偵測到自相矛盾的價位組合,已攔截,等待重算。"
                continue

        # R4:STALE 判定(僅對可執行中的 setup)
        stale_reasons: list[str] = []
        if sc.get("status") in ("PREPARE", "TRIGGERED"):
            if (entry is not None and current_mid is not None and entry > 0
                    and abs(current_mid - entry) / entry > s.setup_stale_deviation_pct):
                dev = abs(current_mid - entry) / entry
                stale_reasons.append(
                    f"現價已偏離進場價 {dev:.2%}(門檻 {s.setup_stale_deviation_pct:.2%})")
            try:
                created = datetime.fromisoformat(sc.get("created_at", ""))
                sc_age = (now - created).total_seconds() / 60.0
                if sc_age > s.setup_expiry_bars * BAR_MINUTES:
                    stale_reasons.append(
                        f"生成已超過 {s.setup_expiry_bars} 根 15 分K 未觸發")
            except (TypeError, ValueError):
                pass
        if stale_reasons:
            logger.info("SETUP_STALE dir=%s reasons=%s", direction, stale_reasons)
            sc["stale"] = True
            sc["stale_reason"] = ";".join(stale_reasons)
            if direction == dominant_dir:
                dominant_bad_reason = (f"原方案已過時({stale_reasons[0]}),"
                                       f"不再有效,等待下一次重算。")

    if dominant_bad_reason:
        _downgrade_decision(out, dominant_bad_reason)
    if snapshot_expired and action not in ("NO_TRADE", "WATCH"):
        _downgrade_decision(out, f"分析快照已過期({out['freshness']['age_minutes']:.0f} 分鐘未更新),"
                                 "不得依過期內容操作,等待新版本。")
    elif snapshot_expired:
        out["decision_downgraded"] = out.get("decision_downgraded", False)
    return out
