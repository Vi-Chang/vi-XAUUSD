"""Persistence and notification adapter for the 15M short alert state machine."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import DirectionalAlertState
from app.db.session import db_session
from app.engines.short_alert_state import (
    AlertEvaluation,
    ShortAlertState,
    evaluate_short_alert,
)


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _load(symbol: str) -> ShortAlertState:
    with db_session() as db:
        row = db.execute(select(DirectionalAlertState).where(
            DirectionalAlertState.symbol == symbol)).scalar_one_or_none()
        if row is None:
            return ShortAlertState()
        status = "BEARISH_WATCH" if row.status == "SHORT_CONFIRMED" else row.status
        return ShortAlertState(status=status, level=row.level,
            invalidation_level=row.invalidation_level, zone_low=row.zone_low,
            zone_high=row.zone_high,
            created_at=row.created_at.isoformat() if row.created_at else "",
            last_closed_candle=row.last_closed_candle or "",
            last_closed_price=row.last_closed_price, last_event=row.last_event or "",
            generation=row.generation)


def _save(symbol: str, state: ShortAlertState) -> None:
    now = datetime.now(timezone.utc)
    with db_session() as db:
        row = db.execute(select(DirectionalAlertState).where(
            DirectionalAlertState.symbol == symbol)).scalar_one_or_none()
        if row is None:
            row = DirectionalAlertState(symbol=symbol, updated_at=now)
            db.add(row)
        row.status = state.status
        row.level = state.level
        row.invalidation_level = state.invalidation_level
        row.zone_low = state.zone_low
        row.zone_high = state.zone_high
        row.created_at = _parse_time(state.created_at)
        row.last_closed_candle = state.last_closed_candle
        row.last_closed_price = state.last_closed_price
        row.last_event = state.last_event
        row.generation = state.generation
        row.updated_at = now


def preview_short_alert(result: dict, entry_plan: dict | None = None) -> AlertEvaluation:
    normalized = result.get("normalized_analysis") or {}
    symbol = str(result.get("symbol") or "XAUUSD")
    return evaluate_short_alert(normalized, _load(symbol), entry_plan=entry_plan)


async def process_short_alert(result: dict, notifier,
                              entry_plan: dict | None = None) -> AlertEvaluation:
    normalized = result.get("normalized_analysis") or {}
    symbol = str(result.get("symbol") or "XAUUSD")
    prior = _load(symbol)
    evaluation = evaluate_short_alert(normalized, prior, entry_plan=entry_plan)
    if evaluation.state != prior:
        _save(symbol, evaluation.state)
    result["directional_alert"] = {
        "status": evaluation.state.status, "event_type": evaluation.event_type,
        "level": evaluation.state.level, "invalidation_level": evaluation.state.invalidation_level,
        "candle_close_time": evaluation.state.last_closed_candle,
        "message": evaluation.message, "blocked_reason": evaluation.blocked_reason,
    }
    if evaluation.should_notify and notifier:
        await notifier.notify("TRIGGER", evaluation.topic, evaluation.message,
                              severity="WARN", force_push=True, exact_once=True)
    return evaluation
