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
from datetime import datetime, timedelta, timezone

from app.config import get_settings

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
    for key in ("decision", "market_decision"):   # 市場層決策同步降級,公開投影才一致
        d = result.get(key)
        if not isinstance(d, dict):
            continue
        d["action"] = "WATCH"
        d["trade_status"] = "BLOCKED_DATA"
        d["can_enter"] = False
        d["blocked_reason"] = reason
        d["reason"] = reason
        result[key] = d
    result["decision_downgraded"] = True


def _expected_closed_15m(now: datetime, delay_seconds: int) -> datetime:
    eligible = now - timedelta(seconds=max(0, delay_seconds))
    boundary = eligible.replace(minute=(eligible.minute // 15) * 15,
                                second=0, microsecond=0)
    return boundary - timedelta(minutes=15)


def _protect_while_candle_refreshes(result: dict, reason: str) -> None:
    _downgrade_decision(result, reason)
    normalized = result.get("normalized_analysis")
    if not isinstance(normalized, dict):
        return
    normalized["entryReadiness"] = "no_trade"
    normalized["entryTiming"] = "wait"
    normalized["longEntryAllowed"] = False
    normalized["shortEntryAllowed"] = False
    normalized["riskOverride"] = "suspend_all_entries"
    normalized["consistencyMessage"] = reason
    trading = normalized.get("tradingDecision")
    if isinstance(trading, dict) and isinstance(trading.get("newEntryDecision"), dict):
        trading["newEntryDecision"].update({
            "readiness": "no_trade", "longAllowed": False, "shortAllowed": False,
            "longReason": reason, "shortReason": reason,
        })


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

    normalized = out.get("normalized_analysis") or {}
    refresh_pending = False
    expected_candle = _expected_closed_15m(now, s.candle_close_refresh_delay_seconds)
    try:
        last_closed = datetime.fromisoformat(normalized.get("lastClosedCandleTimestamp", ""))
        if last_closed.tzinfo is None:
            last_closed = last_closed.replace(tzinfo=timezone.utc)
        from app.services.market_calendar import market_is_open
        refresh_pending = market_is_open(now) and last_closed < expected_candle
    except (TypeError, ValueError):
        refresh_pending = False
    out["freshness"]["candle_refresh_pending"] = refresh_pending
    out["freshness"]["expected_closed_15m"] = expected_candle.isoformat()
    out["freshness"]["last_closed_15m"] = normalized.get("lastClosedCandleTimestamp", "")
    if refresh_pending:
        _protect_while_candle_refreshes(
            out, "新一根 15 分鐘 K 棒已收盤，判斷更新中，暫停進場。")

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
        # 只有 FATAL(方向次序矛盾/幻覺價位)才在渲染層剝價;REJECT(賺賠比不足)
        # 是「沒有優勢就等待」的正常狀況,價位正確,維持顯示、不誤判為自相矛盾。
        if entry is not None or sl is not None or tps:
            from app.engines.setup_validator import has_fatal, validate_prices_detailed
            ref_price = current_mid or entry or 0.0
            detailed = validate_prices_detailed(direction, entry=entry, sl=sl, tps=tps,
                                                current_price=ref_price)
            if has_fatal(detailed):
                reasons = [r["msg"] for r in detailed if r["severity"] == "FATAL"]
                # FATAL 到達渲染層 = 上游已出錯 → ERROR log 附完整 setup(P1)
                logger.error("SETUP_INVALID_AT_RENDER dir=%s reasons=%s setup=%s",
                             direction, reasons, sc)
                _strip_prices(sc, "INVALID", reasons)
                sc["invalid_fatal"] = True
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
    if snapshot_expired and action not in ("NO_TRADE", "WATCH") and not refresh_pending:
        _downgrade_decision(out, f"分析快照已過期({out['freshness']['age_minutes']:.0f} 分鐘未更新),"
                                 "不得依過期內容操作,等待新版本。")
    elif snapshot_expired:
        out["decision_downgraded"] = out.get("decision_downgraded", False)
    return out
