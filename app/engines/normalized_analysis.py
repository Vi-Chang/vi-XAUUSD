"""單一、可驗證的市場分析狀態。UI 與相容欄位都只能由此衍生。"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Literal, cast

import pandas as pd

from app.engines.market_dimensions import dimensions
from app.engines.market_structure import StructureEvent, StructureReport
from app.engines.position_assessment import (
    assess_trading_decision,
    classify_reversal_state,
)
from app.engines.risk_override import apply_risk_priority, detect_short_term_weakness
from app.i18n import state_zh
from app.schemas.analysis import AnalysisEvidence, NormalizedAnalysisState

WAIT_MESSAGE = "訊號尚未一致，等待下一根 15 分 K 收盤確認。"
logger = logging.getLogger(__name__)
Direction = Literal["bullish", "bearish"]
TrendBias = Literal["bullish", "bearish", "neutral"]
BreakoutState = Literal["confirmed", "testing", "failed", "none"]
EntryTiming = Literal["favorable", "chase", "wait", "invalid"]
RiskDirection = Literal["long", "short", "both", "wait", "none"]
QualityStatus = Literal["GOOD", "STALE", "FAILED"]
EventRiskValue = Literal["low", "medium", "high", "unknown"]


def _item(text: str, direction: Direction, index: int) -> AnalysisEvidence:
    prefix, _, label = text.partition(":")
    return AnalysisEvidence(id=f"{direction}-{index}-{abs(hash(text))}", direction=direction,
                            category=prefix or "OTHER", label=label or text)


def _event_status(source: str, manual_stale: bool) -> QualityStatus:
    if source == "none":
        return "FAILED"
    return "STALE" if manual_stale else "GOOD"


def _market_status(status: str) -> QualityStatus:
    if status == "GOOD":
        return "GOOD"
    if status in ("DEGRADED", "STALE"):
        return "STALE"
    return "FAILED"


def _trend_bias(structures: dict[str, StructureReport]) -> TrendBias:
    for tf in ("1H", "4H", "1D"):
        report = structures.get(tf)
        trend = report.trend if report else "UNKNOWN"
        if trend == "UP":
            return "bullish"
        if trend == "DOWN":
            return "bearish"
    return "neutral"


def _recent(events: Iterable[StructureEvent], kinds: tuple[str, ...]) -> StructureEvent | None:
    matches = [e for e in events if e.still_valid and e.event_type in kinds]
    return max(matches, key=lambda e: e.time) if matches else None


def _breakout(m15: StructureReport | None, m15_all: pd.DataFrame | None,
              m15_closed: pd.DataFrame | None) -> tuple[BreakoutState, StructureEvent | None]:
    if not m15:
        return "none", None
    failed = _recent(m15.events, ("FAILED_BREAKOUT", "FAILED_BREAKDOWN"))
    directional = _recent(m15.events, ("BOS_UP", "CHOCH_UP", "BOS_DOWN", "CHOCH_DOWN"))
    if failed and (not directional or failed.time >= directional.time):
        return "failed", failed
    if m15_all is not None and len(m15_all):
        last = m15_all.iloc[-1]
        if not bool(last.get("is_closed", True)):
            close = float(last["close"])
            if (m15.range_high is not None and close > m15.range_high) or \
                    (m15.range_low is not None and close < m15.range_low):
                return "testing", directional
    if directional and not directional.provisional and m15_closed is not None and len(m15_closed):
        close = float(m15_closed.iloc[-1]["close"])
        holds = close > directional.price if directional.event_type.endswith("_UP") else close < directional.price
        if holds:
            return "confirmed", directional
    return "none", directional


def validate_consistency(state: NormalizedAnalysisState) -> NormalizedAnalysisState:
    errors: list[str] = []
    active = state.longEvidence + state.shortEvidence
    breakout_labels = [x for x in state.longEvidence if x.sourceEvent in ("BOS_UP", "CHOCH_UP")]
    if state.breakoutState == "failed" and breakout_labels:
        errors.append("failed breakout 仍含 confirmed breakout 證據")
    if state.evidenceTotal != len(active):
        errors.append("證據分母與展開數量不一致")
    if state.bullPct != state.trendScore or state.bearPct != 100 - state.trendScore:
        errors.append("技術傾向與去相關化評分不一致")
    direction_text = {"long": "追多", "short": "追空"}.get(state.riskDirection)
    if direction_text and direction_text not in state.riskLabel:
        errors.append("risk label 與 riskDirection 不一致")
    if state.riskDirection == "long" and "追空" in state.riskMessage:
        errors.append("risk label 與風險內文方向不一致")
    if state.riskDirection == "short" and "追多" in state.riskMessage:
        errors.append("risk label 與風險內文方向不一致")
    if not state.marketDataTimestamp:
        errors.append("缺少行情資料時間")
    if state.sourceTimestamps and set(state.sourceTimestamps.values()) != {state.marketDataTimestamp}:
        errors.append("各區塊使用不同 timestamp")
    if state.sourcePrices and set(state.sourcePrices.values()) != {state.currentPrice}:
        errors.append("各區塊使用不同 currentPrice")
    if state.marketStateCode == "FAILED_BREAKOUT" and "可追多" in state.tradingScript:
        errors.append("市場狀態與交易建議方向直接相反")
    if state.marketStateCode == "FAILED_BREAKDOWN" and "可追空" in state.tradingScript:
        errors.append("市場狀態與交易建議方向直接相反")
    if state.shortTermWeakness in ("confirmed", "accelerating") and state.longEntryAllowed:
        errors.append("短線確認轉弱卻允許新多單")
    if state.riskOverride == "protect_existing_long" and any(
            text in state.tradingScript for text in ("強烈做多", "立即做多", "放心持有")):
        errors.append("保護既有多單時仍顯示強烈做多")
    if state.eventDataStatus == "FAILED" and state.dataConfidence == "high":
        errors.append("事件資料失效但資料可信度為 high")
    if state.entryReadiness == "no_trade" and state.entryTiming == "favorable":
        errors.append("no_trade 卻標示可進場")
    if state.supportState in ("confirmed_breakdown", "retest_rejected") \
            and not state.lastClosedCandleTimestamp:
        errors.append("沒有已收盤 K 棒時間卻標示有效跌破")
    position = state.tradingDecision.existingPositionAssessment
    market = state.tradingDecision.marketAssessment
    new_entry = state.tradingDecision.newEntryDecision
    if position.positionTimeframe == "unknown" and position.action == "exit_confirmed":
        errors.append("持倉週期未知卻輸出 exit_confirmed")
    if not position.contextComplete and position.action != "insufficient_context":
        errors.append("持倉背景不足卻輸出個人化處置")
    if market.twoSidedRisk == "high_whipsaw" and not any(
            "急彈" in text for text in position.warnings + [state.tradingScript]):
        errors.append("超賣且空方動能擴大卻未揭露急彈風險")
    if not new_entry.longAllowed and position.action == "exit_confirmed" \
            and position.thesisStatus != "invalidated":
        errors.append("禁止新多被誤譯為既有多單退出")
    if errors:
        return state.model_copy(update={"entryTiming": "wait", "riskDirection": "wait",
            "riskLabel": "等待確認", "riskMessage": WAIT_MESSAGE,
            "tradingScript": WAIT_MESSAGE, "mostLikelyMistake": "在訊號矛盾時搶先進場。",
            "entryReadiness": "no_trade", "riskOverride": "suspend_all_entries",
            "longEntryAllowed": False, "shortEntryAllowed": False,
            "consistencyValid": False, "consistencyErrors": errors,
            "consistencyMessage": WAIT_MESSAGE})
    return state.model_copy(update={"consistencyValid": True, "consistencyErrors": [],
                                    "consistencyMessage": ""})


def validate_api_payload(payload: dict, *, strict: bool = False) -> dict:
    """API 最後一道防線：加入頂層快照指紋後重跑 validator。"""
    raw = payload.get("normalized_analysis")
    if not isinstance(raw, dict):
        return payload
    state = NormalizedAnalysisState.model_validate(raw)
    timestamps = dict(state.sourceTimestamps)
    prices = dict(state.sourcePrices)
    timestamps["api"] = str(payload.get("snapshot_ts") or "")
    top_price = payload.get("current_price") or {}
    if top_price.get("mid") is not None:
        prices["api"] = float(top_price["mid"])
    checked = validate_consistency(state.model_copy(update={
        "sourceTimestamps": timestamps, "sourcePrices": prices}))
    api_errors = list(checked.consistencyErrors)
    for key in ("long_scenario", "short_scenario"):
        sc = payload.get(key) or {}
        if checked.entryReadiness == "no_trade" and any(sc.get(x) for x in
                ("entry_zone_id", "stop_loss_id", "target_ids")):
            api_errors.append("no_trade 卻產生立即進場或停損價")
    if api_errors and strict:
        raise ValueError("ANALYSIS_CONSISTENCY_ERROR:" + ";".join(api_errors))
    if api_errors:
        logger.error("ANALYSIS_CONSISTENCY_ERROR errors=%s version=%s",
                     api_errors, payload.get("version"))
        checked = checked.model_copy(update={"entryTiming": "wait",
            "entryReadiness": "no_trade", "riskOverride": "suspend_all_entries",
            "longEntryAllowed": False, "shortEntryAllowed": False,
            "tradingScript": WAIT_MESSAGE, "consistencyValid": False,
            "consistencyErrors": api_errors, "consistencyMessage": WAIT_MESSAGE})
    out = dict(payload)
    out["normalized_analysis"] = checked.model_dump()
    if not checked.consistencyValid:
        decision = dict(out.get("market_decision") or out.get("decision") or {})
        decision.update({"action": "WATCH", "reason": WAIT_MESSAGE})
        out["market_decision"] = decision
        out["summary_zh_tw"] = WAIT_MESSAGE
        out["most_likely_user_mistake_now"] = checked.mostLikelyMistake
    return out


def build_normalized_state(*, generated_at: str, market_timestamp: str, current_price: float,
                           market_state: str, market_quality: str, event_source: str,
                           event_stale: bool, structures: dict[str, StructureReport],
                           m15_all: pd.DataFrame | None, m15_closed: pd.DataFrame | None,
                           bull_evidence: list[str], bear_evidence: list[str],
                           chase_flags: list[str], indicators: dict | None = None,
                           closed_times: dict[str, str] | None = None,
                           atr15: float = 0.0, event_timestamp: str = "",
                           event_risk: str = "unknown", event_lockout: bool = False) -> NormalizedAnalysisState:
    indicators = indicators or {}
    closed_times = closed_times or {}
    market_status = _market_status(market_quality)
    event_status = _event_status(event_source, event_stale)
    dims = dimensions(structures=structures, indicators=indicators,
        closed_times=closed_times, m15_all=m15_all, m15_closed=m15_closed,
        atr15=atr15, price=current_price, market_status=market_status,
        event_status=event_status, chase_flags=chase_flags)
    weakness = detect_short_term_weakness(
        indicators=indicators, support_state=dims["supportState"])
    priority = apply_risk_priority(weakness=weakness, market_status=market_status,
        event_status=event_status, event_lockout=event_lockout,
        market_regime=dims["marketRegime"], entry_readiness=dims["entryReadiness"],
        support_state=dims["supportState"], levels=dims["confirmationLevels"])
    reversal_state = classify_reversal_state(
        m15_closed=m15_closed, indicators=indicators.get("15M", {}),
        support_state=dims["supportState"], oversold=weakness.oversold)
    trading_decision = assess_trading_decision(
        market_regime=dims["marketRegime"], weakness=weakness.state,
        oversold=weakness.oversold, reversal_state=reversal_state,
        readiness=priority["entryReadiness"], long_allowed=priority["longEntryAllowed"],
        short_allowed=priority["shortEntryAllowed"], position_risk=priority["positionRisk"])
    dims["entryReadiness"] = priority["entryReadiness"]
    trend: TrendBias = ("bullish" if dims["marketRegime"] in ("bullish", "strong_bullish") else
                        "bearish" if dims["marketRegime"] in ("bearish", "strong_bearish") else "neutral")
    breakout, ev = _breakout(structures.get("15M"), m15_all, m15_closed)
    longs = [_item(x, "bullish", i) for i, x in enumerate(bull_evidence)]
    shorts = [_item(x, "bearish", i) for i, x in enumerate(bear_evidence)]
    invalid: list[AnalysisEvidence] = []
    if breakout == "failed" and ev:
        failed_up = ev.event_type == "FAILED_BREAKOUT"
        removed = [x for x in longs if failed_up and "順勢突破" in x.label]
        removed += [x for x in shorts if not failed_up and "順勢跌破" in x.label]
        longs = [x for x in longs if x not in removed]
        shorts = [x for x in shorts if x not in removed]
        for i, old in enumerate(removed or [None]):
            invalid.append(AnalysisEvidence(
                id=f"invalid-{i}-{ev.time.isoformat()}",
                direction="bullish" if failed_up else "bearish", category="STRUCT",
                label=(old.label if old else ("15分K順勢突破" if failed_up else "15分K順勢跌破")),
                sourceEvent=ev.event_type, level=ev.price, candleTime=ev.time.isoformat(),
                reason=f"15 分 K 已收回 {'突破位下方' if failed_up else '跌破位上方'}，原證據失效"))
        failure_text = f"15分K假{'突破' if failed_up else '跌破'}：收盤回到 {ev.price:.2f} {'下方' if failed_up else '上方'}"
        target = shorts if failed_up else longs
        if not any("假突破" in x.label or "假跌破" in x.label for x in target):
            target.append(AnalysisEvidence(id=f"failure-{ev.time.isoformat()}",
                direction="bearish" if failed_up else "bullish", category="STRUCT",
                label=failure_text, sourceEvent=ev.event_type, level=ev.price,
                candleTime=ev.time.isoformat(), reason="最新有效已收盤 15 分 K 確認原突破失效"))
    if breakout == "confirmed" and ev:
        target = longs if ev.event_type.endswith("_UP") else shorts
        for item in target:
            if ("順勢突破" in item.label or "順勢跌破" in item.label):
                item.sourceEvent, item.level, item.candleTime = ev.event_type, ev.price, ev.time.isoformat()
    total = len(longs) + len(shorts)
    entry: EntryTiming = "wait"
    risk: RiskDirection = "none"
    if dims["entryReadiness"] == "no_trade":
        entry = "invalid"
    elif breakout in ("failed", "testing"):
        entry, risk = "wait", "wait"
    elif (any(x.startswith("CHASE_LONG_RISK") for x in chase_flags)
          and any(x.startswith("CHASE_SHORT_RISK") for x in chase_flags)):
        entry, risk = "chase", "both"
    elif trend == "bullish" and any(x.startswith("CHASE_LONG_RISK") for x in chase_flags):
        entry, risk = "chase", "long"
    elif trend == "bearish" and any(x.startswith("CHASE_SHORT_RISK") for x in chase_flags):
        entry, risk = "chase", "short"
    elif dims["entryReadiness"] == "ready":
        entry = "favorable"
    labels = {"long": "追多風險", "short": "追空風險", "both": "雙向追價風險",
              "wait": "等待確認", "none": "位置風險正常"}
    if risk == "long":
        risk_msg = "中期劇本偏多，但目前位置過高，不宜追多。"
        mistake = "在偏多劇本的高位追多，忽略停損與回踩確認。"
    elif risk == "short":
        risk_msg = "中期劇本偏空，但目前位置過低，不宜追空。"
        mistake = "在偏空劇本的低位追空，忽略反彈與確認。"
    elif breakout == "failed":
        risk_msg = "突破已失敗，方向可能快速切換，等待下一根 15 分 K 收盤確認。"
        mistake = "把已失效的突破當成仍有效，急著追價。"
    else:
        risk_msg = "目前沒有明確追價警示，仍須等待劇本觸發。"
        mistake = "在條件尚未齊全前搶先進場。"
    support_level = next((x for x in dims["confirmationLevels"] if x.kind == "support"), None)
    resistance_level = next((x for x in dims["confirmationLevels"] if x.kind == "resistance"), None)
    from app.config import get_settings
    from app.engines.tactical_setup import classify_tactical_setup
    next_support = None
    rr_to_next_support = None
    if support_level and structures.get("15M"):
        lower_lows = [float(x.price) for x in structures["15M"].swings
                      if x.kind == "SWING_LOW" and x.price < support_level.price]
        next_support = max(lower_lows) if lower_lows else None
        invalidation = support_level.price + support_level.buffer
        risk_distance = invalidation - current_price
        if next_support is not None and risk_distance > 0 and current_price > next_support:
            rr_to_next_support = (current_price - next_support) / risk_distance
    bullish_breakout_active = bool(
        breakout == "confirmed" and ev and ev.event_type.endswith("_UP"))
    tactical = classify_tactical_setup(
        support_state=dims["supportState"], weakness_state=weakness.state,
        weakness_families=weakness.families, trend_bias=trend,
        current_price=current_price,
        support=support_level.price if support_level else None,
        buffer=support_level.buffer if support_level else 0.0,
        atr15=atr15, last_closed_at=closed_times.get("15M", ""),
        rr_to_next_support=rr_to_next_support,
        bullish_breakout_active=bullish_breakout_active,
        retest_failed=dims["supportState"] == "retest_rejected",
        chase_atr_mult=get_settings().tactical_short_chase_atr_mult,
        min_rr=get_settings().tactical_min_rr,
        expiry_bars=get_settings().tactical_setup_expiry_bars)
    if tactical.setup_state == "SHORT_READY":
        dims["entryReadiness"] = "ready"
        priority["entryReadiness"] = "ready"
        priority["shortEntryAllowed"] = True
        priority["longEntryAllowed"] = False
        entry, risk = "favorable", "short"
    elif tactical.setup_state == "SHORT_WATCH":
        priority["shortEntryAllowed"] = False
        entry, risk = "wait", "short"
    elif tactical.setup_state == "NO_CHASE":
        dims["entryReadiness"] = "avoid_chasing"
        priority["entryReadiness"] = "avoid_chasing"
        priority["shortEntryAllowed"] = False
        entry, risk = "chase", "short"
    trading_decision = assess_trading_decision(
        market_regime=dims["marketRegime"], weakness=weakness.state,
        oversold=weakness.oversold, reversal_state=reversal_state,
        readiness=priority["entryReadiness"], long_allowed=priority["longEntryAllowed"],
        short_allowed=priority["shortEntryAllowed"], position_risk=priority["positionRisk"])
    support_text = (f"15M swing 支撐 {support_level.price:.2f}（緩衝 {support_level.buffer:.2f}）"
                    if support_level else "15M 動態支撐")
    resistance_text = (f"15M swing 壓力 {resistance_level.price:.2f}（緩衝 {resistance_level.buffer:.2f}）"
                       if resistance_level else "15M 動態壓力")
    if weakness.state == "accelerating":
        if weakness.oversold:
            script = ("大週期偏多，但短線明顯轉弱。新多單暫停；短線空方仍占優勢，"
                      "但已進入超賣區，續跌與急彈風險並存。此處不適合追空，也不能只憑超賣搶多。")
        else:
            script = ("短線空方動能擴大。暫停新多單並等待已收盤 K 棒形成止跌與收復結構。"
                      "既有持倉須依原始交易週期與停損計畫判斷，不自動推導平倉。")
        mistake = "把暫停新多單誤當成既有多單必須平倉，或只憑超賣搶反彈。"
    elif weakness.state in ("confirmed", "early_warning") and dims["marketRegime"] in ("bullish", "strong_bullish"):
        script = "大週期偏多，但短線已轉弱；暫停新多單。若已有多單，優先處理風險。"
        mistake = "把大週期偏多誤認為多單可以放心續抱或繼續追多。"
    elif dims["marketRegime"] in ("bullish", "strong_bullish") and dims["shortTermMomentum"] in ("pullback", "weakening"):
        script = (f"大週期維持多頭，但短線正在回調。重新站回{resistance_text}且收盤確認，"
                  f"或{support_text}出現止跌結構後，才評估低風險多單；有效跌破並反抽失敗，"
                  "才啟動短線空方劇本。目前等待。")
        mistake = "把大週期偏多誤認為現在可以直接追多。"
    elif trend == "bullish" and breakout == "failed":
        script = "中期趨勢偏多，但短線突破失敗，目前不適合追多，等待重新站回確認位。"
    elif trend == "bearish" and breakout == "failed":
        script = "中期趨勢偏空，但短線跌破失敗，目前不適合追空，等待重新跌回確認位。"
    elif breakout == "testing":
        script = "盤中正在測試突破位，K 棒尚未收盤，不視為已突破。"
    elif entry == "favorable":
        script = f"中期趨勢{'偏多' if trend == 'bullish' else '偏空'}，最新收盤 K 棒已確認突破，可依劇本等待進場條件。"
    else:
        script = "方向或時機尚未齊全，等待下一根 15 分 K 收盤確認。"
    normalized_event_risk = cast(EventRiskValue,
        event_risk if event_status != "FAILED" and event_risk in
        ("low", "medium", "high", "unknown") else "unknown")
    state = NormalizedAnalysisState(generatedAt=generated_at,
        marketDataTimestamp=market_timestamp, currentPrice=current_price,
        trendBias=trend, tacticalBias=tactical.tactical_bias,
        setupState=tactical.setup_state, triggerLevel=tactical.trigger_level,
        invalidationLevel=tactical.invalidation_level, expiresAt=tactical.expires_at,
        missingCondition=tactical.missing_condition,
        nextCheckTime=tactical.next_check_time,
        bullishTriggerLevel=resistance_level.price if resistance_level else None,
        bearishTriggerLevel=support_level.price if support_level else None,
        falseBreakProtectionLevel=(support_level.price if support_level and
                                   dims["supportState"] == "failed_breakdown" else None),
        falseBreakProtectionExpiresAt=(tactical.expires_at if
                                       dims["supportState"] == "failed_breakdown" else ""),
        breakoutState=breakout, entryTiming=entry,
        longEvidence=longs, shortEvidence=shorts, invalidatedEvidence=invalid,
        eventDataStatus=event_status,
        marketDataStatus=market_status, bullPct=dims["trendScore"],
        bearPct=100 - dims["trendScore"], evidenceTotal=total, riskDirection=risk,
        riskLabel=labels[risk], riskMessage=risk_msg, marketStateCode=market_state,
        marketStateLabel=state_zh(market_state),
        tradingScript=(tactical.message if tactical.setup_state != "OBSERVE" else script),
        mostLikelyMistake=mistake,
        marketRegime=dims["marketRegime"], shortTermMomentum=dims["shortTermMomentum"],
        entryReadiness=dims["entryReadiness"], dataConfidence=dims["dataConfidence"],
        supportState=dims["supportState"], trendScore=dims["trendScore"],
        entryQualityScore=dims["entryQualityScore"],
        technicalBiasLabel=dims["technicalBiasLabel"],
        timeframeAssessments=dims["assessments"],
        confirmationLevels=dims["confirmationLevels"],
        lastClosedCandleTimestamp=closed_times.get("15M", ""),
        eventDataTimestamp=event_timestamp,
        freshnessBySource={"market": market_status, "events": event_status},
        eventRisk=normalized_event_risk,
        shortTermWeakness=weakness.state,
        positionRisk=priority["positionRisk"], riskOverride=priority["riskOverride"],
        longEntryAllowed=priority["longEntryAllowed"],
        shortEntryAllowed=priority["shortEntryAllowed"], reasons=priority["reasons"],
        invalidationConditions=priority["invalidationConditions"],
        existingLongGuidance=priority["existingLongGuidance"],
        existingShortGuidance=priority["existingShortGuidance"],
        structuralInvalidationNote=("以上僅為 swing、ATR 與已收盤 K 棒推導的結構失效區；"
                                    "不是個人帳戶停損價，未提供持倉與可承受風險時不產生精準停損。"),
        tradingDecision=trading_decision,
        sourceTimestamps={k: market_timestamp for k in
            ("marketState", "evidence", "risk", "tradingScript", "dataQuality")},
        sourcePrices={k: current_price for k in
            ("marketState", "evidence", "risk", "tradingScript")})
    return validate_consistency(state)
