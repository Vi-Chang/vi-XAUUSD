"""分析協調器:資料 → 品質 → 指標 → 結構 → 候選價位 → 狀態 → 規則引擎 → 固定 JSON。

MVP 全程無 LLM;輸出即符合 spec 二十二格式(AI 專屬欄位為預設/null),
Phase 7 的三角色 AI 將以本輸出 + candidate_levels 作為唯一輸入。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import PROMPT_VERSION, STRATEGY_VERSION
from app.config import get_settings
from app.i18n import state_zh
from app.db.models import (
    AnalysisRun, CandidateLevel as CandidateLevelRow, MarketStructure,
)
from app.db.session import db_session
from app.engines import data_quality, indicators, market_state
from app.engines.key_levels import build_candidate_levels, resolve_ids
from app.engines.market_structure import StructureReport, analyze_structure
from app.engines.rule_engine import decide
from app.providers.base import MarketDataProvider
from app.schemas.analysis import (
    AnalysisResult, BiasAnalysis, CurrentPrice, DataQuality, Decision, EventRisk,
    KeyLevels, Meta, PositionManagement, TimeframeView, Timeframes, TradingCoachView,
    validate_candidate_refs,
)
from app.services.candle_service import candles_to_df, refresh_candles
from app.services.event_service import evaluate_event_risk
from app.services.market_calendar import load_holidays
from app.utils.timeutils import to_taipei, trading_day

logger = logging.getLogger(__name__)

MISTAKE_BY_STATE = {
    "STRONG_BULL_TREND": "現在漲勢很強,別看到指標「超買」就急著放空,強勢盤可以一直買不停。",
    "STRONG_BEAR_TREND": "現在跌勢很強,別看到指標「超賣」就急著抄底,強勢盤可以一直跌不停。",
    "BULLISH_PULLBACK": "這只是漲勢中的正常回檔,別當成要反轉,更別在支撐附近追空。",
    "BEARISH_REBOUND": "這只是跌勢中的正常反彈,別當成要反轉,更別在壓力附近追多。",
    "RANGE": "現在是區間盤整,別在中間進場,很容易上下兩邊都被掃到。",
    "COMPRESSION": "現在窄幅整理、隨時要變盤,別猜方向,等 K 棒收盤真的突破再說。",
    "BREAKOUT_PENDING_CONFIRMATION": "K 棒還沒收盤,別急著當成突破成功,等這根收完再確認。",
    "BREAKDOWN_PENDING_CONFIRMATION": "K 棒還沒收盤,別急著當成跌破成功,等這根收完再確認。",
    "FAILED_BREAKOUT": "剛剛是假突破、又掉回來了,別再抱著「會漲」的想法不放。",
    "FAILED_BREAKDOWN": "剛剛是假跌破、又漲回來了,別再抱著「會跌」的想法不放。",
    "STRUCTURE_TRANSITION": "多空正在換手,別把大週期方向當成現在就能進場,短週期可能已經反轉了。",
    "EVENT_DRIVEN_VOLATILITY": "數據公布造成大波動,別用公布前的指標去猜公布後會漲還是跌。",
    "INSUFFICIENT_DATA": "資料還不夠,別硬逼自己一定要下單。",
}


def _tf_view(rep: StructureReport | None, ind: dict) -> TimeframeView:
    if rep is None:
        return TimeframeView(structure="INSUFFICIENT_DATA", momentum="",
                             interpretation="資料不足")
    from app.i18n import EVENT_TYPE_ZH
    labels = [s.label for s in rep.swings if s.label][-4:]
    hist = ind.get("macd_hist")
    momentum = ("動能偏多" if hist and hist > 0 else "動能偏空" if hist and hist < 0 else "動能中性")
    recent_ev = [EVENT_TYPE_ZH.get(e.event_type, e.event_type)
                 for e in rep.events[-3:] if e.still_valid]
    # structure 保留趨勢代碼開頭(前端膠囊顏色靠 UP/DOWN 判斷),後面加白話
    trend_zh = {"UP": "偏多", "DOWN": "偏空", "RANGE": "盤整", "UNKNOWN": "不明"}.get(rep.trend, "")
    return TimeframeView(
        structure=f"{rep.trend} {trend_zh}",
        momentum=momentum,
        interpretation=(f"近期訊號:{'、'.join(recent_ev) if recent_ev else '無'};"
                        f"附近高點 {rep.last_swing_high}、低點 {rep.last_swing_low}"),
    )


async def run_analysis(provider: MarketDataProvider, *, trigger: str = "manual",
                       symbol: str = "XAUUSD") -> AnalysisResult:
    """執行一次完整分析並存入 analysis_runs。"""
    s = get_settings()
    now = datetime.now(timezone.utc)
    holidays = load_holidays()

    # ── 1. 行情(統一剔除休市時段 K 棒,含假日表)──
    from app.services.candle_service import filter_market_hours
    all_tfs = tuple(dict.fromkeys((*s.analysis_timeframes, *s.aux_timeframes)))
    candles = await refresh_candles(provider, all_tfs, s.candle_history_count, symbol)
    candles = {tf: (cs if tf in ("1D", "1W") else filter_market_hours(cs, holidays))
               for tf, cs in candles.items()}
    tick = await provider.get_live_price(symbol)

    dfs_all = {tf: candles_to_df(c) for tf, c in candles.items()}
    dfs_closed = {tf: candles_to_df(c, closed_only=True) for tf, c in candles.items()}

    # ── 2. 指標(以已收線資料為準)──
    ind: dict[str, dict] = {}
    for tf in ("1D", "4H", "1H", "15M", "1W"):
        df = dfs_closed.get(tf)
        if df is None or len(df) < 30:
            ind[tf] = {}
            continue
        tds = None
        if tf in ("5M", "15M", "30M", "1H"):
            import pandas as pd
            tds = pd.Series([trading_day(t.to_pydatetime()) for t in df.index], index=df.index)
        ind[tf] = indicators.latest_snapshot(indicators.compute_all(df, tds))
    atr15 = ind.get("15M", {}).get("atr14") or (tick.mid * 0.001)

    # ── 3. 事件風險(MVP:manual fallback)──
    ev = evaluate_event_risk(now)

    # ── 4. 資料品質(含休市/事件放寬;STALE 門檻依 provider 輪詢頻率放寬)──
    poll = max(s.live_poll_seconds, getattr(provider, "min_poll_seconds", 0) or 0)
    quality = data_quality.evaluate(
        dfs_all, tick, atr15=atr15, holidays=holidays, now=now,
        event_window=(ev.level == "HIGH"),
        stale_after_seconds=max(s.stale_price_seconds, int(poll * 1.5)))

    # ── 5. 市場結構(只用已收線)──
    structures: dict[str, StructureReport] = {}
    for tf in ("1W", "1D", "4H", "1H", "15M"):
        df = dfs_closed.get(tf)
        if df is not None and len(df) >= 20:
            structures[tf] = analyze_structure(
                df, tf, left=s.swing_left_bars, right=s.swing_right_bars,
                min_atr_mult=s.swing_min_atr_mult, min_move_pct=s.swing_min_move_pct,
                fail_confirm_bars=s.false_break_confirm_bars,
                min_break_atr_mult=s.false_break_min_atr_mult)

    # ── 6. 候選價位(價位候選編號制,spec 八)──
    levels = build_candidate_levels(price=tick.mid, atr15=atr15,
                                    daily_df=dfs_all.get("1D", dfs_closed.get("1D")),
                                    structure_reports=structures)

    # ── 7. 市場狀態 ──
    state = market_state.classify(
        structures=structures, indicators_h1=ind.get("1H", {}),
        indicators_m15=ind.get("15M", {}), m15_df=dfs_all.get("15M"),
        event_volatility=(ev.level == "HIGH" and not ev.event_lockout))

    # ── 8. 規則引擎 ──
    decision = decide(quality=quality, structures=structures,
                      indicators_h1=ind.get("1H", {}), market_state=state,
                      price=tick.mid, atr15=atr15, levels=levels,
                      event_lockout=ev.event_lockout)

    # ── 9. 組固定輸出 JSON ──
    def zones(kind: str, strength: str) -> list[dict]:
        return [lv.to_dict() for lv in levels if lv.kind == kind and lv.strength == strength]

    result = AnalysisResult(
        timestamp_utc=now.isoformat(),
        timestamp_taipei=to_taipei(now).isoformat(),
        symbol=symbol,
        current_price=CurrentPrice(bid=tick.bid, ask=tick.ask, mid=tick.mid,
                                   spread=tick.spread, provider=tick.provider,
                                   last_update=tick.quote_time.isoformat()),
        data_quality=DataQuality(status=quality.status,
                                 missing_candles=quality.missing_candles[:20],
                                 source_mismatch=quality.source_mismatch,
                                 warnings=quality.warnings[:20]),
        event_risk=EventRisk(level=ev.level, event_lockout=ev.event_lockout,
                             next_event=ev.next_event,
                             minutes_remaining=ev.minutes_remaining,
                             source=ev.source, reason=ev.reason),
        market_state=state,
        timeframes=Timeframes(
            weekly=_tf_view(structures.get("1W"), ind.get("1W", {})),
            daily=_tf_view(structures.get("1D"), ind.get("1D", {})),
            h4=_tf_view(structures.get("4H"), ind.get("4H", {})),
            h1=_tf_view(structures.get("1H"), ind.get("1H", {})),
            m15=_tf_view(structures.get("15M"), ind.get("15M", {})),
        ),
        key_levels=KeyLevels(
            strong_resistance_zones=zones("RES_ZONE", "STRONG"),
            weak_resistance_zones=zones("RES_ZONE", "WEAK"),
            strong_support_zones=zones("SUP_ZONE", "STRONG"),
            weak_support_zones=zones("SUP_ZONE", "WEAK"),
        ),
        long_scenario=decision.long_scenario,
        short_scenario=decision.short_scenario,
        bias_analysis=BiasAnalysis(
            bull_pct=decision.bull_pct, bear_pct=decision.bear_pct,
            bull_evidence=decision.bull_evidence, bear_evidence=decision.bear_evidence,
            chase_flags=decision.chase_flags),
        decision=Decision(action=decision.action,
                          confidence_grade=decision.confidence_grade,
                          evidence_score=decision.evidence_score,
                          reason=decision.reason,
                          next_bullish_trigger="等 15 分K 收盤站上前高、而且不是追高的位置,才考慮做多",
                          next_bearish_trigger="等 15 分K 收盤跌破前低、而且不是追低的位置,才考慮做空",
                          next_recheck_time="下一根 15 分K 收盤後再看"),
        meta=Meta(prompt_version=PROMPT_VERSION, strategy_version=STRATEGY_VERSION,
                  model_version="rule-engine-only", llm_cost_usd_today=0.0),
        summary_zh_tw=f"【{state_zh(state)}】{decision.reason}",
        most_likely_user_mistake_now=MISTAKE_BY_STATE.get(state, ""),
    )

    # ── 9b. 我的持倉整合(持倉管理優先於尋找新交易)──
    # 注意:這裡只看「我實際下單的持倉」(positions 表)。老師帶單(mentor_signals)
    # 是獨立資料表,絕不進入此判斷 —— 只有老師帶單、我空手時仍正常找新交易。
    try:
        from app.services.position_service import (
            list_positions, position_view, recent_behavior_flags,
        )
        open_positions = list_positions(include_closed=False, limit=5)
        if open_positions:
            v = position_view(open_positions[0], tick.mid)
            result.position_management = PositionManagement(
                has_position=True, position_side=v["side"],
                entry_price=v["entry_price"],
                current_r_multiple=v["r_multiple"],
                recommended_action=v["recommended_action"],
                partial_exit_plan="回本後先落袋 2~3 成 → 到主要目標再落袋 3~5 成 → 留 2~4 成續抱趨勢",
                trailing_stop_plan="賠錢出場價跟著最近的 15 分K/1 小時結構往上移,別用固定金額亂移",
                full_exit_condition="到最終目標、行情正式反轉、原本的劇本壞了、移動停損被打到、或快出大數據要降風險",
                prohibited_actions=v["prohibited_actions"])
            if result.decision.action in ("WATCH", "PREPARE_LONG", "PREPARE_SHORT"):
                result.decision.action = "MANAGE"
                result.decision.reason = ("你手上已經有單了,先顧好這張單、別急著找新的。"
                                          + result.decision.reason)
        flags = recent_behavior_flags(limit=5)
        if flags:
            result.trading_coach = TradingCoachView(
                behavior_flags=[f["flag"] for f in flags],
                stop_loss_discipline=("提醒:你最近有把賠錢出場價往虧損方向挪(凹單)的紀錄"
                                       if any(f["flag"] == "STOP_WIDENING" for f in flags) else ""),
                early_exit_risk=("提醒:你最近有太早出場、沒抱到目標的紀錄"
                                  if any(f["flag"] == "EARLY_EXIT" for f in flags) else ""),
                message=flags[0]["corrective_action"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("position integration failed: %s", exc)

    # 候選 ID 引用驗證(規則引擎也必須通過同一道防線)
    known_ids = {lv.level_id for lv in levels}
    unknown = validate_candidate_refs(result, known_ids)
    if unknown:  # 程式錯誤,直接降級為 NO_TRADE
        logger.error("rule engine referenced unknown level ids: %s", unknown)
        result.decision.action = "NO_TRADE"
        result.decision.reason = f"NO_TRADE_AI_INVALID: unknown level ids {unknown}"
        result.decision.confidence_grade = "X"
    # 反查 ID → 實際數字(呈現用)
    for sc in (result.long_scenario, result.short_scenario):
        sc.resolved_prices = resolve_ids(levels, [sc.entry_zone_id, sc.stop_loss_id,
                                                  sc.invalidation_id, *sc.target_ids])

    # ── 9c. 老師帶單比對(純顯示;讀取最終 decision,絕不回饋影響決策/證據)──
    try:
        from app.schemas.analysis import MentorComparison
        from app.services.mentor_service import comparison_block
        result.mentor_comparison = MentorComparison(
            **comparison_block(result.decision.action, tick.mid))
    except Exception as exc:  # noqa: BLE001
        logger.warning("mentor comparison failed: %s", exc)

    # ── 10. 儲存 ──
    try:
        with db_session() as db:
            run = AnalysisRun(
                run_time=now, trigger=trigger, market_state=state,
                decision_action=result.decision.action,
                confidence_grade=result.decision.confidence_grade,
                evidence_score=result.decision.evidence_score,
                data_quality_status=quality.status,
                result_json=result.model_dump(),
                prompt_version=PROMPT_VERSION, strategy_version=STRATEGY_VERSION,
                model_version="rule-engine-only")
            db.add(run)
            db.flush()
            for lv in levels:
                db.add(CandidateLevelRow(analysis_run_id=run.id, level_id=lv.level_id,
                                         kind=lv.kind, price_low=lv.price_low,
                                         price_high=lv.price_high, strength=lv.strength,
                                         source=" + ".join(lv.sources)[:255], created_at=now))
            # 結構事件持久化(Dashboard 圖表標記用;以 tf+type+time 去重)
            from sqlalchemy import select as sa_select
            for tf, rep in structures.items():
                for ev in rep.events[-10:]:
                    row = db.execute(sa_select(MarketStructure).where(
                        MarketStructure.timeframe == tf,
                        MarketStructure.event_type == ev.event_type,
                        MarketStructure.event_time == ev.time,
                    )).scalar_one_or_none()
                    if row is None:
                        db.add(MarketStructure(
                            symbol=symbol, timeframe=tf, event_type=ev.event_type,
                            event_time=ev.time, price=ev.price,
                            confirming_candles=[t.isoformat() for t in ev.confirming_candles],
                            invalidation_price=ev.invalidation_price,
                            still_valid=ev.still_valid, created_at=now))
                    elif row.still_valid != ev.still_valid:
                        row.still_valid = ev.still_valid
    except Exception as exc:  # noqa: BLE001
        logger.error("persist analysis failed: %s", exc)

    return result
