"""Persist the position-free exit, breakout and virtual-profit monitors."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select

from app.db.models import MarketMonitorState
from app.db.session import db_session
from app.engines.breakout_alert_state import (
    BreakoutAlertState,
    breakout_view,
    evaluate_breakout_alert,
)
from app.engines.hypothetical_exit_advisor import (
    build_hypothetical_exit_plans,
    evaluate_hypothetical_exits,
)
from app.engines.trade_plan import evaluate_trade_plans, migrate_legacy_virtual_profit
from app.engines.unified_decision_state import evaluate_unified_decision
from app.engines.virtual_profit_tracker import evaluate_virtual_profit


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


def evaluate_market_monitors(
    data: dict, *, h1_closed: pd.DataFrame | None = None, indicators: dict | None = None
) -> dict:
    symbol = str(data.get("symbol") or "XAUUSD")
    normalized = data.get("normalized_analysis") or {}
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

    entry = data.get("entry_engine") or {}
    support = next(
        (
            x
            for x in normalized.get("confirmationLevels", [])
            if x.get("kind") == "support"
        ),
        None,
    )
    structure_protection = float(support["price"]) if support else None
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
    )
    _save(symbol, "trade_plans", trade_plan_state)
    plans = {plan.side: asdict(plan) for plan in build_hypothetical_exit_plans(data)}
    monitor_result = {
        "hypothetical_exit_advisor": {"plans": plans, "events": exit_events},
        "breakout_alert": breakout_view(breakout_state, breakout_event),
        "virtual_profit_tracker": {**virtual_state, "events": virtual_events},
        "trade_plan_manager": {**trade_plan_state, "events": trade_plan_events},
    }
    final_input = {**data, **monitor_result}
    final_state, final_events = evaluate_unified_decision(
        final_input, _load(symbol, "final_decision")
    )
    _save(
        symbol,
        "final_decision",
        {k: v for k, v in final_state.items() if k != "events"},
    )
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
    current, events = evaluate_unified_decision(
        {**data, "normalized_analysis": normalized}, _load(symbol, "final_decision")
    )
    from app.services.decision_outbox import persist_decision_events

    events = persist_decision_events(symbol, events)
    current["events"] = events
    if events:
        current["latest_event"] = events[-1]
    _save(symbol, "final_decision", {k: v for k, v in current.items() if k != "events"})
    return current, events


def persist_final_decision_state(symbol: str, state: dict) -> None:
    _save(symbol, "final_decision", {k: v for k, v in state.items() if k != "events"})


async def notify_market_monitor_events(result: dict, notifier) -> None:
    """Legacy compatibility hook; market rules must never push directly.

    The canonical FinalDecision outbox is the sole Telegram market-alert path.
    """
    return
