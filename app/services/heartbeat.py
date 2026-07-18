"""系統心跳與死亡偵測(spec 二十三,強制)。

- 交易時段內每 HEARTBEAT_MINUTES 發送:資料源狀態、最後 K 棒時間、當日 LLM 花費。
- 任一關鍵 job 超過 5 分鐘未執行(交易時段內)→ RISK 警報。
理由:沒有心跳,系統掛掉時你會以為「今天只是沒訊號」。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.services.market_calendar import market_is_open

logger = logging.getLogger(__name__)

CRITICAL_JOBS = {"poll_price": 300, "m15_analysis": 1200}  # job → 容忍秒數


def check_liveness(last_job_run: dict[str, datetime]) -> list[str]:
    """回傳停止運作的元件清單。"""
    now = datetime.now(timezone.utc)
    dead = []
    for job, tolerance in CRITICAL_JOBS.items():
        last = last_job_run.get(job)
        if last is None or (now - last).total_seconds() > tolerance:
            dead.append(f"{job} (last={last.isoformat() if last else 'never'})")
    return dead


async def send_heartbeat(state) -> None:
    if not market_is_open():
        return
    last_candle = "n/a"
    try:
        from app.db.models import Candle
        from app.db.session import db_session
        with db_session() as db:
            row = db.execute(select(Candle).where(Candle.timeframe == "15M")
                             .order_by(Candle.open_time.desc()).limit(1)).scalar_one_or_none()
            if row:
                last_candle = row.open_time.isoformat()
    except Exception as exc:  # noqa: BLE001
        last_candle = f"db error: {exc}"

    dead = check_liveness(state.last_job_run)
    provider_name = state.provider.name if state.provider else "none"
    msg = (f"Heartbeat OK\nprovider={provider_name}, last 15M candle={last_candle}, "
           f"LLM cost today=$0.00 (MVP)")
    if state.notifier:
        if dead:
            await state.notifier.notify("RISK", "component_down",
                                        f"元件停止運作:{', '.join(dead)}")
        else:
            await state.notifier.notify("INFO", "heartbeat", msg, bypass_cooldown=True)


def health_payload(state) -> dict:
    """GET /health 回應(供 UptimeRobot 等外部監控)。"""
    dead = check_liveness(state.last_job_run) if market_is_open() else []
    return {
        "status": "degraded" if dead else "ok",
        "market_open": market_is_open(),
        "provider": state.provider.name if state.provider else None,
        "dead_components": dead,
        "last_job_run": {k: v.isoformat() for k, v in state.last_job_run.items()},
        "llm_cost_usd_today": 0.0,
    }
