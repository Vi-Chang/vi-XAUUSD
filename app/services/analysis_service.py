"""分析協調器:資料 → 品質 → 指標 → 結構 → 候選價位 → 狀態 → 規則引擎 → 固定 JSON。

MVP 全程無 LLM;輸出即符合 spec 二十二格式(AI 專屬欄位為預設/null),
Phase 7 的三角色 AI 將以本輸出 + candidate_levels 作為唯一輸入。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal, cast

import pandas as pd

from app import PROMPT_VERSION, STRATEGY_VERSION
from app.config import get_settings
from app.db.models import (
    AnalysisRun,
    MarketStructure,
)
from app.db.models import (
    CandidateLevel as CandidateLevelRow,
)
from app.db.session import db_session
from app.engines import data_quality, indicators, market_state
from app.engines.key_levels import build_candidate_levels, resolve_ids
from app.engines.market_structure import StructureReport, analyze_structure
from app.engines.normalized_analysis import build_normalized_state
from app.engines.rule_engine import decide
from app.i18n import state_zh
from app.providers.base import MarketDataProvider, PriceTick
from app.schemas.analysis import (
    AnalysisResult,
    BiasAnalysis,
    ConfidenceGrade,
    CurrentPrice,
    DataQuality,
    DataQualityStatus,
    Decision,
    DecisionAction,
    EventImpact,
    EventRisk,
    EventSource,
    KeyLevels,
    Meta,
    PositionManagement,
    Timeframes,
    TimeframeView,
    TradingCoachView,
    validate_candidate_refs,
)
from app.services.candle_service import candles_to_df, refresh_candles
from app.services.event_service import evaluate_event_risk
from app.services.market_calendar import load_holidays
from app.utils.formatting import fmt_price
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

# 「最易犯的錯」重複偵測(BUGFIX R6:連續 N 版相同 → log 供人工檢查)
_recent_mistakes: list[str] = []


def build_mistake(state: str, action: str, chase_flags: list[str],
                  scenario_invalid: bool) -> str:
    """由當前快照實際狀態組合「最容易犯的錯」(方向/追價/失效情境化,非靜態查表)。"""
    parts = [MISTAKE_BY_STATE.get(state, "")]
    if scenario_invalid:
        parts.append("系統剛攔截了一組矛盾價位,最不該做的就是自己憑感覺補一組進場價。")
    elif action in ("PREPARE_SHORT", "SHORT"):
        parts.append("現在劇本偏空,最容易手癢的是逆勢接多——別接。")
    elif action in ("PREPARE_LONG", "LONG"):
        parts.append("現在劇本偏多,最容易手癢的是嫌貴不敢上車又臨時逆勢放空——都別。")
    elif action == "MANAGE":
        parts.append("手上有單,最容易犯的是盯著浮動損益亂動單。")
    if chase_flags:
        parts.append("而且現在位置偏追價,更要忍住不追。")
    text = "".join(p for p in parts if p)

    from app.config import get_settings
    _recent_mistakes.append(text)
    n = get_settings().mistake_repeat_log_versions
    if len(_recent_mistakes) >= n and len(set(_recent_mistakes[-n:])) == 1:
        logger.warning("MISTAKE_TEXT_UNCHANGED for %d consecutive versions — "
                       "check generation logic (text=%s)", n, text[:60])
    del _recent_mistakes[:-n]
    return text


def _tf_view(rep: StructureReport | None, ind: dict) -> TimeframeView:
    if rep is None:
        return TimeframeView(structure="INSUFFICIENT_DATA", momentum="",
                             interpretation="資料不足")
    from app.i18n import EVENT_TYPE_ZH
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


def _apply_no_trade_gate(result: AnalysisResult, elig) -> None:
    """交易資格閘門不合格 → 強制 NO_TRADE、清除可執行劇本、市場層決策同步降級。

    - 新入場動作(WATCH/PREPARE/LONG/SHORT)→ NO_TRADE(不得保留新入場指令)。
    - 既有持倉管理(MANAGE)保留(非新入場),但附資料提醒。
    - 公開市場層決策(market_decision)一律 NO_TRADE(公開端不得出現可執行 BUY/SELL)。
    - 劇本剝除可執行價位,避免被當成有效新入場方案。
    """
    qt = result.snapshot_ts or result.current_price.last_update or ""
    reason = f"{elig.reason}(資料更新時間:{qt})" if qt else elig.reason
    new_entry = ("WATCH", "PREPARE_LONG", "PREPARE_SHORT", "LONG", "SHORT")
    if result.decision.action in new_entry:
        result.decision.action = "NO_TRADE"
        result.decision.confidence_grade = "X"
        result.decision.evidence_score = 0
        result.decision.reason = reason
    elif result.decision.action == "MANAGE":
        result.decision.reason = f"(資料提醒:{elig.reason})" + result.decision.reason
    result.market_decision.action = "NO_TRADE"
    result.market_decision.confidence_grade = "X"
    result.market_decision.evidence_score = 0
    result.market_decision.reason = reason

    def _neutralize(sc):
        return sc.model_copy(update={
            "status": "WATCH", "entry_zone_id": None, "stop_loss_id": None,
            "invalidation_id": None, "target_ids": [], "risk_reward": [],
            "resolved_prices": {}})
    result.long_scenario = _neutralize(result.long_scenario)
    result.short_scenario = _neutralize(result.short_scenario)


async def run_analysis(provider: MarketDataProvider, *, trigger: str = "manual",
                       symbol: str = "XAUUSD", tick: PriceTick | None = None,
                       cached_only: bool = False) -> AnalysisResult:
    """執行一次完整分析並存入 analysis_runs。

    tick:第 1 層報價快取的新鮮報價(提供時不再打 API 取價)。
    cached_only:TD 軟上限降級模式 —— K 棒只用 DB 既有資料,不打任何行情 API。
    """
    s = get_settings()
    now = datetime.now(timezone.utc)
    holidays = load_holidays()

    # ── 1. 行情(統一剔除休市時段 K 棒,含假日表)──
    from app.services.candle_service import filter_market_hours, load_candles_from_db
    all_tfs = tuple(dict.fromkeys((*s.analysis_timeframes, *s.aux_timeframes)))
    if cached_only:
        candles = {tf: load_candles_from_db(tf, s.candle_history_count, symbol)
                   for tf in all_tfs}
    else:
        candles = await refresh_candles(provider, all_tfs, s.candle_history_count, symbol)
    candles = {tf: (cs if tf in ("1D", "1W") else filter_market_hours(cs, holidays))
               for tf, cs in candles.items()}
    if tick is None:
        tick = await provider.get_live_price(symbol)

    dfs_all = {tf: candles_to_df(c) for tf, c in candles.items()}
    dfs_closed = {tf: candles_to_df(c, closed_only=True) for tf, c in candles.items()}

    # ── 2. 指標(以已收線資料為準)──
    ind: dict[str, dict] = {}
    closed_times: dict[str, str] = {}
    for tf in ("1D", "4H", "1H", "15M", "1W"):
        df = dfs_closed.get(tf)
        if df is None or len(df) < 30:
            ind[tf] = {}
            continue
        tds = None
        if tf in ("5M", "15M", "30M", "1H"):
            tds = pd.Series([trading_day(t.to_pydatetime()) for t in df.index], index=df.index)
        computed = indicators.compute_all(df, tds)
        ind[tf] = indicators.latest_snapshot(computed)
        if len(computed) >= 2:
            prev = computed.iloc[-2]
            for key in ("macd_hist", "rsi6", "rsi12", "rsi14", "stoch_k", "stoch_d"):
                value = prev.get(key)
                if value is not None:
                    ind[tf][f"{key}_prev"] = None if pd.isna(value) else round(float(value), 4)
        if len(computed) >= 3:
            value = computed.iloc[-3].get("macd_hist")
            ind[tf]["macd_hist_prev2"] = (
                None if value is None or pd.isna(value) else round(float(value), 4))
        closed_times[tf] = df.index[-1].isoformat()
    atr15 = ind.get("15M", {}).get("atr14") or (tick.mid * 0.001)

    # ── 3. 事件風險(MVP:manual fallback)──
    ev = evaluate_event_risk(now)

    # ── 4. 資料品質(含休市/事件放寬;STALE 門檻依 provider 輪詢頻率放寬)──
    poll = max(s.live_poll_seconds, getattr(provider, "min_poll_seconds", 0) or 0)
    quality = data_quality.evaluate(
        dfs_all, tick, atr15=atr15, holidays=holidays, now=now,
        event_window=(ev.event_impact == "HIGH" and ev.time_risk == "HIGH"),
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
        event_volatility=(ev.event_impact == "HIGH" and ev.time_risk == "HIGH"
                          and not ev.event_lockout),
        price=tick.mid)

    # ── 8. 規則引擎 ──
    previous_action = None
    try:
        from sqlalchemy import select as sa_select
        with db_session() as db:
            previous = db.execute(
                sa_select(AnalysisRun).order_by(AnalysisRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            previous_action = previous.decision_action if previous else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("previous analysis state unavailable: %s", exc)

    decision = decide(quality=quality, structures=structures,
                      indicators_h1=ind.get("1H", {}), market_state=state,
                      price=tick.mid, atr15=atr15, levels=levels,
                      event_lockout=ev.event_lockout,
                      previous_action=previous_action,
                      m15_df=dfs_closed.get("15M"))

    # API 與 normalized state 必須共用完全相同的快照價格與精度。
    snapshot_price = cast(float, fmt_price(tick.mid))

    # ── 9. 組固定輸出 JSON ──
    def zones(kind: str, strength: str) -> list[dict]:
        return [lv.to_dict() for lv in levels if lv.kind == kind and lv.strength == strength]

    result = AnalysisResult(
        timestamp_utc=now.isoformat(),
        timestamp_taipei=to_taipei(now).isoformat(),
        symbol=symbol,
        current_price=CurrentPrice(bid=fmt_price(tick.bid), ask=fmt_price(tick.ask),
                                   mid=snapshot_price, spread=fmt_price(tick.spread),
                                   provider=tick.provider,
                                   last_update=tick.quote_time.isoformat()),
        data_quality=DataQuality(status=cast(DataQualityStatus, quality.status),
                                 missing_candles=quality.missing_candles[:20],
                                 source_mismatch=quality.source_mismatch,
                                 warnings=quality.warnings[:20]),
        event_risk=EventRisk(event_impact=cast(EventImpact, ev.event_impact),
                             time_risk=cast(EventImpact, ev.time_risk),
                             level=cast(EventImpact, ev.level), event_lockout=ev.event_lockout,
                             next_event=ev.next_event,
                             minutes_remaining=ev.minutes_remaining,
                              source=cast(EventSource, ev.source), reason=ev.reason,
                             data_updated_at=ev.data_updated_at,
                             event_phase=cast(Literal["upcoming", "post_release", "unknown"], ev.event_phase),
                             post_event_wait=ev.post_event_wait),
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
        decision=Decision(action=cast(DecisionAction, decision.action),
                          confidence_grade=cast(ConfidenceGrade, decision.confidence_grade),
                          evidence_score=decision.evidence_score,
                          reason=decision.reason,
                          next_bullish_trigger="等 15 分K 收盤站上前高、而且不是追高的位置,才考慮做多",
                          next_bearish_trigger="等 15 分K 收盤跌破前低、而且不是追低的位置,才考慮做空",
                          next_recheck_time="下一根 15 分K 收盤後再看"),
        meta=Meta(prompt_version=PROMPT_VERSION, strategy_version=STRATEGY_VERSION,
                  model_version="rule-engine-only", llm_cost_usd_today=0.0),
        summary_zh_tw=f"【{state_zh(state)}】{decision.reason}",
        most_likely_user_mistake_now=build_mistake(
            state, decision.action, decision.chase_flags,
            scenario_invalid=(decision.long_scenario.status == "INVALID"
                              or decision.short_scenario.status == "INVALID")),
    )
    result.snapshot_ts = tick.quote_time.isoformat()
    # 唯一分析狀態：在 API/DB 回傳前完成，後續 UI 與相容欄位均由它回填。
    normalized = build_normalized_state(
        generated_at=now.isoformat(), market_timestamp=tick.quote_time.isoformat(),
        current_price=snapshot_price, market_state=state,
        market_quality=quality.status, event_source=ev.source,
        event_stale=ev.data_stale, structures=structures,
        m15_all=dfs_all.get("15M"), m15_closed=dfs_closed.get("15M"),
        bull_evidence=decision.bull_evidence, bear_evidence=decision.bear_evidence,
        chase_flags=decision.chase_flags, indicators=ind, closed_times=closed_times,
        atr15=atr15, event_timestamp=ev.data_updated_at,
        event_risk=(ev.time_risk or "UNKNOWN").lower(), event_lockout=ev.event_lockout)
    result.normalized_analysis = normalized
    # 舊 API 欄位保留，但不可再自行判斷；全部鏡射 normalized state。
    result.bias_analysis = BiasAnalysis(
        bull_pct=normalized.bullPct, bear_pct=normalized.bearPct,
        bull_evidence=[x.label for x in normalized.longEvidence],
        bear_evidence=[x.label for x in normalized.shortEvidence],
        chase_flags=([f"RISK:{normalized.riskLabel}"]
                     if normalized.riskDirection != "none" else []),
        disclaimer="技術證據傾向採週期與家族加權；此數值不是勝率。")
    result.summary_zh_tw = normalized.tradingScript
    result.most_likely_user_mistake_now = normalized.mostLikelyMistake
    if normalized.entryTiming in ("wait", "invalid"):
        result.decision.action = "NO_TRADE" if normalized.entryTiming == "invalid" else "WATCH"
        result.decision.reason = normalized.consistencyMessage or normalized.tradingScript
        safe_watch: dict[str, object] = {"status": "WATCH", "entry_zone_id": None, "stop_loss_id": None,
                      "invalidation_id": None, "target_ids": [], "risk_reward": [],
                      "resolved_prices": {}}
        result.long_scenario = result.long_scenario.model_copy(update=safe_watch)
        result.short_scenario = result.short_scenario.model_copy(update=safe_watch)
    # 市場層決策快照(在持倉 MANAGE 覆寫之前捕捉);公開投影用此,避免洩露個人持倉。
    result.market_decision = result.decision.model_copy()
    # 隱私邊界戳記:本 pipeline 為 position-free / public-safe,標記為可公開自由文字。
    from app.services.public_view import PRIVACY_BOUNDARY_VERSION
    result.privacy_boundary_version = PRIVACY_BOUNDARY_VERSION

    # ── 9b. 我的持倉整合(持倉管理優先於尋找新交易)──
    # 注意:這裡只看「我實際下單的持倉」(positions 表)。老師帶單(mentor_signals)
    # 是獨立資料表,絕不進入此判斷 —— 只有老師帶單、我空手時仍正常找新交易。
    try:
        from app.services.position_service import (
            list_positions,
            position_view,
            recent_behavior_flags,
        )
        open_positions = list_positions(include_closed=False, limit=5)
        if open_positions:
            v = position_view(open_positions[0], tick.mid)
            from app.engines.position_assessment import (
                PositionContext,
                assess_trading_decision,
            )
            context = PositionContext(
                direction=v["side"].lower(), entry_price=v["entry_price"],
                size=v["lot_size"], original_stop=v.get("stop_loss"),
                max_loss_usd=v.get("max_loss_usd"),
                timeframe=v.get("position_timeframe", "unknown"),
                thesis=v.get("original_thesis", ""),
                allow_event_hold=v.get("allow_event_hold"))
            position_decision = assess_trading_decision(
                market_regime=normalized.marketRegime,
                weakness=normalized.shortTermWeakness,
                oversold=(normalized.tradingDecision.marketAssessment.twoSidedRisk
                          in ("oversold_rebound", "high_whipsaw")),
                reversal_state=normalized.tradingDecision.marketAssessment.reversalState,
                readiness=normalized.entryReadiness,
                long_allowed=normalized.longEntryAllowed,
                short_allowed=normalized.shortEntryAllowed,
                position_risk=normalized.positionRisk,
                context=context)
            normalized.tradingDecision.existingPositionAssessment = (
                position_decision.existingPositionAssessment)
            result.normalized_analysis = normalized
            position_action = position_decision.existingPositionAssessment.message
            result.position_management = PositionManagement(
                has_position=True, position_side=v["side"],
                entry_price=v["entry_price"],
                current_r_multiple=v["r_multiple"],
                recommended_action=position_action,
                partial_exit_plan="回本後先落袋 2~3 成 → 到主要目標再落袋 3~5 成 → 留 2~4 成續抱趨勢",
                trailing_stop_plan="賠錢出場價跟著最近的 15 分K/1 小時結構往上移,別用固定金額亂移",
                full_exit_condition="到最終目標、行情正式反轉、原本的劇本壞了、移動停損被打到、或快出大數據要降風險",
                prohibited_actions=v["prohibited_actions"],
                current_price=fmt_price(tick.mid), unrealized_pnl=v["unrealized_pnl"],
                structural_risk=normalized.structuralInvalidationNote,
                account_risk=("依使用者輸入的進場價、停損與手數計算；不得由市場結構代替。"
                              if v.get("stop_loss") is not None else
                              "未提供有效停損，無法計算個人帳戶風險。"),
                risk_release_condition=(normalized.invalidationConditions[0].message
                                        if normalized.invalidationConditions else
                                        "等待短線動能與已收盤結構恢復一致。"),
                data_timestamp=normalized.marketDataTimestamp)
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
    # 反查 ID → 實際數字(呈現用)。Scenario 為 frozen(R1/TC-08):
    # 禁止逐欄修改,一律 model_copy 整組替換。
    def _stamped(sc):
        return sc.model_copy(update={
            "resolved_prices": resolve_ids(levels, [sc.entry_zone_id, sc.stop_loss_id,
                                                    sc.invalidation_id, *sc.target_ids]),
            "created_at": now.isoformat(),
            "snapshot_ts": tick.quote_time.isoformat(),
        })
    result.long_scenario = _stamped(result.long_scenario)
    result.short_scenario = _stamped(result.short_scenario)

    # ── 9c. 老師帶單比對(純顯示;讀取最終 decision,絕不回饋影響決策/證據)──
    try:
        from app.schemas.analysis import MentorComparison
        from app.services.mentor_service import comparison_block
        result.mentor_comparison = MentorComparison(
            **comparison_block(result.decision.action, tick.mid))
    except Exception as exc:  # noqa: BLE001
        logger.warning("mentor comparison failed: %s", exc)

    # ── 9c2. 交易資格閘門(單一權威;必在 AI 呼叫前執行)──
    # 資料過期/異常、來源異常、休市、K 棒不足或證據不足 → 一律 NO_TRADE(資料不足,暫不交易),
    # 清除可執行劇本、市場層決策同步降級,並跳過付費 AI(省成本),
    # 確保不輸出可被誤認為有效的新入場指令。
    from app.engines.trade_gate import evaluate_trade_eligibility
    # 證據門檻只約束「可執行動作」(PREPARE/LONG/SHORT);WATCH 本就非可執行,不因此改判 NO_TRADE。
    _actionable = result.decision.action in ("PREPARE_LONG", "PREPARE_SHORT", "LONG", "SHORT")
    elig = evaluate_trade_eligibility(
        tick=tick, quality=quality, market_state=state, atr15=atr15,
        evidence_score=(result.decision.evidence_score if _actionable else None),
        now=now, is_fallback=cached_only)
    result.trade_eligibility = elig
    if not elig.eligible:
        _apply_no_trade_gate(result, elig)

    # ── 9d. V2 AI 分析層(4 Agent;任何失敗不影響上面的確定性輸出)──
    try:
        from app.llm.client import llm_available
        from app.schemas.ai import AiStrategy
        # 閘門不合格 → 在呼叫 AI 前即停止(省 token),不打任何 AI。
        ok_ai, ai_reason = (False, "") if not elig.eligible else llm_available()
        if not elig.eligible:
            result.ai_strategy = AiStrategy(
                unavailable_reason=f"資料品質閘門:{elig.reason}",
                gate_note=f"程式風控:未過交易資格閘門({elig.code}),AI 呼叫前即停止出訊")
        elif not ok_ai:
            result.ai_strategy = AiStrategy(unavailable_reason=ai_reason)
        else:
            from app.services.price_offset import get_offset_for
            off = get_offset_for(tick.provider or "")
            # 隱私邊界:AI 為「公開市場分析」,不餵入個人持倉/老師資料,
            # 確保公開 ai_strategy 文字不會引用私人內容(持倉管理由確定性引擎另行私有輸出)。
            from app.llm.service import generate_ai_strategy
            result.ai_strategy = await generate_ai_strategy(
                price=tick.mid, atr15=atr15, state=state,
                quality_status=quality.status, ev=ev, ind=ind,
                structures=structures, levels=levels, dfs_closed=dfs_closed,
                bias=result.bias_analysis, position=None,
                no_signal=not off.get("calibrated", False), normalized=normalized)
            # 跨市場資料同步填入顯示欄位(讀快取,不重複抓)
            from app.services.cross_market import get_cross_market
            cross = await get_cross_market()
            cm = result.cross_market_context
            cm.dxy = (f"{cross.dxy}({cross.dxy_chg_pct:+.2f}%)"
                      if cross.dxy is not None and cross.dxy_chg_pct is not None
                      else (str(cross.dxy) if cross.dxy is not None else ""))
            cm.us10y = (f"{cross.us10y}%({cross.us10y_chg:+.2f})"
                        if cross.us10y is not None and cross.us10y_chg is not None
                        else (f"{cross.us10y}%" if cross.us10y is not None else ""))
            cm.vix = str(cross.vix) if cross.vix is not None else ""
            cm.data_freshness = cross.fetched_at
            cm.interpretation = cross.interpretation_zh()
            from app.llm.usage import spent_today
            result.meta.llm_cost_usd_today = spent_today()
            if result.ai_strategy.available:
                result.meta.model_version = f"rules+{result.ai_strategy.model}"
    except Exception:
        logger.exception("ai strategy layer failed")

    # ── 10. 儲存 ──
    try:
        with db_session() as db:
            from app.services.outcome_tracker import backfill_outcomes
            backfill_outcomes(db, now=now, current_price=tick.mid)
            run = AnalysisRun(
                run_time=now, trigger=trigger, market_state=state,
                decision_action=result.decision.action,
                confidence_grade=result.decision.confidence_grade,
                evidence_score=result.decision.evidence_score,
                data_quality_status=quality.status,
                result_json={},
                prompt_version=PROMPT_VERSION, strategy_version=STRATEGY_VERSION,
                model_version="rule-engine-only")
            db.add(run)
            db.flush()
            # BUGFIX R6:版本號 = analysis_runs.id(單調遞增、跨重啟持續)
            result.version = run.id
            run.result_json = result.model_dump()
            for lv in levels:
                db.add(CandidateLevelRow(analysis_run_id=run.id, level_id=lv.level_id,
                                         kind=lv.kind, price_low=lv.price_low,
                                         price_high=lv.price_high, strength=lv.strength,
                                         source=" + ".join(lv.sources)[:255], created_at=now))
            # 結構事件持久化(Dashboard 圖表標記用;以 tf+type+time 去重)
            from sqlalchemy import select as sa_select
            for tf, rep in structures.items():
                for structure_event in rep.events[-10:]:
                    row = db.execute(sa_select(MarketStructure).where(
                        MarketStructure.timeframe == tf,
                        MarketStructure.event_type == structure_event.event_type,
                        MarketStructure.event_time == structure_event.time,
                    )).scalar_one_or_none()
                    if row is None:
                        db.add(MarketStructure(
                            symbol=symbol, timeframe=tf, event_type=structure_event.event_type,
                            event_time=structure_event.time, price=structure_event.price,
                            confirming_candles=[t.isoformat() for t in structure_event.confirming_candles],
                            invalidation_price=structure_event.invalidation_price,
                            still_valid=structure_event.still_valid, created_at=now))
                    elif row.still_valid != structure_event.still_valid:
                        row.still_valid = structure_event.still_valid
    except Exception as exc:  # noqa: BLE001
        logger.error("persist analysis failed: %s", exc)

    return result
