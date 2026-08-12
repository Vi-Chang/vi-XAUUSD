"""多週期四維度判斷與去相關化評分；不產生第二套交易結論。"""
from __future__ import annotations

from typing import Literal, cast

import pandas as pd

from app.config import get_settings
from app.engines.market_structure import StructureReport
from app.schemas.analysis import DynamicConfirmationLevel, TimeframeAssessment

MarketRegime = Literal["strong_bullish", "bullish", "range", "bearish", "strong_bearish"]
Momentum = Literal["accelerating", "stable", "weakening", "pullback", "reversal_risk"]
SupportState = Literal["none", "testing_support", "intrabar_breach", "confirmed_breakdown",
                       "failed_breakdown", "retest_rejected"]


def _sign(value: float | None, neutral: float = 0.0) -> float | None:
    if value is None:
        return None
    if value > neutral:
        return 1.0
    if value < neutral:
        return -1.0
    return 0.0


def _family_scores(rep: StructureReport | None, ind: dict) -> dict[str, float]:
    """每個證據家族只產生一個 [-1, 1] 分數，相關指標不得重複投票。"""
    scores: dict[str, float] = {}
    if rep:
        scores["structure"] = {"UP": 1.0, "DOWN": -1.0}.get(rep.trend, 0.0)
    emas = [ind.get(f"ema{x}") for x in (20, 50, 200)]
    if all(x is not None for x in emas):
        e20, e50, e200 = (cast(float, x) for x in emas)
        scores["trend"] = 1.0 if e20 > e50 > e200 else \
            -1.0 if e20 < e50 < e200 else 0.0
    hist = ind.get("macd_hist")
    prev_hist = ind.get("macd_hist_prev")
    if hist is not None:
        direction = _sign(hist) or 0.0
        if prev_hist is not None and abs(hist) < abs(prev_hist):
            direction *= 0.55
        scores["momentum"] = direction
    oscillator_parts = [x for x in (
        _sign(ind.get("rsi14"), 50.0), _sign(ind.get("stoch_k"), 50.0),
    ) if x is not None]
    if oscillator_parts:
        scores["oscillator"] = sum(oscillator_parts) / len(oscillator_parts)
    adx = ind.get("adx")
    if adx is not None:
        # 波動／趨勢強度不投方向票，只縮放既有結構方向。
        base = scores.get("structure", 0.0)
        scores["volatility"] = base * min(1.0, max(0.0, (adx - 15.0) / 20.0))
    return scores


def _family_total(scores: dict[str, float]) -> float:
    weights = get_settings().evidence_family_weights
    available = [(scores[k], weights[k]) for k in weights if k in scores]
    total_w = sum(w for _, w in available)
    return sum(v * w for v, w in available) / total_w if total_w else 0.0


def _tf_momentum(ind: dict, trend: str) -> Momentum:
    hist, prev = ind.get("macd_hist"), ind.get("macd_hist_prev")
    rsi, rsi_prev = ind.get("rsi14"), ind.get("rsi14_prev")
    if hist is None:
        return "stable"
    opposing = (trend == "bullish" and hist < 0) or (trend == "bearish" and hist > 0)
    if opposing and (rsi is not None and ((trend == "bullish" and rsi < 50)
                                           or (trend == "bearish" and rsi > 50))):
        return "pullback"
    cooling = prev is not None and abs(hist) < abs(prev) * get_settings().momentum_weakening_ratio
    rsi_cooling = rsi is not None and rsi_prev is not None and (
        (trend == "bullish" and rsi < rsi_prev) or (trend == "bearish" and rsi > rsi_prev))
    if cooling or rsi_cooling:
        return "weakening"
    if prev is not None and abs(hist) > abs(prev) and not opposing:
        return "accelerating"
    return "stable"


def assess_timeframes(structures: dict[str, StructureReport], indicators: dict,
                      closed_times: dict[str, str]) -> list[TimeframeAssessment]:
    out: list[TimeframeAssessment] = []
    for tf in ("1D", "4H", "1H", "15M"):
        rep, ind = structures.get(tf), indicators.get(tf, {})
        scores = _family_scores(rep, ind)
        total = _family_total(scores)
        trend: Literal["bullish", "bearish", "neutral"] = (
            "bullish" if total > .15 else "bearish" if total < -.15 else "neutral")
        momentum = _tf_momentum(ind, trend)
        if tf == "15M" and momentum == "pullback":
            label = "回調／支撐測試"
        elif tf == "1H" and momentum in ("weakening", "pullback"):
            label = f"{'多頭' if trend == 'bullish' else '空頭'}動能降溫"
        else:
            label = {"bullish": "多頭", "bearish": "空頭", "neutral": "盤整"}[trend]
        out.append(TimeframeAssessment(timeframe=tf, trend=trend, momentum=momentum,
            label=label, familyScores={k: round(v, 3) for k, v in scores.items()},
            closedCandleTime=closed_times.get(tf, "")))
    return out


