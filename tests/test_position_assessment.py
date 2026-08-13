"""持倉風險 T0–T5 回放；每一步只傳入截至當時的快照。"""
from __future__ import annotations

import pandas as pd

from app.engines.position_assessment import (
    PositionContext,
    assess_trading_decision,
    classify_reversal_state,
)


def bars(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def decision(*, weakness="none", oversold=False, reversal="none", context=None,
             invalidation=False):
    return assess_trading_decision(
        market_regime="bullish", weakness=weakness, oversold=oversold,
        reversal_state=reversal, readiness="no_trade" if weakness != "none" else "ready",
        long_allowed=weakness == "none", short_allowed=False,
        position_risk="elevated" if weakness != "none" else "normal",
        context=context, invalidation_confirmed=invalidation)


def test_t0_entry_keeps_new_and_existing_decisions_separate():
    out = decision()
    assert out.newEntryDecision.longAllowed is True
    assert out.existingPositionAssessment.action == "insufficient_context"


def test_t1_4413_weakness_does_not_exit_existing_long():
    out = decision(weakness="early_warning")
    assert out.newEntryDecision.longAllowed is False
    assert out.existingPositionAssessment.action == "insufficient_context"


def test_t2_4406_is_high_whipsaw_without_exit_conclusion():
    snapshot = bars([
        (4420, 4422, 4410, 4413),
        (4413, 4415, 4404, 4406),
        (4406, 4408, 4399, 4401),
    ])
    reversal = classify_reversal_state(
        m15_closed=snapshot, indicators={"macd_hist": -2.51, "macd_hist_prev": -1.20},
        support_state="confirmed_breakdown", oversold=True)
    out = decision(weakness="accelerating", oversold=True, reversal=reversal)
    assert out.marketAssessment.twoSidedRisk == "high_whipsaw"
    assert out.marketAssessment.reversalState == "oversold_without_reversal"
    assert out.newEntryDecision.longAllowed is False
    assert out.newEntryDecision.shortAllowed is False
    assert out.existingPositionAssessment.action == "insufficient_context"


def test_t3_4398_can_only_be_exhaustion_candidate():
    snapshot = bars([
        (4410, 4411, 4400, 4402),
        (4402, 4404, 4398.38, 4399.5),
        (4399.5, 4404, 4398.5, 4402.5),
    ])
    state = classify_reversal_state(
        m15_closed=snapshot, indicators={"macd_hist": -1.2, "macd_hist_prev": -2.0},
        support_state="testing_support", oversold=True)
    assert state == "selling_exhaustion_candidate"


def test_t4_reclaim_requires_sequential_closed_bar_confirmation():
    snapshot = bars([
        (4399, 4408, 4398, 4406),
        (4406, 4415, 4405, 4413),
        (4413, 4422, 4410, 4420),
    ])
    attempt = classify_reversal_state(
        m15_closed=snapshot.iloc[:2], indicators={"macd_hist": -0.8, "macd_hist_prev": -1.2},
        support_state="failed_breakdown", oversold=True)
    confirmed = classify_reversal_state(
        m15_closed=snapshot, indicators={"macd_hist": -0.3, "macd_hist_prev": -0.8},
        support_state="none", oversold=True)
    assert attempt == "reclaim_attempt"
    assert confirmed == "reversal_confirmed"


def test_t5_4449_does_not_make_new_long_automatically_ready():
    out = assess_trading_decision(
        market_regime="bullish", weakness="none", oversold=False,
        reversal_state="reversal_confirmed", readiness="avoid_chasing",
        long_allowed=False, short_allowed=False, position_risk="normal")
    assert out.existingPositionAssessment.action == "insufficient_context"
    assert out.newEntryDecision.longAllowed is False


def test_accelerating_weakness_with_complete_context_only_monitors_reclaim():
    ctx = PositionContext(direction="long", entry_price=4429.13, size=0.1,
                          timeframe="1H", original_stop=4380.0,
                          thesis="1H higher-low trend", allow_event_hold=False)
    out = decision(weakness="accelerating", oversold=True, context=ctx)
    assert out.existingPositionAssessment.action == "monitor_reclaim"
    assert out.existingPositionAssessment.thesisStatus == "under_pressure"


def test_exit_confirmed_requires_complete_context_and_confirmed_invalidation():
    incomplete = decision(weakness="accelerating", oversold=True, invalidation=True)
    assert incomplete.existingPositionAssessment.action == "insufficient_context"
    ctx = PositionContext(direction="long", entry_price=4429.13, size=0.1,
                          timeframe="1H", original_stop=4380.0,
                          thesis="1H higher-low trend", allow_event_hold=False)
    complete = decision(weakness="accelerating", oversold=True, context=ctx,
                        invalidation=True)
    assert complete.existingPositionAssessment.action == "exit_confirmed"
    assert complete.existingPositionAssessment.thesisStatus == "invalidated"
