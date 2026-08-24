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
from datetime import datetime, timedelta, timezone

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
        self.fast_provider = None  # 第 1 層快速報價源(可為 None → 降級)
        self.notifier = None
        self.latest_result: dict | None = None
        self.last_job_run: dict[str, datetime] = {}
        self.last_decision_action: str | None = None
        self.last_daily_date = None
        self.started_at: datetime | None = None
        # WebSocket 分流:公開頻道只收公開投影;私人頻道須有效 session,收完整 payload。
        self.ws_public: set = set()
        self.ws_private: dict = {}  # ws -> session_id(broadcast 時檢查過期)
        # 三層架構狀態
        self.quote_cache = QuoteCache()
        self.event_cooldown = EventCooldown()
        self.last_full_analysis: datetime | None = None
        self.candle_refresh_bucket: datetime | None = None
        self.candle_refresh_attempts = 0
        self.l1_fail_count = 0
        self.l1_alerted = False
        self.td_degraded_alerted = False
        # ── 監控用(readiness/health)──
        self.last_quote_ok_at: datetime | None = None  # 最後一次成功取得報價
        self.scheduler_started = False  # 排程是否已啟動
        self.last_market_freshness: str | None = None
        self.last_trigger_cross_key: str | None = None

    def mark(self, job: str) -> None:
        self.last_job_run[job] = datetime.now(timezone.utc)


state = AppState()