def regime_of(assessments: list[TimeframeAssessment]) -> tuple[MarketRegime, int, float]:
    weights: dict[str, float] = get_settings().regime_weights
    values: dict[str, float] = {x.timeframe: _family_total(x.familyScores) for x in assessments}
    score = sum(values.get(tf, 0.0) * w for tf, w in weights.items())
    s = get_settings()
    if score >= s.regime_strong_threshold:
        regime: MarketRegime = "strong_bullish"
    elif score >= s.regime_direction_threshold:
        regime = "bullish"
    elif score <= -s.regime_strong_threshold:
        regime = "strong_bearish"
    elif score <= -s.regime_direction_threshold:
        regime = "bearish"
    else:
        regime = "range"
    # 技術傾向刻意保留 10% 不確定帶，避免被誤讀成 100% 勝率。
    return regime, max(10, min(90, round(50 + score * 50))), score


def support_state(m15: StructureReport | None, all_df: pd.DataFrame | None,
                  closed_df: pd.DataFrame | None, atr15: float, price: float
                  ) -> tuple[SupportState, list[DynamicConfirmationLevel]]:
    if not m15 or m15.last_swing_low is None:
        return "none", []
    s = get_settings()
    support = float(m15.last_swing_low)
    buffer = max(atr15 * s.structure_atr_buffer, price * s.structure_price_buffer_pct)
    levels = [DynamicConfirmationLevel(kind="support", price=support, buffer=buffer,
        timeframe="15M", source="最近已確認 swing low + ATR／價格比例緩衝")]
    if m15.last_swing_high is not None:
        levels.append(DynamicConfirmationLevel(kind="resistance", price=float(m15.last_swing_high),
            buffer=buffer, timeframe="15M", source="最近已確認 swing high + ATR／價格比例緩衝"))
    if closed_df is None or closed_df.empty:
        return "none", levels
    closes = [float(x) for x in closed_df["close"].iloc[-3:]]
    threshold = support - buffer
    if all_df is not None and len(all_df):
        live = all_df.iloc[-1]
        if not bool(live.get("is_closed", True)) and float(live["low"]) < threshold and closes[-1] >= threshold:
            return "intrabar_breach", levels
    if len(closes) >= 2 and closes[-2] < threshold and closes[-1] > support:
        return "failed_breakdown", levels
    if closes[-1] < threshold:
        if len(closes) >= 2 and closes[-2] < threshold:
            latest = closed_df.iloc[-1]
            if float(latest["high"]) >= support - atr15 * s.structure_retest_atr:
                return "retest_rejected", levels
        return "confirmed_breakdown", levels
    if abs(closes[-1] - support) <= buffer:
        return "testing_support", levels
    return "none", levels


def dimensions(*, structures: dict[str, StructureReport], indicators: dict,
               closed_times: dict[str, str], m15_all: pd.DataFrame | None,
               m15_closed: pd.DataFrame | None, atr15: float, price: float,
               market_status: str, event_status: str, chase_flags: list[str]) -> dict:
    assessments = assess_timeframes(structures, indicators, closed_times)
    regime, trend_score, regime_signed = regime_of(assessments)
    support, levels = support_state(structures.get("15M"), m15_all, m15_closed, atr15, price)
    by_tf = {x.timeframe: x for x in assessments}
    m1, m15 = by_tf.get("1H"), by_tf.get("15M")
    if support in ("confirmed_breakdown", "retest_rejected"):
        momentum: Momentum = "reversal_risk" if regime in ("bullish", "strong_bullish") else "accelerating"
    elif m15 and m15.momentum == "pullback" and regime in ("bullish", "strong_bullish"):
        momentum = "pullback"
    elif m1 and m1.momentum == "weakening" or m15 and m15.momentum == "weakening":
        momentum = "weakening"
    elif m1 and m15 and m1.momentum == m15.momentum == "accelerating":
        momentum = "accelerating"
    else:
        momentum = "stable"
    weights = get_settings().entry_weights
    entry_signed = sum(_family_total(x.familyScores) * weights[x.timeframe] for x in assessments)
    entry_score = round(max(0, min(100, 50 + entry_signed * 50)))
    if market_status in ("FAILED", "STALE"):
        confidence, readiness = "insufficient", "no_trade"
    else:
        confidence = "high" if market_status == "GOOD" and event_status == "GOOD" else \
            "medium" if market_status == "GOOD" and event_status == "STALE" else "low"
        if support in ("intrabar_breach", "testing_support", "confirmed_breakdown",
                       "failed_breakdown", "retest_rejected") or momentum in ("pullback", "weakening", "reversal_risk"):
            readiness = "wait_confirmation"
        elif chase_flags:
            readiness = "avoid_chasing"
        elif abs(entry_signed) >= get_settings().entry_ready_threshold and confidence != "low":
            readiness = "ready"
        else:
            readiness = "wait_confirmation"
    bias = "偏多" if regime_signed > .15 else "偏空" if regime_signed < -.15 else "中性"
    return {"assessments": assessments, "marketRegime": regime, "trendScore": trend_score,
            "shortTermMomentum": momentum, "supportState": support,
            "entryQualityScore": entry_score, "entryReadiness": readiness,
            "dataConfidence": confidence, "technicalBiasLabel": bias,
            "confirmationLevels": levels}
