"""新單與既有持倉分離的決策層；只使用當下已知資料。"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.schemas.analysis import (
    AnalysisEvidence,
    ExistingPositionAssessment,
    MarketAssessment,
    NewEntryDecision,
    TradingDecision,
)

MISSING_CONTEXT_MESSAGE = (
    "目前可判斷市場風險，但缺少原始交易週期、交易理由或事件容許設定，"
    "無法僅依短線指標判定應續抱或平倉。"
)


@dataclass(frozen=True)
class PositionContext:
    direction: str = "unknown"
    entry_price: float | None = None
    size: float | None = None
    timeframe: str = "unknown"
    original_stop: float | None = None
    max_loss_usd: float | None = None
    thesis: str = ""
    allow_event_hold: bool | None = None

    @property
    def complete(self) -> bool:
        return bool(
            self.direction in ("long", "short")
            and self.entry_price is not None
            and self.size is not None
            and self.timeframe in ("15M", "1H", "4H", "1D")
            and (self.original_stop is not None or self.max_loss_usd is not None)
            and self.thesis
            and self.allow_event_hold is not None
        )


def classify_two_sided_risk(*, weakness: str, oversold: bool) -> str:
    if oversold and weakness in ("confirmed", "accelerating"):
        return "high_whipsaw"
    if oversold:
        return "oversold_rebound"
    if weakness in ("confirmed", "accelerating"):
        return "downside_continuation"
    return "normal"


def classify_reversal_state(*, m15_closed: pd.DataFrame | None, indicators: dict,
                            support_state: str, oversold: bool) -> str:
    """僅讀傳入快照最後三根已收盤 K；呼叫端不得傳入未來 K 棒。"""
    if m15_closed is None or len(m15_closed) == 0:
        return "oversold_without_reversal" if oversold else "none"
    if support_state == "failed_breakdown":
        return "reclaim_attempt"
    if support_state == "retest_rejected":
        return "reversal_failed"
    if len(m15_closed) < 3:
        return "oversold_without_reversal" if oversold else "none"
    bars = m15_closed.iloc[-3:]
    last, prev = bars.iloc[-1], bars.iloc[-2]
    hist, hist_prev = indicators.get("macd_hist"), indicators.get("macd_hist_prev")
    contracting = (hist is not None and hist_prev is not None and hist < 0 and hist > hist_prev)
    lower_low = float(last["low"]) < float(prev["low"])
    lower_wick = float(last["close"]) - float(last["low"])
    body = abs(float(last["close"]) - float(last["open"]))

    if support_state == "none" and oversold and contracting and not lower_low:
        return "reversal_confirmed"
    if oversold and not lower_low and contracting and lower_wick > body:
        return "selling_exhaustion_candidate"
    if oversold:
        return "oversold_without_reversal"
    return "none"


def assess_trading_decision(*, market_regime: str, weakness: str, oversold: bool,
                            reversal_state: str, readiness: str, long_allowed: bool,
                            short_allowed: bool, position_risk: str,
                            context: PositionContext | None = None,
                            invalidation_confirmed: bool = False) -> TradingDecision:
    two_sided = classify_two_sided_risk(weakness=weakness, oversold=oversold)
    market = MarketAssessment(regime=market_regime, shortTermWeakness=weakness,
                              twoSidedRisk=two_sided, reversalState=reversal_state)
    new_entry = NewEntryDecision(
        readiness=readiness, longAllowed=long_allowed, shortAllowed=short_allowed,
        longReason=("暫停新多單，等待止跌與收復確認。" if not long_allowed else "多方條件已確認。"),
        shortReason=("不宜追空，超賣區存在急彈風險。"
                     if two_sided == "high_whipsaw" else
                     "空方條件尚未確認。" if not short_allowed else "空方條件已確認。"),
    )
    ctx = context or PositionContext()
    warnings = [
        "protect_existing_long 只代表檢查原始停損、倉位與結構失效條件，不是立即平倉訊號。"
    ]
    if two_sided == "high_whipsaw":
        warnings.append("短線續跌與超賣急彈風險並存，不宜追空，也不能只憑超賣搶多。")
    if not ctx.complete:
        existing = ExistingPositionAssessment(
            direction=ctx.direction if ctx.direction in ("long", "short") else "unknown",
            positionTimeframe=(ctx.timeframe if ctx.timeframe in ("15M", "1H", "4H", "1D")
                               else "unknown"),
            riskLevel=position_risk, action="insufficient_context", thesisStatus="unknown",
            warnings=warnings, contextComplete=False, message=MISSING_CONTEXT_MESSAGE)
    else:
        invalidation = [AnalysisEvidence(id="confirmed-position-invalidation", direction="bearish",
            category="STRUCT", label=f"{ctx.timeframe} 交易假設已由已收盤結構確認失效",
            reason="對應持倉週期結構跌破、超過緩衝，且延續或反抽失敗")]
        if invalidation_confirmed:
            action, thesis = "exit_confirmed", "invalidated"
        elif weakness in ("confirmed", "accelerating"):
            action, thesis = "monitor_reclaim", "under_pressure"
        else:
            action, thesis = "follow_original_plan", "intact"
        existing = ExistingPositionAssessment(
            direction=ctx.direction, positionTimeframe=ctx.timeframe, riskLevel=position_risk,
            action=action, thesisStatus=thesis, warnings=warnings,
            invalidationEvidence=invalidation if invalidation_confirmed else [],
            contextComplete=True,
            message=("交易假設已由對應持倉週期確認失效。" if invalidation_confirmed else
                     "依原始交易計畫管理；短線轉弱本身不構成立即退出。"))
    return TradingDecision(marketAssessment=market, newEntryDecision=new_entry,
                           existingPositionAssessment=existing)
