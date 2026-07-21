"""系統監控與死亡偵測(spec 二十三,強制)— 靜默 heartbeat。

設計(第二層:靜默 heartbeat + 第三層分級):
- 監控排程照跑(每 HEARTBEAT_MINUTES,維持高頻才能及時抓資料斷線),
  但「一切正常只寫 log、不推播」,手機只在真的需要時響:
    · 最新 15M K 棒落後現在 > DATA_LAG_WARN_MINUTES → WARN 推播
    · 關鍵 job 停擺 / provider 掛掉 → ERROR 推播(標記 @you)
    · 每天固定一則 [DAILY] 摘要(昨日是否正常 + LLM 成本)
理由:沒有心跳,系統掛掉時你會以為「今天只是沒訊號」;但正常時的 OK 訊息只是噪音。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.services.market_calendar import market_is_open

logger = logging.getLogger(__name__)

# job → 容忍秒數(須大於該 job 的執行間隔,避免時序抖動誤報)。
# 三層架構:quote_l1 依實際輪詢間隔動態放寬;structure_l2 每 300s;
# full_analysis 最遲每 tier3_max_age 分鐘由保底觸發。


def _critical_jobs() -> dict[str, int]:
    from app.config import get_settings
    s = get_settings()
    try:
        from app.services.scheduler import l1_interval_seconds
        l1 = l1_interval_seconds()
    except Exception:  # noqa: BLE001
        l1 = s.tier1_quote_seconds
    return {
        "quote_l1": max(l1 * 3, 300),
        "structure_l2": s.tier2_check_seconds * 2 + 100,
        "full_analysis": s.tier3_max_age_minutes * 60 + s.tier2_check_seconds * 2,
    }




def check_liveness(last_job_run: dict[str, datetime],
                   started_at: datetime | None = None) -> list[str]:
    """回傳停止運作的元件清單。

    開機寬限期:started_at 提供時,尚未執行過的 job 在「開機後未滿容忍時間」內
    不算死亡(剛重啟時各層還沒輪到第一次,不應誤報 degraded/ERROR)。
    """
    now = datetime.now(timezone.utc)
    dead = []
    for job, tolerance in _critical_jobs().items():
        last = last_job_run.get(job)
        if last is not None:
            if (now - last).total_seconds() > tolerance:
                dead.append(f"{job} (last={last.isoformat()})")
        else:
            in_grace = started_at is not None and (now - started_at).total_seconds() <= tolerance
            if not in_grace:
                dead.append(f"{job} (last=never)")
    return dead


def _last_15m_candle():
    """回傳 (最新 15M K 棒 open_time, 落後分鐘數);無資料回傳 (None, None)。"""
    try:
        from app.db.models import Candle
        from app.db.session import db_session
        with db_session() as db:
            row = db.execute(select(Candle).where(Candle.timeframe == "15M")
                             .order_by(Candle.open_time.desc()).limit(1)).scalar_one_or_none()
        if row is None:
            return None, None
        from app.utils.timeutils import ensure_utc
        t = ensure_utc(row.open_time)
        age = (datetime.now(timezone.utc) - t).total_seconds() / 60.0
        return t, age
    except Exception as exc:  # noqa: BLE001
        logger.warning("read last candle failed: %s", exc)
        return None, None


async def _maybe_daily_summary(state) -> None:
    """每天固定一則 [DAILY] 摘要(首次跨入新 UTC 日、且過了設定時點才發)。"""
    s = get_settings()
    now = datetime.now(timezone.utc)
    if now.hour < s.daily_summary_hour_utc:
        return
    today = now.date()
    if getattr(state, "last_daily_date", None) == today:
        return
    state.last_daily_date = today

    # 統計過去 24h 的 ERROR/RISK 警報數(判斷昨日是否正常)
    err_count = 0
    try:
        from app.db.models import Alert
        from app.db.session import db_session
        since = now - timedelta(hours=24)
        with db_session() as db:
            err_count = db.query(Alert).filter(
                Alert.sent_at >= since,
                Alert.level.in_(["RISK", "EXIT"])).count()
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily summary query failed: %s", exc)

    status = "運行正常" if err_count == 0 else f"有 {err_count} 則警報(請查 log)"
    if state.notifier:
        await state.notifier.notify(
            "INFO", "daily_summary",
            f"[DAILY] 昨日{status},LLM 成本 $0.00(MVP)",
            severity="INFO", force_push=True, bypass_cooldown=True)


async def run_monitor(state) -> None:
    """排程每 HEARTBEAT_MINUTES 呼叫;靜默監控,只在異常/每日摘要時推播。"""
    await _maybe_daily_summary(state)
    if not market_is_open():
        return
    if not state.notifier:
        return

    # 1) 元件死亡偵測(最嚴重)→ ERROR(含開機寬限期)
    dead = check_liveness(state.last_job_run, getattr(state, "started_at", None))
    if dead:
        await state.notifier.notify(
            "RISK", "component_down",
            f"元件停止運作:{', '.join(dead)}", severity="ERROR")
        return

    # 2) 資料延遲 → WARN
    last_t, age_min = _last_15m_candle()
    lag = get_settings().data_lag_warn_minutes
    if age_min is not None and age_min > lag:
        await state.notifier.notify(
            "RISK", "data_lag",
            f"資料延遲:最新 15M K 棒為 {int(age_min)} 分鐘前(門檻 {lag} 分),"
            f"provider={state.provider.name if state.provider else 'none'}",
            severity="WARN")
        return

    # 3) 一切正常 → 只寫 log,不推播
    logger.info("monitor ok: last 15M candle %s (%s min ago)",
                last_t.isoformat() if last_t else "n/a",
                int(age_min) if age_min is not None else "n/a")


# 向後相容別名(scheduler 舊呼叫)
send_heartbeat = run_monitor


def health_payload(state) -> dict:
    """GET /health 回應(供 UptimeRobot 等外部監控)。"""
    dead = (check_liveness(state.last_job_run, getattr(state, "started_at", None))
            if market_is_open() else [])
    last_t, age_min = _last_15m_candle()
    lag = get_settings().data_lag_warn_minutes
    data_lagging = age_min is not None and age_min > lag and market_is_open()
    from app.services.api_counter import snapshot
    s = get_settings()
    try:
        from app.providers.twelve_data import get_shared_quota
        td_used = get_shared_quota().used_today
    except Exception:  # noqa: BLE001
        td_used = None
    return {
        "status": "degraded" if (dead or data_lagging) else "ok",
        "market_open": market_is_open(),
        "provider": state.provider.name if state.provider else None,
        "tiered": {
            "fast_quote_provider": (state.fast_provider.name
                                    if getattr(state, "fast_provider", None) else None),
            "l1_degraded": getattr(state, "fast_provider", None) is None
                           and not s.mock_data_mode,
            "last_full_analysis": (state.last_full_analysis.isoformat()
                                   if getattr(state, "last_full_analysis", None) else None),
        },
        "api_usage_today": {**snapshot(), "twelve_data_quota": td_used,
                            "twelve_data_soft_limit": s.twelve_data_soft_limit},
        "dead_components": dead,
        "last_15m_candle": last_t.isoformat() if last_t else None,
        "data_lag_minutes": round(age_min, 1) if age_min is not None else None,
        "last_job_run": {k: v.isoformat() for k, v in state.last_job_run.items()},
        "notify_level": s.notify_level,
        "llm_cost_usd_today": 0.0,
    }
