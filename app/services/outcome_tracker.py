"""Backfill forward returns for persisted analysis decisions."""
from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import AnalysisRun, Candle

HORIZONS = {
    "outcome_15m": timedelta(minutes=15),
    "outcome_1h": timedelta(hours=1),
    "outcome_4h": timedelta(hours=4),
    "outcome_1d": timedelta(days=1),
}


def signed_return_pct(action: str, entry: float, current: float) -> float | None:
    if entry <= 0 or action not in ("LONG", "SHORT", "PREPARE_LONG", "PREPARE_SHORT"):
        return None
    direction = 1 if action.endswith("LONG") else -1
    return round(direction * (current - entry) / entry * 100.0, 5)


def excursion_pct(action: str, entry: float, highs: list[float], lows: list[float]) -> tuple[float, float] | None:
    """Return direction-adjusted maximum favorable/adverse excursion in percent."""
    if signed_return_pct(action, entry, entry) is None or not highs or not lows:
        return None
    direction = 1 if action.endswith("LONG") else -1
    favorable_prices = highs if direction > 0 else lows
    adverse_prices = lows if direction > 0 else highs
    favorable = max(direction * (price - entry) / entry * 100 for price in favorable_prices)
    adverse = min(direction * (price - entry) / entry * 100 for price in adverse_prices)
    return round(favorable, 5), round(adverse, 5)


def _entry_price(row: AnalysisRun) -> float | None:
    payload = row.result_json or {}
    value = (payload.get("current_price") or {}).get("mid")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def first_close_at_or_after(candles: list[tuple[datetime, float]],
                            target: datetime) -> float | None:
    """Return the first closed-candle price at/after target, never a live price."""
    if not candles:
        return None
    times = [item[0] for item in candles]
    index = bisect_left(times, _utc(target))
    return candles[index][1] if index < len(candles) else None


def backfill_outcomes(db, *, now: datetime, current_price: float | None = None) -> int:
    """Recompute each horizon from its own first closed 15M candle.

    ``current_price`` remains accepted for call-site compatibility but is
    deliberately unused: one late live quote must not populate several
    horizons with the same value.
    """
    oldest = now - max(HORIZONS.values()) - timedelta(hours=1)
    rows = db.execute(
        select(AnalysisRun).where(AnalysisRun.run_time >= oldest)
        .order_by(AnalysisRun.run_time.asc()).limit(1000)
    ).scalars().all()
    candle_rows = db.execute(
        select(Candle.close_time, Candle.high, Candle.low, Candle.close, Candle.received_at)
        .where(Candle.symbol == "XAUUSD", Candle.timeframe == "15M",
               Candle.is_closed.is_(True), Candle.close_time >= oldest)
        .order_by(Candle.close_time.asc(), Candle.received_at.desc())
    ).all()
    # Providers may store several versions of one candle.  The query orders
    # newest received first, so setdefault keeps the newest version per close.
    by_time: dict[datetime, tuple[float, float, float]] = {}
    for close_time, high, low, close, _received_at in candle_rows:
        by_time.setdefault(_utc(close_time), (float(high), float(low), float(close)))
    candles = sorted(by_time.items())

    updated = 0
    for row in rows:
        entry = _entry_price(row)
        if signed_return_pct(row.decision_action, entry or 0.0, entry or 0.0) is None:
            continue
        run_time = _utc(row.run_time)
        age = now - run_time
        for field, horizon in HORIZONS.items():
            if age < horizon:
                continue
            closes = [(stamp, values[2]) for stamp, values in candles]
            close = first_close_at_or_after(closes, run_time + horizon)
            value = signed_return_pct(row.decision_action, entry or 0.0, close or 0.0)
            if value is not None and getattr(row, field) != value:
                setattr(row, field, value)
                updated += 1
            if close is not None:
                reached = next((stamp for stamp, values in candles
                                if values[2] == close and stamp >= run_time + horizon), None)
                path = [values for stamp, values in candles if reached and run_time < stamp <= reached]
                excursion = excursion_pct(row.decision_action, entry or 0.0,
                                          [item[0] for item in path], [item[1] for item in path])
                if excursion is not None:
                    payload = dict(row.result_json or {})
                    outcome_path = dict(payload.get("outcome_path") or {})
                    outcome_path[field] = {"mfe_pct": excursion[0], "mae_pct": excursion[1]}
                    payload["outcome_path"] = outcome_path
                    row.result_json = payload
    return updated
