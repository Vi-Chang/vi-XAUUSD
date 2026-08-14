import pandas as pd

from app.schemas.analysis import (
    AnalysisEvidence,
    AnalysisResult,
    CurrentPrice,
    Decision,
    NormalizedAnalysisState,
    Scenario,
)
from app.services.decision_trace import build_decision_trace


def test_trace_uses_same_snapshot_and_only_counts_later_closed_bars():
    scenario = Scenario(
        lifecycle_status="MISSED_ENTRY_WAIT_RETEST", setup_id="XAU-L-abc",
        breakout_at="2026-08-14T12:45:00+00:00", entry_zone_id="E",
        blocking_reasons=["ENTRY_ALREADY_MISSED"],
        resolved_prices={"E": {"price_low": 4368.04, "price_high": 4377.05}},
        rr_details=[{"available": True, "ratio": 2.0}],
    )
    result = AnalysisResult(
        current_price=CurrentPrice(mid=4378), long_scenario=scenario,
        market_decision=Decision(action="WATCH"),
        normalized_analysis=NormalizedAnalysisState(
            marketDataStatus="GOOD",
            longEvidence=[AnalysisEvidence(
                id="x", direction="bullish", category="structure", label="多方")]),
    )
    frame = pd.DataFrame(index=pd.to_datetime([
        "2026-08-14T12:45:00Z", "2026-08-14T13:00:00Z", "2026-08-14T13:15:00Z"]))
    trace = build_decision_trace(
        result, evaluated_at="2026-08-14T13:16:00+00:00",
        market_snapshot_at="2026-08-14T13:15:01+00:00", m15_closed=frame)
    assert trace.setupId == "XAU-L-abc"
    assert trace.closedBarsSinceBreakout == 2
    assert trace.marketSnapshotAt == "2026-08-14T13:15:01+00:00"
    assert trace.blockingReasons == ["ENTRY_ALREADY_MISSED"]
    assert result.long_scenario.closed_bars_since_breakout == 2


def test_pending_breakout_expires_after_confirmation_bar_limit():
    scenario = Scenario(
        lifecycle_status="BREAKOUT_PENDING", setup_id="XAU-L-pending",
        breakout_at="2026-08-14T12:00:00+00:00", entry_zone_id="E",
        blocking_reasons=["BREAKOUT_NOT_CONFIRMED"],
        resolved_prices={"E": {"price_low": 100, "price_high": 101}},
    )
    result = AnalysisResult(
        current_price=CurrentPrice(mid=100.5), long_scenario=scenario,
        market_decision=Decision(action="WATCH"),
        normalized_analysis=NormalizedAnalysisState(
            marketDataStatus="GOOD",
            longEvidence=[AnalysisEvidence(
                id="x", direction="bullish", category="structure", label="多方")]),
    )
    frame = pd.DataFrame(index=pd.date_range(
        "2026-08-14T12:15:00Z", periods=4, freq="15min"))
    trace = build_decision_trace(
        result, evaluated_at="2026-08-14T13:00:00+00:00",
        market_snapshot_at="2026-08-14T13:00:00+00:00", m15_closed=frame)
    assert trace.lifecycleStatus == "EXPIRED"
    assert trace.blockingReasons == ["SETUP_EXPIRED"]
    assert "等待已達上限" in result.market_decision.reason
