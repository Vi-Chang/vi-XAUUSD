"""Aggregate anonymous rule-signal performance from persisted outcomes."""
from __future__ import annotations

from collections import defaultdict
from sqlalchemy import select
from app.db.models import AnalysisRun

HORIZONS = ("outcome_15m", "outcome_1h", "outcome_4h", "outcome_1d")
MIN_SAMPLE_SIZE = 30


def _direction(action: str) -> str | None:
    if action in ("LONG", "PREPARE_LONG"):
        return "LONG"
    if action in ("SHORT", "PREPARE_SHORT"):
        return "SHORT"
    return None


def _score_band(score: int) -> str:
    floor = max(0, min(90, (score // 10) * 10))
    return f"{floor}-{floor + 9}"


def _session(hour: int) -> str:
    if hour < 7:
        return "ASIA"
    if hour < 13:
        return "LONDON"
    if hour < 21:
        return "NEW_YORK"
    return "OFF_HOURS"


def _summary(values: list[float]) -> dict:
    n = len(values)
    return {"sample_size": n, "sufficient_sample": n >= MIN_SAMPLE_SIZE,
            "win_rate_pct": round(100 * sum(v > 0 for v in values) / n, 1) if n else None,
            "average_return_pct": round(sum(values) / n, 5) if n else None}


def performance_report(db, *, limit: int = 5000) -> dict:
    rows = db.execute(select(AnalysisRun).order_by(AnalysisRun.run_time.desc()).limit(limit)).scalars().all()
    buckets = {name: defaultdict(list) for name in
               ("overall", "direction", "score_band", "market_state", "session")}
    eligible = 0
    for row in rows:
        direction = _direction(row.decision_action)
        if direction is None:
            continue
        eligible += 1
        keys = {"overall": "ALL", "direction": direction,
                "score_band": _score_band(row.evidence_score),
                "market_state": row.market_state, "session": _session(row.run_time.hour)}
        for horizon in HORIZONS:
            value = getattr(row, horizon)
            if value is None:
                continue
            for group, key in keys.items():
                buckets[group][f"{key}|{horizon}"].append(float(value))
    groups = {}
    for group, entries in buckets.items():
        groups[group] = [{"key": combined.rsplit("|", 1)[0],
                          "horizon": combined.rsplit("|", 1)[1].removeprefix("outcome_"),
                          **_summary(values)}
                         for combined, values in sorted(entries.items())]
    return {"eligible_signals": eligible, "minimum_sample_size": MIN_SAMPLE_SIZE,
            "auto_tuning_enabled": False, "groups": groups}
