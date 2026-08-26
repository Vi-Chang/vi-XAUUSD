"""Fixed-boundary closed-candle identity and WAIT_FOR_CLOSE contract."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def _utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def next_candle_close_boundary(value: str | datetime,
                               timeframe_minutes: int = 15) -> datetime:
    """Return the next fixed UTC boundary (:00/:15/:30/:45 for 15M)."""
    current = _utc(value)
    floored = current.replace(second=0, microsecond=0)
    remainder = floored.minute % timeframe_minutes
    boundary = floored - timedelta(minutes=remainder) + timedelta(minutes=timeframe_minutes)
    return boundary


def closed_candle_identity(symbol: str, timeframe: str,
                           close_time: str | datetime,
                           timeframe_minutes: int = 15) -> str:
    close = _utc(close_time)
    opened = close - timedelta(minutes=timeframe_minutes)
    return f"{symbol}|{timeframe}|{opened.isoformat()}|{close.isoformat()}"


def build_close_gate(*, symbol: str, evaluated_at: str | datetime,
                     strategy_id: str, direction: str,
                     trigger_or_defense_reference: float | None,
                     timeframe: str = "15M") -> dict[str, Any]:
    target = next_candle_close_boundary(evaluated_at, 15)
    return {
        "symbol": symbol, "timeframe": timeframe,
        "targetCandleCloseTime": target.isoformat(),
        "strategyId": strategy_id, "direction": direction,
        "triggerOrDefenseReference": trigger_or_defense_reference,
        "status": "WAITING",
    }
