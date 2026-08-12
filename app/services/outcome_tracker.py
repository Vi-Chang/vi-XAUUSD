"""Backfill forward returns for persisted analysis decisions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import AnalysisRun


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


def _entry_price(row: AnalysisRun) -> float | None:
    payload = row.result_json or {}
    value = (payload.get("current_price") or {}).get("mid")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def backfill_outcomes(db, *, now: datetime, current_price: float) -> int:
    """Fill each due horizon once, using the first later analysis price observed."""
    oldest = now - max(HORIZONS.values()) - timedelta(hours=1)
    rows = db.execute(
        select(AnalysisRun).where(AnalysisRun.run_time >= oldest)
        .order_by(AnalysisRun.run_time.asc()).limit(1000)
    ).scalars().all()
    updated = 0
    for row in rows:
        entry = _entry_price(row)
        value = signed_return_pct(row.decision_action, entry or 0.0, current_price)
        if value is None:
            continue
        run_time = row.run_time
        if run_time.tzinfo is None:
            run_time = run_time.replace(tzinfo=timezone.utc)
        age = now - run_time
        for field, horizon in HORIZONS.items():
            if getattr(row, field) is None and age >= horizon:
                setattr(row, field, value)
                updated += 1
    return updated
