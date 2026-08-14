"""Create one auditable decision trace from the same persisted analysis snapshot."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.schemas.analysis import AnalysisResult, DecisionTrace, Scenario


def _closed_bars_since(frame: Any, breakout_at: str) -> int:
    if frame is None or not breakout_at or len(getattr(frame, "index", ())) == 0:
        return 0
    try:
        breakout = datetime.fromisoformat(breakout_at.replace("Z", "+00:00"))
        return sum(index.to_pydatetime() > breakout for index in frame.index)
    except (TypeError, ValueError, AttributeError):
        return 0


def _chosen_scenario(result: AnalysisResult) -> tuple[str, Scenario]:
    action = result.market_decision.action or result.decision.action
    if "LONG" in action:
        return "LONG", result.long_scenario
    if "SHORT" in action:
        return "SHORT", result.short_scenario
    normalized = result.normalized_analysis
    if len(normalized.longEvidence) > len(normalized.shortEvidence):
        return "LONG", result.long_scenario
    if len(normalized.shortEvidence) > len(normalized.longEvidence):
        return "SHORT", result.short_scenario
    return "NONE", Scenario()


def build_decision_trace(
    result: AnalysisResult, *, evaluated_at: str, market_snapshot_at: str,
    m15_closed: Any = None,
) -> DecisionTrace:
    direction, scenario = _chosen_scenario(result)
    prices = scenario.resolved_prices or {}
    entry = prices.get(scenario.entry_zone_id) or {}
    current = result.current_price.mid
    inside = (isinstance(current, (int, float))
              and isinstance(entry.get("price_low"), (int, float))
              and entry["price_low"] <= current <= entry["price_high"])
    rr1 = scenario.rr_details[0] if scenario.rr_details else {}
    bars = _closed_bars_since(m15_closed, scenario.breakout_at)
    lifecycle = scenario.lifecycle_status
    blocks = list(dict.fromkeys(scenario.blocking_reasons))
    if lifecycle == "BREAKOUT_PENDING":
        from app.config import get_settings
        if bars >= get_settings().tactical_setup_expiry_bars:
            lifecycle = "EXPIRED"
            blocks = [code for code in blocks if code != "BREAKOUT_NOT_CONFIRMED"]
            blocks.append("SETUP_EXPIRED")
            result.market_decision.reason = "突破確認等待已達上限，劇本已失效；等待新的結構事件。"
            result.decision.reason = result.market_decision.reason
    scenario = scenario.model_copy(update={
        "closed_bars_since_breakout": bars, "lifecycle_status": lifecycle,
        "blocking_reasons": blocks,
    })
    if direction == "LONG":
        result.long_scenario = scenario
    elif direction == "SHORT":
        result.short_scenario = scenario
    if result.normalized_analysis.marketDataStatus != "GOOD":
        blocks.append("MARKET_DATA_STALE")
    return DecisionTrace(
        setupId=scenario.setup_id, finalDecision=result.market_decision.action,
        lifecycleStatus=lifecycle, direction=direction,
        triggerLevel=result.normalized_analysis.triggerLevel,
        breakoutAt=scenario.breakout_at, closedBarsSinceBreakout=bars,
        confirmationPassed=scenario.lifecycle_status == "READY",
        entryZoneValid=scenario.lifecycle_status != "INVALID" and bool(entry),
        priceInsideEntryZone=inside,
        riskRewardPassed=bool(rr1.get("available")) and rr1.get("ratio", 0) >= 1.5,
        structurePassed=scenario.lifecycle_status != "INVALID",
        dataQualityPassed=result.normalized_analysis.marketDataStatus == "GOOD",
        positionStatus="OPEN" if result.position_management.has_position else "FLAT",
        blockingReasons=blocks, evaluatedAt=evaluated_at,
        marketSnapshotAt=market_snapshot_at,
    )
