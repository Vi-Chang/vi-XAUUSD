"""Persist the position-free exit, breakout and virtual-profit monitors."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select

from app.db.models import MarketMonitorState
from app.db.session import db_session
from app.engines.break_lifecycle import evaluate_break_lifecycle
from app.engines.breakout_alert_state import (
    BreakoutAlertState,
    breakout_view,
    evaluate_breakout_alert,
)
from app.engines.breakout_setup_manager import (
    evaluate_breakout_setups,
    migrate_legacy_breakout_setup,
)
from app.engines.canonical_conflict_resolver import (
    build_canonical_market_snapshot,
    engine_result_envelope,
    stamp_engine_result,
)
from app.engines.data_health_gate import evaluate_data_health
from app.engines.decision_assistant import evaluate_decision_assistant
from app.engines.decision_health import (
    evaluate_decision_health,
    evaluate_defense_state,
)
from app.engines.dynamic_profit_protection import evaluate_dynamic_profit
from app.engines.early_entry_candidate import (
    apply_canonical_entry_result,
    evaluate_early_entry_candidate,
)
from app.engines.entry_opportunity import evaluate_entry_opportunities
from app.engines.entry_starvation_monitor import evaluate_entry_starvation
from app.engines.failed_breakout_rejection import (
    evaluate_failed_breakout,
    evaluate_intrabar_support_pressure,
)
from app.engines.fake_breakout_recovery import evaluate_fake_breakout_recovery
from app.engines.final_decision_engine import (
    collect_signal_candidates,
    evaluate_final_decision,
)
from app.engines.hypothetical_exit_advisor import (
    build_hypothetical_exit_plans,
    evaluate_hypothetical_exits,
)
from app.engines.live_bias import evaluate_live_bias
from app.engines.market_behavior import evaluate_market_behavior
from app.engines.opportunity_coverage_watchdog import evaluate_opportunity_coverage
from app.engines.regime_state_machine import evaluate_regime_state
from app.engines.signal_lifecycle import evaluate_signal_lifecycle
from app.engines.trade_plan import evaluate_trade_plans, migrate_legacy_virtual_profit
from app.engines.trend_continuation_engine import evaluate_trend_continuation
from app.engines.virtual_profit_tracker import evaluate_virtual_profit
from app.engines.volume_intelligence import evaluate_volume_intelligence
from app.engines.wick_rejection import evaluate_wick_rejection
from app.services.double_sweep_service import evaluate_double_sweep_monitor


def _load(symbol: str, key: str) -> dict:
    with db_session() as db:
        row = db.execute(
            select(MarketMonitorState).where(
                MarketMonitorState.symbol == symbol,
                MarketMonitorState.monitor_key == key,
            )
        ).scalar_one_or_none()
        return dict(row.payload or {}) if row else {}


def _save(symbol: str, key: str, payload: dict) -> None:
    with db_session() as db:
        row = db.execute(
            select(MarketMonitorState).where(
                MarketMonitorState.symbol == symbol,
                MarketMonitorState.monitor_key == key,
            )
        ).scalar_one_or_none()
        if row is None:
            row = MarketMonitorState(
                symbol=symbol, monitor_key=key, updated_at=datetime.now(timezone.utc)
            )
            db.add(row)
        row.payload, row.updated_at = payload, datetime.now(timezone.utc)


def _last_close(frame: pd.DataFrame | None) -> float | None:
    if frame is None or frame.empty:
        return None
    return float(frame.iloc[-1]["close"])


def _defense_scenario_identity(previous_final: dict,
                               previous_health: dict) -> tuple[str, int, str]:
    """Resolve the lifecycle boundary used by the defense event ledger."""
    scenario_id = str(previous_final.get("selectedScenarioId") or
                      previous_health.get("scenarioId") or "UNSCOPED")
    version = int(previous_final.get("selectedScenarioVersion") or
                  previous_health.get("scenarioVersion") or 1)
    same_scenario = str(previous_health.get("scenarioId") or "") == scenario_id
    structure_version = str(
        previous_health.get("structureVersion")
        if same_scenario and previous_health.get("structureVersion") is not None
        else previous_final.get("selectedScenarioVersion") or version)
    return scenario_id, version, structure_version


def evaluate_market_monitors(
    data: dict, *, m15_closed: pd.DataFrame | None = None,
    h1_closed: pd.DataFrame | None = None, h4_closed: pd.DataFrame | None = None,
    indicators: dict | None = None
) -> dict:
    symbol = str(data.get("symbol") or "XAUUSD")
    normalized = dict(data.get("normalized_analysis") or {})
    previous_decision_health = _load(symbol, "decision_health")
    previous_final_decision = _load(symbol, "final_decision")
    market_snapshot = build_canonical_market_snapshot(data)
    data = {
        **data, "canonical_market_snapshot": market_snapshot,
        "snapshotId": market_snapshot["snapshotId"],
        "previous_canonical_strategy_snapshot": (
            previous_final_decision.get("canonicalDecision") or
            previous_final_decision),
    }
    decision_health = evaluate_decision_health(
        data, previous=previous_decision_health,
        now=str(data.get("timestamp_utc") or "") or None)
    scenario_id, scenario_version, structure_version = _defense_scenario_identity(
        previous_final_decision, previous_decision_health)
    defense_binding = dict(previous_final_decision.get("defenseBinding") or {})
    decision_health.update(evaluate_defense_state(
        defense_level=defense_binding.get("level", previous_final_decision.get(
            "invalidationPrice")),
        side=str(defense_binding.get("side") or previous_final_decision.get(
            "direction") or "NEUTRAL"),
        current_price=normalized.get("currentPrice"),
        atr15=float(normalized.get("atr15") or 0),
        closed_context=decision_health.get("latestClosed15m"),
        entry_confirmation=str(decision_health.get("entryConfirmation") or
                               "BLOCKED_BY_DATA"),
        previous=previous_decision_health,
        reclaim_level=((previous_final_decision.get("canonicalDecision") or {}).get(
            "canonicalNextTrigger") or {}).get("level") or normalized.get("triggerLevel"),
        scenario_id=scenario_id, scenario_version=scenario_version,
        structure_version=structure_version,
        defense_strategy_id=str(defense_binding.get("strategyId") or scenario_id),
        defense_side=str(defense_binding.get("side") or previous_final_decision.get(
            "direction") or ""),
    ))
    _save(symbol, "decision_health", decision_health)
    data = {**data, "decision_health_state": decision_health}
    health = evaluate_data_health(data)
    canonical_health = str(decision_health.get("dataHealth") or "INVALID")
    if canonical_health != "HEALTHY":
        normalized["marketDataStatus"] = (
            "DEGRADED" if canonical_health == "DEGRADED" else "FAILED")
        normalized["dataHealthReason"] = (
            "部分收盤資料暫缺；原策略僅供參考，所有價位暫不可執行"
            if canonical_health == "DEGRADED" else
            "必要行情資料無效；已清除可執行訊號")
        data = {**data, "normalized_analysis": normalized, "data_health": health}
    indicators = indicators or {}
    exit_state, exit_events = evaluate_hypothetical_exits(
        data, _load(symbol, "hypothetical_exit")
    )
    _save(symbol, "hypothetical_exit", exit_state)

    raw_breakout = _load(symbol, "bullish_breakout")
    breakout_previous = (
        BreakoutAlertState(**raw_breakout) if raw_breakout else BreakoutAlertState()
    )
    m15_ind = indicators.get("15M") or {}
    macd_declining = (
        isinstance(m15_ind.get("macd_hist"), (int, float))
        and isinstance(m15_ind.get("macd_hist_prev"), (int, float))
        and m15_ind["macd_hist"] < m15_ind["macd_hist_prev"]
    )
    breakout_state, breakout_event = evaluate_breakout_alert(
        normalized,
        breakout_previous,
        h1_close=_last_close(h1_closed),
        higher_low_broken=normalized.get("supportState")
        in ("confirmed_breakdown", "retest_rejected"),
        macd_declining=macd_declining,
    )
    _save(symbol, "bullish_breakout", asdict(breakout_state))

    entry = dict(data.get("entry_engine") or {})
    support = next(
        (
            x
            for x in normalized.get("confirmationLevels", [])
            if x.get("kind") == "support"
        ),
        None,
    )
    structure_protection = float(support["price"]) if support else None
    # Detect sweep context before freezing a new trade thesis. Statistical
    # context may select the initial structural level, but can never mutate it
    # after the trade plan is created.
    timeframe_data = data.get("timeframes") or {}
    generated = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
    try:
        evaluation_time = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError:
        evaluation_time = datetime.now(timezone.utc)
    double_sweep_state, double_sweep_events = evaluate_double_sweep_monitor(
        m15_closed, symbol=symbol,
        current_price=float(normalized.get("currentPrice") or 0),
        regime4h=str((timeframe_data.get("h4") or {}).get("trend")
                    or data.get("market_state") or "UNKNOWN"),
        structure1h=str((timeframe_data.get("h1") or {}).get("structure") or "UNKNOWN"),
        macro_context=str((data.get("event_risk") or {}).get("status") or "UNKNOWN"),
        now=evaluation_time, previous=_load(symbol, "double_sweep"))
    _save(symbol, "double_sweep", double_sweep_state)
    sweep = double_sweep_state.get("event") or {}
    if entry.get("status") == "ENTRY_TRIGGERED" and sweep:
        reference_low = sweep.get("referenceLow")
        reference_high = sweep.get("referenceHigh")
        sweep_description = "流動性掃掠後重新收回，reclaim 成立"
        if entry.get("direction") == "LONG" and isinstance(reference_low, (int, float)):
            sweep_description = f"{float(reference_low):.2f} 下方掃低後重新收回，多方 reclaim 成立"
        elif isinstance(reference_high, (int, float)):
            sweep_description = f"{float(reference_high):.2f} 上方掃高後重新跌回，空方 reclaim 成立"
        entry.update({
            "strategy_type": ("SWEEP_RECLAIM_LONG" if entry.get("direction") == "LONG"
                              else "SWEEP_RECLAIM_SHORT"),
            "sweep_low": sweep.get("referenceLow"),
            "sweep_high": sweep.get("referenceHigh"),
            "atr15": normalized.get("atr15"),
            "thesis_description": sweep_description,
            "thesis_evidence": ["LIQUIDITY_SWEEP", "RECLAIM", "CLOSED_CANDLE"],
            "mae_profile": double_sweep_state.get("profile") or {},
        })
    virtual_state, virtual_events = evaluate_virtual_profit(
        entry,
        _load(symbol, "virtual_profit"),
        current_price=float(normalized.get("currentPrice") or 0),
        closed_price=normalized.get("lastClosedCandlePrice"),
        latest_structure_protection=structure_protection,
        candle_close_time=str(normalized.get("lastClosedCandleTimestamp") or ""),
    )
    _save(symbol, "virtual_profit", virtual_state)
    stored_trade_plans = _load(symbol, "trade_plans")
    if not stored_trade_plans:
        stored_trade_plans = migrate_legacy_virtual_profit(
            virtual_state, symbol=symbol,
            calculated_at=str(data.get("timestamp_utc") or ""))
    trade_plan_state, trade_plan_events = evaluate_trade_plans(
        entry,
        stored_trade_plans,
        symbol=symbol,
        current_price=float(normalized.get("currentPrice") or 0),
        closed_price=normalized.get("lastClosedCandlePrice"),
        latest_structure_protection=structure_protection,
        candle_close_time=str(normalized.get("lastClosedCandleTimestamp") or ""),
        calculated_at=str(data.get("timestamp_utc") or ""),
        atr15=float(normalized.get("atr15") or 0),
        regime=str(normalized.get("marketRegime") or data.get("market_state") or ""),
        data_status=str(normalized.get("marketDataStatus") or "FAILED"),
    )
    _save(symbol, "trade_plans", trade_plan_state)
    stored_breakout_setups = _load(symbol, "breakout_setups")
    if not stored_breakout_setups:
        stored_breakout_setups = migrate_legacy_breakout_setup(
            {**data, "entry_engine": entry}, _load(symbol, "final_decision"))
    latest_closed_15m = {}
    if m15_closed is not None and not m15_closed.empty:
        row = m15_closed.iloc[-1]
        latest_closed_15m = {
            key: float(row[key]) for key in ("open", "high", "low", "close")
            if key in row and pd.notna(row[key])
        }
    break_state, break_events = evaluate_break_lifecycle(
        m15_closed, data=data, previous=_load(symbol, "break_lifecycle"))
    _save(symbol, "break_lifecycle", break_state)
    recovery_state, recovery_events = evaluate_fake_breakout_recovery(
        data=data, break_state=break_state,
        previous=_load(symbol, "fake_breakout_recovery"))
    _save(symbol, "fake_breakout_recovery", recovery_state)
    breakout_setup_state, breakout_setup_events = evaluate_breakout_setups(
        {**data, "entry_engine": entry, "latest_closed_15m": latest_closed_15m},
        stored_breakout_setups)
    _save(symbol, "breakout_setups", breakout_setup_state)
    opportunity_state, opportunity_events = evaluate_entry_opportunities(
        {**data, "latest_closed_15m": latest_closed_15m,
         "breakout_setup_manager": breakout_setup_state,
         "break_lifecycle_engine": break_state},
        _load(symbol, "entry_opportunities"))
    opportunity_state = stamp_engine_result(
        opportunity_state, market_snapshot, engine="entry_opportunity_engine")
    _save(symbol, "entry_opportunities", opportunity_state)
    continuation_state, continuation_events = evaluate_trend_continuation(
        {**data, "breakout_setup_manager": breakout_setup_state},
        m15=m15_closed, h1=h1_closed, h4=h4_closed,
        previous=_load(symbol, "trend_continuation"))
    _save(symbol, "trend_continuation", continuation_state)
    plans = {plan.side: asdict(plan) for plan in build_hypothetical_exit_plans(data)}
    monitor_result = {
        "decision_health_state": decision_health,
        "hypothetical_exit_advisor": {"plans": plans, "events": exit_events},
        "breakout_alert": breakout_view(breakout_state, breakout_event),
        "virtual_profit_tracker": {**virtual_state, "events": virtual_events},
        "trade_plan_manager": {**trade_plan_state, "events": trade_plan_events},
        "breakout_setup_manager": {
            **breakout_setup_state, "events": breakout_setup_events},
        "entry_opportunity_engine": {
            **opportunity_state, "events": opportunity_events},
        "trend_continuation_engine": {
            **continuation_state, "events": continuation_events},
        "double_sweep_statistical": {
            **double_sweep_state, "events": double_sweep_events},
        "fake_breakout_recovery": {
            **recovery_state, "events": recovery_events},
    }
    wick_state, wick_events = evaluate_wick_rejection(
        m15_closed, data=data, previous=_load(symbol, "wick_rejection"))
    wick_state = stamp_engine_result(
        wick_state, market_snapshot, engine="wick_rejection_engine")
    _save(symbol, "wick_rejection", wick_state)
    monitor_result["wick_rejection_engine"] = wick_state
    monitor_result["break_lifecycle_engine"] = break_state
    levels = list(normalized.get("confirmationLevels") or [])
    current_price = float(normalized.get("currentPrice") or 0)
    initial_bias = str(decision_health.get("marketBias") or "NEUTRAL")
    failed_side = ("LONG" if initial_bias == "BULLISH" else
                   "SHORT" if initial_bias == "BEARISH" else
                   str(previous_final_decision.get("direction") or "LONG"))
    resistance_kind = "resistance" if failed_side == "LONG" else "support"
    support_kind = "support" if failed_side == "LONG" else "resistance"
    resistance_prices = [float(item["price"]) for item in levels
                         if item.get("kind") == resistance_kind and
                         isinstance(item.get("price"), (int, float))]
    support_prices = [float(item["price"]) for item in levels
                      if item.get("kind") == support_kind and
                      isinstance(item.get("price"), (int, float))]
    atr15 = max(float(normalized.get("atr15") or wick_state.get("atr") or 0), .01)
    resistance_price = (min(resistance_prices, key=lambda value: abs(value-current_price))
                        if resistance_prices else None)
    support_price = (min(support_prices, key=lambda value: abs(value-current_price))
                     if support_prices else None)
    wick_direction_matches = (
        (failed_side == "LONG" and "UPPER" in str(
            wick_state.get("wick_rejection_state") or "")) or
        (failed_side == "SHORT" and "LOWER" in str(
            wick_state.get("wick_rejection_state") or "")))
    rejection_zone = (wick_state.get("wick_rejection_zone") or {}
                      if wick_direction_matches else {})
    resistance_zone = (dict(rejection_zone) if rejection_zone else
                       ({"low": resistance_price - atr15 * .15,
                         "high": resistance_price + atr15 * .15}
                        if resistance_price is not None else None))
    support_zone = ({"low": support_price, "high": support_price}
                    if support_price is not None else None)
    closed_rows = []
    if m15_closed is not None:
        for index, row in m15_closed.tail(8).iterrows():
            closed_rows.append({"time": str(index), **{
                key: float(row[key]) for key in ("open", "high", "low", "close")
                if key in row and pd.notna(row[key])}})
    position = data.get("position_management") or {}
    position_side = (str(position.get("position_side") or "").upper()
                     if position.get("has_position") else None)
    failed_state, failed_events = evaluate_failed_breakout(
        side=failed_side, resistance_zone=resistance_zone,
        support_zone=support_zone,
        attempt_count=int(wick_state.get("failed_breakout_count") or
                          wick_state.get("wick_rejection_count") or 0),
        closed_candles=closed_rows, wick_rejection=wick_state,
        current_price=current_price, position_side=position_side,
        momentum={
            "macd_histogram_shrinking": macd_declining,
            "kd_rollover": bool(m15_ind.get("stoch_k") is not None and
                                m15_ind.get("stoch_k_prev") is not None and
                                m15_ind["stoch_k"] < m15_ind["stoch_k_prev"]),
            "rsi_divergence": bool(m15_ind.get("rsi_divergence")),
        },
        volume={"decreasing_on_attempts": bool(m15_ind.get("volume_declining"))},
        follow_through={
            "distance_decreasing": bool(m15_ind.get("follow_through_declining")),
            "confirmed": bool(m15_ind.get("reclaim_follow_through")),
        },
        confirmation_buffer=atr15 * .05,
        base_bias_state=initial_bias,
        previous=_load(symbol, "failed_breakout_rejection"))
    failed_state = stamp_engine_result(
        failed_state, market_snapshot, engine="failed_breakout_rejection_engine")
    _save(symbol, "failed_breakout_rejection", failed_state)
    monitor_result["failed_breakout_rejection_engine"] = failed_state
    # Re-evaluate health/bias after the evidence engine. This is current
    # evidence, not a cached previous-bias override.
    refreshed_health = evaluate_decision_health(
        {**data, **monitor_result}, previous=previous_decision_health,
        now=str(data.get("timestamp_utc") or "") or None)
    decision_health.update({key: refreshed_health.get(key) for key in (
        "marketBias", "marketBiasState", "higherTimeframeBias",
        "biasConfidence", "marketContext")})
    decision_health = stamp_engine_result(
        decision_health, market_snapshot, engine="structure_health_engine")
    _save(symbol, "decision_health", decision_health)
    monitor_result["decision_health_state"] = decision_health
    data = {**data, "decision_health_state": decision_health}
    volume_state = evaluate_volume_intelligence(
        m15_closed=m15_closed, h1_closed=h1_closed,
        atr15=float(normalized.get("atr15") or 0),
        atr1h=float((indicators.get("1H") or {}).get("atr14") or 0),
        structural_bias=str(decision_health.get("marketBias") or "NEUTRAL"))
    volume_state = stamp_engine_result(
        volume_state, market_snapshot, engine="volume_engine",
        engine_version="volume-intelligence-v1")
    _save(symbol, "volume_intelligence", volume_state)
    monitor_result["volume_intelligence"] = volume_state
    live_candidates = [asdict(candidate) for candidate in collect_signal_candidates(
        {**data, **monitor_result})]
    live_bias_state, live_bias_events = evaluate_live_bias(
        {**data, **monitor_result},
        structural_bias=str(decision_health.get("marketBias") or "NEUTRAL"),
        candidates=live_candidates, previous=_load(symbol, "live_bias"))
    live_bias_state = stamp_engine_result(
        live_bias_state, market_snapshot, engine="live_bias_engine")
    _save(symbol, "live_bias", live_bias_state)
    monitor_result["live_bias_state"] = live_bias_state
    behavior_input = {**data, "wick_rejection_engine": wick_state,
                      "break_lifecycle_engine": break_state,
                      "failed_breakout_rejection_engine": failed_state}
    behavior_state, behavior_events = evaluate_market_behavior(
        m15=m15_closed, h1=h1_closed, h4=h4_closed, data=behavior_input,
        previous=_load(symbol, "market_behavior"))
    behavior_state = stamp_engine_result(
        behavior_state, market_snapshot, engine="market_behavior_engine")
    _save(symbol, "market_behavior", behavior_state)
    monitor_result["market_behavior_engine"] = behavior_state
    early_state, early_events = evaluate_early_entry_candidate(
        {**data, **monitor_result}, _load(symbol, "early_entry_candidate"))
    monitor_result["early_entry_candidate"] = early_state
    coverage_state, coverage_events = evaluate_opportunity_coverage(
        {**data, **monitor_result}, early_state,
        _load(symbol, "opportunity_coverage"))
    _save(symbol, "opportunity_coverage", coverage_state)
    monitor_result["opportunity_coverage_watchdog"] = coverage_state
    profit_state, profit_events = evaluate_dynamic_profit(
        data={**data, **monitor_result, "indicator_snapshot": indicators}, frame=m15_closed,
        trade_plans=trade_plan_state, break_state=break_state,
        previous=_load(symbol, "dynamic_profit"))
    _save(symbol, "dynamic_profit", profit_state)
    monitor_result["dynamic_profit_protection"] = profit_state
    regime_state, regime_events = evaluate_regime_state(
        data, indicators=indicators, previous=_load(symbol, "regime_state"))
    _save(symbol, "regime_state", regime_state)
    monitor_result["regime_state_machine"] = regime_state
    assistant_state, assistant_events = evaluate_decision_assistant(
        {**data, **monitor_result}, latest_candle=latest_closed_15m,
        previous=_load(symbol, "decision_assistant"))
    _save(symbol, "decision_assistant", assistant_state)
    monitor_result["decision_assistant"] = assistant_state
    signal_facts = (exit_events + ([breakout_event] if breakout_event else []) + wick_events
                    + failed_events
                    + virtual_events + trade_plan_events + breakout_setup_events
                    + continuation_events + regime_events + behavior_events
                    + assistant_events + opportunity_events + early_events
                    + coverage_events + live_bias_events)
    signal_facts += (double_sweep_events + break_events + recovery_events
                     + profit_events)
    health_event = decision_health.get("dataHealthEvent")
    if health_event:
        published_bias = str(previous_final_decision.get("marketBias") or
                             previous_final_decision.get("direction") or
                             decision_health.get("marketBias") or "NEUTRAL").upper()
        published_bias = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(
            published_bias, published_bias)
        signal_facts.append({
            **dict(health_event),
            "marketBias": published_bias,
            "entryConfirmation": decision_health.get("entryConfirmation"),
            "dataHealth": decision_health.get("dataHealth"),
            "transitionReason": decision_health.get("reason"),
        })
    previous_defense = str(previous_decision_health.get("defenseState") or "")
    current_defense = str(decision_health.get("defenseState") or "")
    defense_event_types = {
        "TESTING": "DEFENSE_TEST", "BROKEN_PENDING_CLOSE": "DEFENSE_TEST",
        "RECLAIMED": "DEFENSE_RECLAIMED", "HELD": "DEFENSE_HELD",
        "BROKEN_CONFIRMED": "DEFENSE_BROKEN_CONFIRMED",
    }
    if previous_defense != current_defense and current_defense in defense_event_types:
        signal_facts.append({
            "event_type": defense_event_types[current_defense],
            "currentState": current_defense,
            "marketBias": decision_health.get("marketBias"),
            "entryConfirmation": decision_health.get("entryConfirmation"),
            "dataHealth": decision_health.get("dataHealth"),
            "defenseState": current_defense,
            "defenseLevel": decision_health.get("defenseLevel"),
            "defenseSide": decision_health.get("side"),
            "confirmationBuffer": decision_health.get("confirmationBuffer"),
            "falseBreakDetected": decision_health.get("falseBreakDetected"),
            "scenarioId": decision_health.get("scenarioId"),
            "scenarioState": decision_health.get("scenarioState"),
            "marketContext": decision_health.get("marketContext"),
            "closedBarTimestamp": ((decision_health.get("latestClosed15m") or
                                    decision_health.get("contextClosed15m") or {}).get(
                                        "closeTime")),
            "transitionReason": (
                "盤中價格已穿越防守，等待15M收盤確認"
                if current_defense == "BROKEN_PENDING_CLOSE"
                else "防守測試狀態發生實質變化"),
        })
    reclaim_event = decision_health.get("reclaimEvent")
    if reclaim_event and not previous_decision_health.get("reclaimEvent"):
        signal_facts.append({
            **dict(reclaim_event), "event_type": "NEW_RECLAIM_EVENT",
            "currentState": "WAIT_NEW_STRUCTURE",
            "marketBias": decision_health.get("marketBias"),
            "dataHealth": decision_health.get("dataHealth"),
            "entryConfirmation": decision_health.get("entryConfirmation"),
            "scenarioId": reclaim_event.get("newScenarioId"),
            "marketContext": decision_health.get("marketContext"),
            "transitionReason": "舊劇本維持失效；reclaim 只建立全新候選劇本",
        })
    engine_results = [
        engine_result_envelope(name, result, market_snapshot,
                               str(result.get("engineVersion") or "v1"))
        for name, result in (
            ("structure_health_engine", decision_health),
            ("volume_engine", volume_state),
            ("live_bias_engine", live_bias_state),
            ("market_behavior_engine", behavior_state),
            ("wick_rejection_engine", wick_state),
            ("failed_breakout_rejection_engine", failed_state),
            ("entry_opportunity_engine", opportunity_state),
        )
    ]
    final_input = {
        **data, **monitor_result, "signal_facts": signal_facts,
        "canonical_engine_results": engine_results,
        "previous_canonical_strategy_snapshot": (
            previous_final_decision.get("canonicalDecision") or previous_final_decision),
    }
    final_state, final_events = evaluate_final_decision(
        final_input, previous_final_decision
    )
    starvation_state, starvation_events = evaluate_entry_starvation(
        final_state, previous=_load(symbol, "entry_starvation"),
        evaluated_at=str(data.get("timestamp_utc") or "") or None)
    if starvation_events:
        starvation_state["latestDiagnosticEvent"] = starvation_events[-1]
    _save(symbol, "entry_starvation", starvation_state)
    monitor_result["entry_starvation_monitor"] = starvation_state
    final_state["entryStarvationMonitor"] = starvation_state
    from app.engines.canonical_decision import build_canonical_decision
    canonical = build_canonical_decision(final_input, final_state)
    early_state = apply_canonical_entry_result(
        early_state, {**canonical, **final_state},
        evaluated_at=str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat()))
    _save(symbol, "early_entry_candidate", early_state)
    monitor_result["early_entry_candidate"] = early_state
    final_state["canonicalDecision"] = canonical
    canonical_entry = dict(canonical.get("newEntryDecision") or {})
    final_state.update({
        "snapshotId": canonical.get("snapshotId"),
        "canonicalMarketSnapshot": canonical.get("canonicalMarketSnapshot"),
        "conflictType": canonical.get("conflictType"),
        "conflictReasonTrace": canonical.get("conflictReasonTrace"),
        "snapshotCompleteness": canonical.get("snapshotCompleteness"),
        "timeframeState": canonical.get("timeframeState"),
        "lastConfirmedBias": canonical.get("lastConfirmedBias"),
        "tradePermission": canonical.get("tradePermission"),
        "marketBias": canonical.get("marketBias", final_state.get("marketBias")),
        "structuralBias": canonical.get(
            "structuralBias", final_state.get("structuralBias")),
        "liveBiasState": canonical.get("liveBiasState", final_state.get("liveBiasState")),
        "executionBias": canonical.get("executionBias", final_state.get("executionBias")),
        "canEnter": bool(canonical_entry.get("canEnter")),
    })
    if not final_state["canEnter"] and canonical.get("tradePermission") in {
            "BLOCKED_DATA", "BLOCKED_SYSTEM"}:
        final_state.update({"finalAction": "WAIT", "state": "WAIT_CONFIRMATION"})
    conflict_type = str(canonical.get("conflictType") or "NO_CONFLICT")
    previous_conflict = str((previous_final_decision.get("canonicalDecision") or
                             previous_final_decision).get("conflictType") or
                            "NO_CONFLICT")
    conflict_event_types = {
        "TIMEFRAME_DIVERGENCE": "TIMEFRAME_DIVERGENCE",
        "BIAS_TRANSITION": "BIAS_TRANSITION",
        "SCORE_NEAR_TIE": "SCORE_NEAR_TIE",
        "TRUE_ENGINE_CONFLICT": "TRUE_ENGINE_CONFLICT",
        "CANONICAL_INVARIANT_VIOLATION": "SYSTEM_STATE_INVARIANT_BLOCKED",
    }
    if conflict_type != previous_conflict and conflict_type in conflict_event_types:
        final_events.append({
            "event_type": conflict_event_types[conflict_type],
            "currentState": final_state.get("state"),
            "finalDecision": final_state.get("finalAction"),
            "canEnter": final_state.get("canEnter"),
            "marketBias": final_state.get("marketBias"),
            "structuralBias": final_state.get("structuralBias"),
            "liveBiasState": final_state.get("liveBiasState"),
            "executionBias": final_state.get("executionBias"),
            "timeframeState": canonical.get("timeframeState"),
            "conflictType": conflict_type,
            "lastConfirmedBias": canonical.get("lastConfirmedBias"),
            "dataHealth": canonical.get("dataHealth"),
            "snapshotId": canonical.get("snapshotId"),
            "transitionReason": "市場週期、即時動能或系統一致性分類出現實質變化",
        })
    for event in final_events:
        event["canonicalDecision"] = canonical
        event["nextTriggerCondition"] = canonical["canonicalNextTrigger"]
    lifecycle_input = {**final_state,
                       "entryZone": final_state.get("entryZone") or canonical.get("entryZone"),
                       "triggerLevel": (canonical.get("canonicalNextTrigger") or {}).get("level")
                       if isinstance(canonical.get("canonicalNextTrigger"), dict) else None}
    lifecycle_state, lifecycle_events = evaluate_signal_lifecycle(
        lifecycle_input, _load(symbol, "signal_lifecycle"))
    _save(symbol, "signal_lifecycle", lifecycle_state)
    for event in lifecycle_events:
        event.update({
            "symbol": symbol, "decisionId": final_state.get("decisionId"),
            "decisionVersion": final_state.get("decisionVersion"),
            "dataVersion": int(data.get("version") or 0),
            "marketState": lifecycle_state.get("bias"),
            "canonicalDecision": canonical,
        })
    final_events.extend(lifecycle_events)
    from app.engines.candle_close_analysis import evaluate_candle_close_reports
    close_state, close_events = evaluate_candle_close_reports(
        final_input, final_state, volume=volume_state,
        m15_closed=m15_closed, h1_closed=h1_closed,
        previous=_load(symbol, "candle_close_analysis"),
        evaluated_at=str(data.get("timestamp_utc") or "") or None)
    _save(symbol, "candle_close_analysis", close_state)
    monitor_result["candle_close_analysis"] = close_state
    final_state["candleCloseAnalysis"] = close_state
    for event in close_events:
        event["canonicalDecision"] = canonical
        event["decisionId"] = final_state.get("decisionId")
        event["decisionVersion"] = final_state.get("decisionVersion")
        event["canonicalStateVersion"] = final_state.get("decisionVersion")
    final_events.extend(close_events)
    final_state["events"] = final_events
    if final_events:
        final_state["latest_event"] = final_events[-1]
    from app.services.current_decision_store import atomic_publish_canonical_snapshot
    final_state, _ = atomic_publish_canonical_snapshot(symbol, final_state)
    final_events = list(final_state.get("events") or [])
    if final_state.get("decisionChanged"):
        from app.services.decision_replay import persist_decision_replay
        persist_decision_replay(symbol, final_input, final_state)
        from app.db.session import db_session
        from app.services.phase2_validation import persist_decision_journals
        with db_session() as db:
            persist_decision_journals(
                db, symbol=symbol, data=final_input, decision=final_state)
    return {
        **monitor_result,
        "final_decision_state": final_state,
        "final_events": final_events,
    }


def evaluate_live_quote_state(
    data: dict, *, price: float, quote_time: str
) -> tuple[dict, list[dict]]:
    """Re-evaluate transitions on every quote without pretending a candle closed."""
    symbol = str(data.get("symbol") or "XAUUSD")
    normalized = dict(data.get("normalized_analysis") or {})
    normalized["currentPrice"] = price
    normalized["marketDataTimestamp"] = quote_time
    normalized["sourceTimestamps"] = {
        key: quote_time for key in (normalized.get("sourceTimestamps") or {"market": quote_time})
    }
    normalized["sourcePrices"] = {
        key: price for key in (normalized.get("sourcePrices") or {"market": price})
    }
    candidate = {**data, "normalized_analysis": normalized,
                 "snapshot_ts": quote_time, "timestamp_utc": quote_time,
                 "current_price": {**(data.get("current_price") or {}),
                                   "mid": price, "last_update": quote_time}}
    market_snapshot = build_canonical_market_snapshot(candidate)
    previous_final = _load(symbol, "final_decision")
    candidate.update({
        "canonical_market_snapshot": market_snapshot,
        "snapshotId": market_snapshot["snapshotId"],
        "previous_canonical_strategy_snapshot": (
            previous_final.get("canonicalDecision") or previous_final),
    })
    previous_decision_health = _load(symbol, "decision_health")
    decision_health = evaluate_decision_health(
        candidate, previous=previous_decision_health, now=quote_time)
    scenario_id, scenario_version, structure_version = _defense_scenario_identity(
        previous_final, previous_decision_health)
    defense_binding = dict(previous_final.get("defenseBinding") or {})
    decision_health.update(evaluate_defense_state(
        defense_level=defense_binding.get("level", previous_final.get(
            "invalidationPrice")),
        side=str(defense_binding.get("side") or previous_final.get(
            "direction") or "NEUTRAL"),
        current_price=price, atr15=float(normalized.get("atr15") or 0),
        closed_context=decision_health.get("latestClosed15m"),
        entry_confirmation=str(decision_health.get("entryConfirmation") or
                               "BLOCKED_BY_DATA"),
        previous=previous_decision_health,
        reclaim_level=((previous_final.get("canonicalDecision") or {}).get(
            "canonicalNextTrigger") or {}).get("level") or normalized.get("triggerLevel"),
        scenario_id=scenario_id, scenario_version=scenario_version,
        structure_version=structure_version,
        defense_strategy_id=str(defense_binding.get("strategyId") or scenario_id),
        defense_side=str(defense_binding.get("side") or previous_final.get(
            "direction") or ""),
    ))
    previous_failed = _load(symbol, "failed_breakout_rejection")
    live_failed, failed_live_events = evaluate_intrabar_support_pressure(
        previous_failed, current_price=price)
    if live_failed:
        _save(symbol, "failed_breakout_rejection", live_failed)
        candidate["failed_breakout_rejection_engine"] = live_failed
        refreshed_health = evaluate_decision_health(
            candidate, previous=previous_decision_health, now=quote_time)
        decision_health.update({key: refreshed_health.get(key) for key in (
            "marketBias", "marketBiasState", "higherTimeframeBias",
            "biasConfidence", "marketContext")})
    candidate["decision_health_state"] = decision_health
    _save(symbol, "decision_health", decision_health)
    from app.engines.freshness_state import evaluate_freshness_state
    from app.utils.timeutils import parse_utc
    candidate["freshness_state"] = evaluate_freshness_state(
        candidate, now=parse_utc(quote_time))
    canonical_health = str(decision_health.get("dataHealth") or "INVALID")
    if canonical_health != "HEALTHY":
        normalized["marketDataStatus"] = (
            "DEGRADED" if canonical_health == "DEGRADED" else "FAILED")
        normalized["dataHealthReason"] = (
            "部分收盤資料暫缺；原策略僅供參考，所有價位暫不可執行"
            if canonical_health == "DEGRADED" else
            "必要行情資料無效；已清除可執行訊號")
    regime_state, _ = evaluate_regime_state(
        {**candidate, "normalized_analysis": normalized},
        previous=_load(symbol, "regime_state"),
    )
    _save(symbol, "regime_state", regime_state)
    live_facts: list[dict] = list(failed_live_events)
    canonical_bias = str(previous_final.get("marketBias") or
                         previous_final.get("direction") or
                         decision_health.get("marketBias") or "NEUTRAL")
    canonical_bias = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(
        canonical_bias.upper(), canonical_bias.upper())
    health_event = decision_health.get("dataHealthEvent")
    if health_event:
        live_facts.append({
            **dict(health_event),
            # Data health consumes the last published canonical direction; it
            # never derives or restores a separate cached market bias.
            "marketBias": canonical_bias,
            "entryConfirmation": decision_health.get("entryConfirmation"),
            "dataHealth": decision_health.get("dataHealth"),
            "transitionReason": decision_health.get("reason"),
        })
    old_defense = str(previous_decision_health.get("defenseState") or "")
    new_defense = str(decision_health.get("defenseState") or "")
    defense_events = {
        "TESTING": "DEFENSE_TEST", "BROKEN_PENDING_CLOSE": "DEFENSE_TEST",
        "RECLAIMED": "DEFENSE_RECLAIMED", "HELD": "DEFENSE_HELD",
        "BROKEN_CONFIRMED": "DEFENSE_BROKEN_CONFIRMED",
    }
    if old_defense != new_defense and new_defense in defense_events:
        live_facts.append({
            "event_type": defense_events[new_defense], "currentState": new_defense,
            "marketBias": decision_health.get("marketBias"),
            "entryConfirmation": decision_health.get("entryConfirmation"),
            "dataHealth": decision_health.get("dataHealth"),
            "defenseState": new_defense, "defenseLevel": decision_health.get("defenseLevel"),
            "defenseSide": decision_health.get("side"),
            "confirmationBuffer": decision_health.get("confirmationBuffer"),
            "scenarioId": decision_health.get("scenarioId"),
            "scenarioState": decision_health.get("scenarioState"),
            "marketContext": decision_health.get("marketContext"),
            "closedBarTimestamp": ((decision_health.get("latestClosed15m") or
                                    decision_health.get("contextClosed15m") or {}).get(
                                        "closeTime")),
        })
    reclaim_event = decision_health.get("reclaimEvent")
    if reclaim_event and not previous_decision_health.get("reclaimEvent"):
        live_facts.append({
            **dict(reclaim_event), "event_type": "NEW_RECLAIM_EVENT",
            "currentState": "WAIT_NEW_STRUCTURE",
            "marketBias": decision_health.get("marketBias"),
            "dataHealth": decision_health.get("dataHealth"),
            "entryConfirmation": decision_health.get("entryConfirmation"),
            "scenarioId": reclaim_event.get("newScenarioId"),
            "marketContext": decision_health.get("marketContext"),
            "transitionReason": "舊劇本維持失效；reclaim 只建立全新候選劇本",
        })
    early_state, early_events = evaluate_early_entry_candidate(
        {**candidate, "normalized_analysis": normalized,
         "decision_health_state": decision_health,
         "entry_opportunity_engine": _load(symbol, "entry_opportunities"),
         "break_lifecycle_engine": _load(symbol, "break_lifecycle"),
         "wick_rejection_engine": _load(symbol, "wick_rejection"),
         "market_behavior_engine": _load(symbol, "market_behavior")},
        _load(symbol, "early_entry_candidate"))
    _save(symbol, "early_entry_candidate", early_state)
    live_facts.extend(early_events)
    coverage_state, coverage_events = evaluate_opportunity_coverage(
        {**candidate, "normalized_analysis": normalized}, early_state,
        _load(symbol, "opportunity_coverage"))
    _save(symbol, "opportunity_coverage", coverage_state)
    live_facts.extend(coverage_events)
    current, events = evaluate_final_decision(
        {**candidate, "normalized_analysis": normalized,
         "regime_state_machine": regime_state, "signal_facts": live_facts}, previous_final
    )
    from app.engines.realtime_presentation import build_realtime_presentation
    presentation = build_realtime_presentation(
        candidate, price=price, quote_time=quote_time, now=parse_utc(quote_time))
    current["realtimePresentation"] = presentation
    current["freshnessState"] = candidate["freshness_state"]
    current["earlyEntryCandidate"] = early_state
    if presentation["opportunityState"] == "WAIT_RETEST":
        current["canEnter"] = False
        current["finalAction"] = "WAIT"
        current["humanSummary"] = "突破已確認，但目前離原進場區過遠；不追價，等待回踩。"
    if presentation["defenseState"] in {"TESTING", "BROKEN_PENDING_CLOSE"}:
        current["canEnter"] = False
        current["positionDefenseState"] = presentation["defenseState"]
    from app.engines.canonical_decision import build_canonical_decision
    canonical_input = {
        **candidate, "normalized_analysis": normalized,
        "decision_health_state": decision_health,
        "entry_opportunity_engine": _load(symbol, "entry_opportunities"),
        "break_lifecycle_engine": _load(symbol, "break_lifecycle"),
        "wick_rejection_engine": _load(symbol, "wick_rejection"),
        "market_behavior_engine": _load(symbol, "market_behavior"),
        "previous_canonical_strategy_snapshot": (
            previous_final.get("canonicalDecision") or previous_final),
    }
    canonical = build_canonical_decision(canonical_input, current)
    canonical_entry = dict(canonical.get("newEntryDecision") or {})
    current.update({
        "canonicalDecision": canonical, "snapshotId": canonical.get("snapshotId"),
        "canonicalMarketSnapshot": canonical.get("canonicalMarketSnapshot"),
        "conflictType": canonical.get("conflictType"),
        "conflictReasonTrace": canonical.get("conflictReasonTrace"),
        "snapshotCompleteness": canonical.get("snapshotCompleteness"),
        "timeframeState": canonical.get("timeframeState"),
        "lastConfirmedBias": canonical.get("lastConfirmedBias"),
        "tradePermission": canonical.get("tradePermission"),
        "marketBias": canonical.get("marketBias", current.get("marketBias")),
        "structuralBias": canonical.get("structuralBias", current.get("structuralBias")),
        "liveBiasState": canonical.get("liveBiasState", current.get("liveBiasState")),
        "executionBias": canonical.get("executionBias", current.get("executionBias")),
        "canEnter": bool(canonical_entry.get("canEnter")),
    })
    for event in events:
        event["canonicalDecision"] = canonical
        event["snapshotId"] = canonical.get("snapshotId")
    lifecycle_state, lifecycle_events = evaluate_signal_lifecycle(
        current, _load(symbol, "signal_lifecycle"), live_quote=True)
    _save(symbol, "signal_lifecycle", lifecycle_state)
    for event in lifecycle_events:
        event.update({
            "symbol": symbol, "decisionId": current.get("decisionId"),
            "decisionVersion": current.get("decisionVersion"),
            "dataVersion": int(candidate.get("version") or 0),
            "marketState": lifecycle_state.get("bias"),
        })
    events.extend(lifecycle_events)
    current["events"] = events
    from app.services.current_decision_store import atomic_publish_canonical_snapshot
    current, published = atomic_publish_canonical_snapshot(symbol, current)
    events = list(current.get("events") or []) if published else []
    from app.services.decision_outbox import persist_decision_events
    events = persist_decision_events(symbol, events)
    current["events"] = events
    if events:
        current["latest_event"] = events[-1]
    if current.get("decisionChanged"):
        from app.services.decision_replay import persist_decision_replay
        persist_decision_replay(symbol, candidate, current)
    return current, events


def persist_final_decision_state(symbol: str, state: dict) -> None:
    from app.services.current_decision_store import atomic_publish_canonical_snapshot
    canonical, _ = atomic_publish_canonical_snapshot(symbol, state)
    state.clear()
    state.update(canonical)


async def notify_market_monitor_events(result: dict, notifier) -> None:
    """Legacy compatibility hook; market rules must never push directly.

    The canonical FinalDecision outbox is the sole Telegram market-alert path.
    """
    return
