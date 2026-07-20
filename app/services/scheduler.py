"""APScheduler 排程(spec 一;架構可日後切換 Celery — job 皆為獨立 async 函式)。

Jobs:
- poll_price:每 15 秒即時報價(僅交易時段)。
- m15_analysis:每 15 分鐘收線後 +10 秒觸發分析(LLM 觸發政策同一入口)。
- cross_check:每 15 分鐘 Twelve Data 交叉驗證(遠低於免費層 800/日)。
- heartbeat:每 30 分鐘心跳(spec 二十三)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.services.market_calendar import market_is_open

logger = logging.getLogger(__name__)


class AppState:
    """行程內共享狀態(main.py 建立;heartbeat 讀取做死亡偵測)。"""

    def __init__(self) -> None:
        self.provider = None
        self.secondary = None
        self.notifier = None
        self.latest_result: dict | None = None
        self.last_job_run: dict[str, datetime] = {}
        self.last_decision_action: str | None = None
        self.last_daily_date = None
        self.ws_clients: set = set()

    def mark(self, job: str) -> None:
        self.last_job_run[job] = datetime.now(timezone.utc)


state = AppState()


async def broadcast(payload: dict) -> None:
    """向所有 WebSocket 客戶端推送 JSON 訊息(壞連線自動清除)。"""
    import json
    msg = json.dumps(payload, ensure_ascii=False, default=str)
    dead = set()
    for ws in state.ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:  # noqa: BLE001
            dead.add(ws)
    state.ws_clients -= dead


async def job_poll_price() -> None:
    if not market_is_open():
        return
    state.mark("poll_price")
    try:
        tick = await state.provider.get_live_price()
        from app.db.models import LivePrice
        from app.db.session import db_session
        now = datetime.now(timezone.utc)
        with db_session() as db:
            db.add(LivePrice(symbol=tick.symbol, bid=tick.bid, ask=tick.ask,
                             mid=tick.mid, spread=tick.spread, provider=tick.provider,
                             quote_time=tick.quote_time, received_at=now))
        # 即時推送給 Dashboard(未收線 K 棒即時跳動用)
        await broadcast({"type": "tick", "bid": tick.bid, "ask": tick.ask,
                         "mid": tick.mid, "spread": tick.spread,
                         "time": int(tick.quote_time.timestamp())})
    except Exception as exc:  # noqa: BLE001
        logger.error("poll_price failed: %s", exc)


async def job_m15_analysis() -> None:
    """15M 收線後分析;狀態變化時通知(去重/冷卻由 NotificationManager 處理)。"""
    if not market_is_open():
        return  # 休市:不觸發分析、不呼叫 LLM(spec 一之觸發政策)
    state.mark("m15_analysis")
    try:
        from app.services.analysis_service import run_analysis
        result = await run_analysis(state.provider, trigger="m15_close")
        state.latest_result = result.model_dump()

        action = result.decision.action
        if state.notifier:
            if action != state.last_decision_action:
                level = ("TRIGGER" if action in ("LONG", "SHORT")
                         else "WATCH" if action.startswith(("PREPARE", "WATCH"))
                         else "INFO")
                await state.notifier.notify(
                    level, f"decision:{action}",
                    f"XAUUSD {result.market_state} → {action}\n"
                    f"{result.summary_zh_tw}\n"
                    f"最易犯的錯:{result.most_likely_user_mistake_now}")
            if result.data_quality.status in ("STALE", "FAILED"):
                await state.notifier.notify(
                    "RISK", "data_quality",
                    f"資料品質 {result.data_quality.status}: {result.data_quality.warnings[:3]}",
                    severity="ERROR" if result.data_quality.status == "FAILED" else "WARN")
        state.last_decision_action = action

        # WebSocket 廣播:收線事件 + 最新分析(套用 TMGM Offset 校正)
        from app.services.price_offset import apply_offset_to_result
        await broadcast({"type": "candle_closed", "timeframe": "15M"})
        await broadcast({"type": "analysis",
                         "data": apply_offset_to_result(state.latest_result)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("m15_analysis failed: %s", exc)
        if state.notifier:
            await state.notifier.notify("RISK", "analysis_error", f"分析失敗:{exc}",
                                        severity="ERROR")


async def job_cross_check() -> None:
    """Twelve Data 交叉驗證(每 15 分鐘一次 ≈ 96 次/日 << 800)。"""
    if not market_is_open() or state.secondary is None:
        return
    state.mark("cross_check")
    try:
        primary = await state.provider.get_live_price()
        secondary = await state.secondary.get_live_price()
        from app.engines.data_quality import check_source_mismatch
        mismatch, msg = check_source_mismatch(primary.mid, secondary.mid, None)
        if mismatch and state.notifier:
            await state.notifier.notify("RISK", "source_mismatch", msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cross_check failed: %s", exc)


async def job_heartbeat() -> None:
    state.mark("heartbeat")
    from app.services.heartbeat import send_heartbeat
    await send_heartbeat(state)


def effective_poll_seconds() -> int:
    """即時價輪詢間隔:設定值與 provider 下限取大者(如 Twelve Data 免費層 300s)。"""
    s = get_settings()
    provider_min = getattr(state.provider, "min_poll_seconds", 0) or 0
    return max(s.live_poll_seconds, provider_min)


def build_scheduler() -> AsyncIOScheduler:
    s = get_settings()
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(job_poll_price, "interval", seconds=effective_poll_seconds(),
                  id="poll_price", max_instances=1, coalesce=True)
    sched.add_job(job_m15_analysis, "cron", minute="0,15,30,45", second=10,
                  id="m15_analysis", max_instances=1, coalesce=True)
    sched.add_job(job_cross_check, "cron", minute="7,22,37,52", id="cross_check",
                  max_instances=1, coalesce=True)
    sched.add_job(job_heartbeat, "interval", minutes=s.heartbeat_minutes,
                  id="heartbeat", max_instances=1, coalesce=True)
    return sched