def _dump(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


async def broadcast_public(payload: dict) -> None:
    """推送給公開頻道(壞連線自動清除)。"""
    msg = _dump(payload)
    dead = set()
    for ws in state.ws_public:
        try:
            await ws.send_text(msg)
        except Exception:  # noqa: BLE001
            dead.add(ws)
    state.ws_public -= dead


async def broadcast_private(payload: dict) -> None:
    """推送給私人頻道;送出前檢查 session 是否仍有效,過期則關閉並移除。"""
    from app.security import session_valid

    msg = _dump(payload)
    dead = []
    for ws, sid in list(state.ws_private.items()):
        if not session_valid(sid):
            try:
                await ws.close(code=1008)  # session 過期 → 不再傳私人資料
            except Exception:
                logger.debug("failed to close expired private websocket", exc_info=True)
            dead.append(ws)
            continue
        try:
            await ws.send_text(msg)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        state.ws_private.pop(ws, None)


async def broadcast_all(payload: dict) -> None:
    """公開安全訊息(tick、candle_closed)→ 公開與私人頻道皆送。"""
    await broadcast_public(payload)
    await broadcast_private(payload)


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


def expected_closed_15m(
    now: datetime | None = None, delay_seconds: int | None = None
) -> datetime:
    """Open timestamp of the latest 15M candle expected to be closed and available."""
    now = now or datetime.now(timezone.utc)
    delay = (
        get_settings().candle_close_refresh_delay_seconds
        if delay_seconds is None
        else delay_seconds
    )
    eligible = now - timedelta(seconds=max(0, delay))
    close_boundary = eligible.replace(
        minute=(eligible.minute // 15) * 15, second=0, microsecond=0
    )
    return close_boundary - timedelta(minutes=15)


def _analysis_closed_15m() -> datetime | None:
    raw = ((state.latest_result or {}).get("normalized_analysis") or {}).get(
        "lastClosedCandleTimestamp"
    )
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def job_candle_close_refresh() -> None:
    """Refresh once a newly closed 15M candle should be available."""
    if not market_is_open():
        return
    state.mark("candle_close_refresh")
    s = get_settings()
    expected = expected_closed_15m()
    current = _analysis_closed_15m()
    if current is not None and current >= expected:
        state.candle_refresh_bucket = expected
        state.candle_refresh_attempts = 0
        return
    if state.candle_refresh_bucket != expected:
        state.candle_refresh_bucket = expected
        state.candle_refresh_attempts = 0
    if state.candle_refresh_attempts >= s.candle_close_refresh_max_attempts:
        return
    state.candle_refresh_attempts += 1
    await broadcast_all(
        {
            "type": "analysis_refreshing",
            "timeframe": "15M",
            "expected_close": expected.isoformat(),
        }
    )
    await run_full_analysis(
        trigger="candle_close", reason_zh="15 分鐘 K 棒已收盤，更新判斷"
    )
    if (
        _analysis_closed_15m() or datetime.min.replace(tzinfo=timezone.utc)
    ) >= expected:
        state.candle_refresh_attempts = 0


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
        state.last_quote_ok_at = datetime.now(timezone.utc)

        from app.db.models import LivePrice
        from app.db.session import db_session

        now = datetime.now(timezone.utc)
        with db_session() as db:
            db.add(
                LivePrice(
                    symbol=tick.symbol,
                    bid=tick.bid,
                    ask=tick.ask,
                    mid=tick.mid,
                    spread=tick.spread,
                    provider=tick.provider,
                    quote_time=tick.quote_time,
                    received_at=now,
                )
            )
        from app.utils.formatting import fmt_price

        await broadcast_all(
            {
                "type": "tick",
                "bid": fmt_price(tick.bid),
                "ask": fmt_price(tick.ask),
                "mid": fmt_price(tick.mid),
                "spread": fmt_price(tick.spread),
                "time": int(tick.quote_time.timestamp()),
            }
        )
        if state.latest_result:
            from app.services.market_monitor_service import evaluate_live_quote_state

            final_state, events = evaluate_live_quote_state(
                state.latest_result,
                price=tick.mid,
                quote_time=tick.quote_time.isoformat(),
            )
            state.latest_result["final_decision_state"] = final_state
            presentation = final_state.get("realtimePresentation") or {}
            state.latest_result["realtime_presentation"] = presentation
            state.latest_result["freshness_state"] = final_state.get("freshnessState") or {}
            latest_price = dict(state.latest_result.get("current_price") or {})
            latest_price.update({
                "bid": tick.bid, "ask": tick.ask, "mid": tick.mid,
                "spread": tick.spread, "last_update": tick.quote_time.isoformat(),
            })
            state.latest_result["current_price"] = latest_price
            latest_normalized = dict(state.latest_result.get("normalized_analysis") or {})
            latest_normalized.update({
                "currentPrice": tick.mid,
                "marketDataTimestamp": tick.quote_time.isoformat(),
                "sourceTimestamps": {
                    key: tick.quote_time.isoformat()
                    for key in (latest_normalized.get("sourceTimestamps") or {"market": ""})
                },
                "sourcePrices": {
                    key: tick.mid
                    for key in (latest_normalized.get("sourcePrices") or {"market": 0})
                },
            })
            state.latest_result["normalized_analysis"] = latest_normalized
            state.latest_result["snapshot_ts"] = tick.quote_time.isoformat()
            from app.engines.decision_snapshot import build_decision_snapshot
            state.latest_result["decision_snapshot"] = build_decision_snapshot(
                state.latest_result)
            await broadcast_all({"type": "decision_state", "data": final_state})
            for event in events:
                await broadcast_all({"type": "decision_event", "data": event})
            freshness_now = ((final_state.get("freshnessState") or {})
                             .get("marketFreshness") or {}).get("status")
            recovered = state.last_market_freshness == "stale" and freshness_now == "fresh"
            state.last_market_freshness = freshness_now
            trigger_key = None
            if presentation.get("intrabarCrossed") and not presentation.get("closedConfirmed"):
                trigger_key = f"{presentation.get('activeSetupId')}:{presentation.get('triggerPrice')}"
            crossed_new = bool(trigger_key and trigger_key != state.last_trigger_cross_key)
            if trigger_key:
                state.last_trigger_cross_key = trigger_key
            if recovered:
                await run_full_analysis(
                    trigger="freshness_recovered", reason_zh="行情資料恢復，立即重新判斷")
            elif crossed_new:
                await run_full_analysis(
                    trigger="breakout_crossed", reason_zh="即時價格穿越突破線，檢查盤中狀態")
        # 首次 provider session 可能耗時超過 APScheduler 的 misfire grace，導致原定
        # 第 10 秒執行的 L2 被跳過。首次報價成功後直接補一筆完整分析，避免新部署
        # 長時間停在 analysis_refresh_required；後續仍由 L2 事件／定時規則接手。
        if state.last_full_analysis is None:
            await run_full_analysis(
                trigger="startup", reason_zh="服務啟動後首次報價已就緒"
            )
    except Exception as exc:  # noqa: BLE001 — 靜默重試;連續失敗 N 次才警告一次
        state.l1_fail_count += 1
        logger.warning("quote_l1 failed (%d consecutive): %s", state.l1_fail_count, exc)
        if state.l1_fail_count >= s.tier1_fail_alert_after and not state.l1_alerted:
            state.l1_alerted = True
            if state.latest_result:
                stale_result = dict(state.latest_result)
                stale_normalized = dict(stale_result.get("normalized_analysis") or {})
                stale_normalized["marketDataStatus"] = "FAILED"
                stale_result["normalized_analysis"] = stale_normalized
                from app.services.market_monitor_service import (
                    evaluate_live_quote_state,
                )

                last = state.quote_cache.last_tick
                final_state, events = evaluate_live_quote_state(
                    stale_result, price=last.mid if last else 0,
                    quote_time=datetime.now(timezone.utc).isoformat())
                state.latest_result["final_decision_state"] = final_state
                state.latest_result["normalized_analysis"] = stale_normalized
                from app.engines.decision_snapshot import build_decision_snapshot
                state.latest_result["decision_snapshot"] = build_decision_snapshot(
                    state.latest_result)
                await broadcast_all({"type": "decision_state", "data": final_state})
                for event in events:
                    await broadcast_all({"type": "decision_event", "data": event})


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

            events = check_structure_events(
                tick.mid, state.quote_cache, state.event_cooldown
            )
        if events:
            reason = ";".join(e.reason_zh for e in events)
            await run_full_analysis(trigger="event", reason_zh=reason)
            return
        # BUGFIX R4:進行中方案的現價偏離檢查 → 過時即重算(帶冷卻防抖動)
        if tick is not None and state.latest_result:
            for key in ("long_scenario", "short_scenario"):
                sc = state.latest_result.get(key) or {}
                if sc.get("status") not in ("WATCH", "PREPARE", "TRIGGERED"):
                    continue
                rp = sc.get("resolved_prices") or {}
                source_price = float(sc.get("sourcePrice") or 0)
                dev = abs(tick.mid - source_price) / source_price if source_price else 0
                watched = [
                    value
                    for zone in rp.values()
                    if isinstance(zone, dict)
                    for value in (zone.get("price_low"), zone.get("price_high"))
                    if isinstance(value, (int, float))
                ]
                previous_tick = state.quote_cache.previous_tick
                crossed = bool(
                    previous_tick
                    and any(
                        min(previous_tick.mid, tick.mid)
                        <= level
                        <= max(previous_tick.mid, tick.mid)
                        for level in watched
                    )
                )
                if (
                    dev >= s.setup_stale_deviation_pct or crossed
                ) and state.event_cooldown.allow(f"setup_recalc:{key}", 1):
                    await run_full_analysis(
                        trigger="event",
                        reason_zh=(
                            "價格已到達劇本關鍵價位，重新計算"
                            if crossed
                            else f"現價相對劇本來源價變動 {dev:.2%}，重新計算"
                        ),
                    )
                    return

        # 定時保底:距上次完整分析超過 tier3_max_age_minutes
        last = state.last_full_analysis
        overdue = (
            last is None
            or (datetime.now(timezone.utc) - last).total_seconds()
            > s.tier3_max_age_minutes * 60
        )
        if overdue:
            await run_full_analysis(trigger="timed", reason_zh=None)
    except Exception:
        logger.exception("structure_l2 failed")


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
    """執行完整分析；事件觸發訊息帶 ⚡，startup／定時保底不主動發交易提醒。"""
    state.mark("full_analysis")
    s = get_settings()
    try:
        degraded = _td_soft_limited()
        if degraded and not state.td_degraded_alerted and state.notifier:
            state.td_degraded_alerted = True
            await state.notifier.notify(
                "RISK",
                "td_soft_limit",
                f"Twelve Data 今日用量已達 {s.twelve_data_soft_limit} 次,"
                f"完整分析自動降級:改用既有快取 K 棒,不再打行情 API",
                severity="WARN",
            )

        from app.services.single_flight import run_analysis_shared

        tick = state.quote_cache.fresh_tick(max_age_seconds=l1_interval_seconds() * 3)
        # single-flight:與手動 API / 首載共用同一道鎖,並發只實際跑一次
        result = await run_analysis_shared(
            state.provider, trigger=trigger, tick=tick, cached_only=degraded
        )
        state.latest_result = result.model_dump()
        state.last_full_analysis = datetime.now(timezone.utc)

        action = result.decision.action
        entry = state.latest_result.get("entry_engine") or {}
        if state.notifier:
            # Structural monitoring never terminates merely because a long plan was
            # invalidated.  Publish the breakdown/exit-risk event first, then the
            # independent entry-plan event.  Distinct event topics cannot block one another.
            from app.services.short_alert_service import process_short_alert

            await process_short_alert(state.latest_result, None, entry_plan=entry)
        for event in (state.latest_result.get("final_decision_state") or {}).get(
            "events", []
        ):
            await broadcast_all({"type": "decision_event", "data": event})
        state.last_decision_action = action

        from app.services.freshness import annotate_freshness
        from app.services.price_offset import apply_offset_to_result
        from app.services.public_view import public_analysis

        fresh_tick = state.quote_cache.fresh_tick(max_age_seconds=600)
        cm = fresh_tick.mid if fresh_tick else None
        await broadcast_all({"type": "candle_closed", "timeframe": "15M"})
        # 公開頻道:公開投影(不含個人 offset/持倉/老師);私人頻道:完整 payload
        full = annotate_freshness(
            apply_offset_to_result(state.latest_result), current_mid=cm
        )
        pub = public_analysis(annotate_freshness(state.latest_result, current_mid=cm))
        await broadcast_public({"type": "analysis", "data": pub})
        await broadcast_private({"type": "analysis", "data": full})
    except Exception as exc:
        logger.exception("full_analysis failed")
        if state.notifier:
            await state.notifier.notify(
                "RISK", "analysis_error", f"分析失敗:{exc}", severity="ERROR"
            )


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


async def job_outcome_backfill() -> None:
    """Recover settled outcomes even when no full analysis ran at a horizon."""
    state.mark("outcome_backfill")
    s = get_settings()
    try:
        from app.db.session import db_session
        from app.services.decision_event_outcomes import (
            backfill_decision_event_outcomes,
        )
        from app.services.decision_replay import backfill_decision_replay_outcomes
        from app.services.outcome_tracker import backfill_outcomes

        with db_session() as db:
            updated = backfill_outcomes(
                db,
                now=datetime.now(timezone.utc),
                lookback_days=s.outcome_backfill_lookback_days,
                limit=s.outcome_backfill_batch_size,
            )
            event_updated = backfill_decision_event_outcomes(
                db, now=datetime.now(timezone.utc),
                lookback_days=s.outcome_backfill_lookback_days,
                limit=s.outcome_backfill_batch_size)
            replay_updated = backfill_decision_replay_outcomes(
                db, now=datetime.now(timezone.utc),
                lookback_days=s.outcome_backfill_lookback_days,
                limit=s.outcome_backfill_batch_size)
        if updated:
            logger.info("outcome backfill updated %d horizon values", updated)
        if event_updated:
            logger.info("decision event outcomes updated %d rows", event_updated)
        if replay_updated:
            logger.info("decision replay outcomes updated %d rows", replay_updated)
    except Exception:
        logger.exception("outcome backfill failed")


async def job_telegram_outbox() -> None:
    """Retry-safe Telegram delivery; rows survive restarts."""
    state.mark("telegram_outbox")
    from app.services.decision_outbox import deliver_pending_telegram

    await deliver_pending_telegram()


def build_scheduler() -> AsyncIOScheduler:
    s = get_settings()
    sched = AsyncIOScheduler(timezone="UTC")
    startup = datetime.now(timezone.utc)
    sched.add_job(
        job_quote_l1,
        "interval",
        seconds=l1_interval_seconds(),
        id="quote_l1",
        max_instances=1,
        coalesce=True,
        next_run_time=startup,
    )
    sched.add_job(
        job_structure_l2,
        "interval",
        seconds=s.tier2_check_seconds,
        id="structure_l2",
        max_instances=1,
        coalesce=True,
        next_run_time=startup + timedelta(seconds=10),
    )
    sched.add_job(
        job_candle_close_refresh,
        "interval",
        seconds=30,
        id="candle_close_refresh",
        max_instances=1,
        coalesce=True,
        next_run_time=startup + timedelta(seconds=20),
    )
    sched.add_job(
        job_outcome_backfill,
        "interval",
        seconds=s.outcome_backfill_seconds,
        id="outcome_backfill",
        max_instances=1,
        coalesce=True,
        next_run_time=startup + timedelta(seconds=60),
    )
    sched.add_job(
        job_telegram_outbox,
        "interval",
        seconds=5,
        id="telegram_outbox",
        max_instances=1,
        coalesce=True,
        next_run_time=startup + timedelta(seconds=3),
    )
    sched.add_job(
        job_cross_check,
        "cron",
        minute="7,22,37,52",
        id="cross_check",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        job_heartbeat,
        "interval",
        minutes=s.heartbeat_minutes,
        id="heartbeat",
        max_instances=1,
        coalesce=True,
    )
    return sched
