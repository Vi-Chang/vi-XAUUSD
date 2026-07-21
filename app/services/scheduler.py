"""APScheduler 排程 — 三層更新頻率架構(沿用 APScheduler,不換框架)。

Jobs:
- quote_l1(第 1 層,預設 60s):快速報價源(Capital/OANDA)抓最新報價入快取;
  無快速源時降級為主力 provider 最低頻率(TD=300s)。禁 TD K 棒、禁 AI。
- structure_l2(第 2 層,預設 300s):純程式邏輯檢查觸及/突破/異常波動;
  事件成立 → 觸發第 3 層;另含 60 分鐘定時保底。禁 AI。
- full_analysis(第 3 層):由第 2 層觸發執行,非獨立排程。
- cross_check、heartbeat:沿用。

三層獨立:任一層例外只影響自己;第 3 層有定時保底兜底。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.services.market_calendar import market_is_open
from app.services.tiered import EventCooldown, QuoteCache

logger = logging.getLogger(__name__)


class AppState:
    """行程內共享狀態(main.py 建立;heartbeat 讀取做死亡偵測)。"""

    def __init__(self) -> None:
        self.provider = None
        self.secondary = None
        self.fast_provider = None            # 第 1 層快速報價源(可為 None → 降級)
        self.notifier = None
        self.latest_result: dict | None = None
        self.last_job_run: dict[str, datetime] = {}
        self.last_decision_action: str | None = None
        self.last_daily_date = None
        self.started_at: datetime | None = None
        self.ws_clients: set = set()
        # 三層架構狀態
        self.quote_cache = QuoteCache()
        self.event_cooldown = EventCooldown()
        self.last_full_analysis: datetime | None = None
        self.l1_fail_count = 0
        self.l1_alerted = False
        self.td_degraded_alerted = False

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


# ═══ 第 1 層:報價層 ═══════════════════════════════════════

def l1_provider():
    return state.fast_provider or state.provider


def l1_interval_seconds() -> int:
    """快速源存在 → tier1 設定值;否則降級為主力 provider 最低輪詢頻率。"""
    s = get_settings()
    if state.fast_provider is not None or s.mock_data_mode:
        return s.tier1_quote_seconds
    provider_min = getattr(state.provider, "min_poll_seconds", 0) or 0
    return max(s.tier1_quote_seconds, provider_min)


async def job_quote_l1() -> None:
    if not market_is_open():
        return
    state.mark("quote_l1")
    s = get_settings()
    provider = l1_provider()
    try:
        tick = await provider.get_live_price()
        from app.services.api_counter import bump
        bump(provider.name)
        state.quote_cache.add(tick)
        state.l1_fail_count = 0
        state.l1_alerted = False

        from app.db.models import LivePrice
        from app.db.session import db_session
        now = datetime.now(timezone.utc)
        with db_session() as db:
            db.add(LivePrice(symbol=tick.symbol, bid=tick.bid, ask=tick.ask,
                             mid=tick.mid, spread=tick.spread, provider=tick.provider,
                             quote_time=tick.quote_time, received_at=now))
        await broadcast({"type": "tick", "bid": tick.bid, "ask": tick.ask,
                         "mid": tick.mid, "spread": tick.spread,
                         "time": int(tick.quote_time.timestamp())})
    except Exception as exc:  # noqa: BLE001 — 靜默重試;連續失敗 N 次才警告一次
        state.l1_fail_count += 1
        logger.warning("quote_l1 failed (%d consecutive): %s", state.l1_fail_count, exc)
        if (state.l1_fail_count >= s.tier1_fail_alert_after
                and not state.l1_alerted and state.notifier):
            state.l1_alerted = True
            await state.notifier.notify(
                "RISK", "quote_l1_down",
                f"報價層連續 {state.l1_fail_count} 次抓不到價格"
                f"(來源 {provider.name}),請留意行情可能中斷", severity="WARN")


# ═══ 第 2 層:結構層(純邏輯,禁 AI)═══════════════════════

async def job_structure_l2() -> None:
    if not market_is_open():
        return
    state.mark("structure_l2")
    s = get_settings()
    try:
        tick = state.quote_cache.fresh_tick(max_age_seconds=l1_interval_seconds() * 3)
        events = []
        if tick is not None:
            from app.services.tiered import check_structure_events
            events = check_structure_events(tick.mid, state.quote_cache,
                                            state.event_cooldown)
        if events:
            reason = ";".join(e.reason_zh for e in events)
            await run_full_analysis(trigger="event", reason_zh=reason)
            return
        # 定時保底:距上次完整分析超過 tier3_max_age_minutes
        last = state.last_full_analysis
        overdue = (last is None or
                   (datetime.now(timezone.utc) - last).total_seconds()
                   > s.tier3_max_age_minutes * 60)
        if overdue:
            await run_full_analysis(trigger="timed", reason_zh=None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("structure_l2 failed: %s", exc)


# ═══ 第 3 層:完整分析(事件觸發 + 定時保底)═══════════════

def _td_soft_limited() -> bool:
    """TD 當日用量達軟上限 → 降級(K 棒只用快取,不再打 TD)。"""
    s = get_settings()
    try:
        from app.providers.twelve_data import get_shared_quota
        return get_shared_quota().used_today >= s.twelve_data_soft_limit
    except Exception:  # noqa: BLE001
        return False


async def run_full_analysis(*, trigger: str, reason_zh: str | None) -> None:
    """執行完整分析;事件觸發訊息帶 ⚡ 前綴,定時保底帶 🕐。"""
    state.mark("full_analysis")
    s = get_settings()
    try:
        degraded = _td_soft_limited()
        if degraded and not state.td_degraded_alerted and state.notifier:
            state.td_degraded_alerted = True
            await state.notifier.notify(
                "RISK", "td_soft_limit",
                f"Twelve Data 今日用量已達 {s.twelve_data_soft_limit} 次,"
                f"完整分析自動降級:改用既有快取 K 棒,不再打行情 API", severity="WARN")

        from app.services.analysis_service import run_analysis
        tick = state.quote_cache.fresh_tick(max_age_seconds=l1_interval_seconds() * 3)
        result = await run_analysis(state.provider, trigger=trigger,
                                    tick=tick, cached_only=degraded)
        state.latest_result = result.model_dump()
        state.last_full_analysis = datetime.now(timezone.utc)

        action = result.decision.action
        if state.notifier:
            if trigger == "event":
                await state.notifier.notify(
                    "TRIGGER", f"event:{(reason_zh or '')[:60]}",
                    f"⚡ 事件觸發:{reason_zh}\n"
                    f"{result.summary_zh_tw}\n"
                    f"提醒:{result.most_likely_user_mistake_now}",
                    severity="WARN")
            elif action != state.last_decision_action:
                level = ("TRIGGER" if action in ("LONG", "SHORT")
                         else "WATCH" if action.startswith(("PREPARE", "WATCH"))
                         else "INFO")
                await state.notifier.notify(
                    level, f"decision:{action}",
                    f"🕐 定時更新:XAUUSD {result.market_state} → {action}\n"
                    f"{result.summary_zh_tw}\n"
                    f"提醒:{result.most_likely_user_mistake_now}")
            if result.data_quality.status in ("STALE", "FAILED"):
                await state.notifier.notify(
                    "RISK", "data_quality",
                    f"資料品質 {result.data_quality.status}: {result.data_quality.warnings[:3]}",
                    severity="ERROR" if result.data_quality.status == "FAILED" else "WARN")
        state.last_decision_action = action

        from app.services.price_offset import apply_offset_to_result
        await broadcast({"type": "candle_closed", "timeframe": "15M"})
        await broadcast({"type": "analysis",
                         "data": apply_offset_to_result(state.latest_result)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("full_analysis failed: %s", exc)
        if state.notifier:
            await state.notifier.notify("RISK", "analysis_error", f"分析失敗:{exc}",
                                        severity="ERROR")


# ═══ 其他既有 jobs ═════════════════════════════════════════

async def job_cross_check() -> None:
    """Twelve Data 交叉驗證(主力=TD 時 secondary 為 None,自動跳過)。"""
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
    from app.services.heartbeat import run_monitor
    await run_monitor(state)


def build_scheduler() -> AsyncIOScheduler:
    s = get_settings()
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(job_quote_l1, "interval", seconds=l1_interval_seconds(),
                  id="quote_l1", max_instances=1, coalesce=True)
    sched.add_job(job_structure_l2, "interval", seconds=s.tier2_check_seconds,
                  id="structure_l2", max_instances=1, coalesce=True)
    sched.add_job(job_cross_check, "cron", minute="7,22,37,52", id="cross_check",
                  max_instances=1, coalesce=True)
    sched.add_job(job_heartbeat, "interval", minutes=s.heartbeat_minutes,
                  id="heartbeat", max_instances=1, coalesce=True)
    return sched
