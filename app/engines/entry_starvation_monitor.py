"""Rolling diagnostics for entry-opportunity starvation; never relaxes gates."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings


def _time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _summary(rows: list[dict]) -> dict:
    decisions = Counter(str(row.get("state") or "WATCH") for row in rows)
    evaluations = [evaluation for row in rows
                   for evaluation in row.get("candidateEvaluations") or []]
    hard = Counter(code for evaluation in evaluations
                   for code in evaluation.get("hardBlocks") or [])
    soft = Counter(item.get("code") for evaluation in evaluations
                   for item in evaluation.get("softFilters") or [] if item.get("code"))
    rejection_total = sum(hard.values()) or 1
    penalty_total = sum(soft.values()) or 1
    return {
        "candidateCount": sum(int(row.get("candidateCount") or 0) for row in rows),
        "entryReadyCount": decisions["ENTRY_READY"],
        "probeReadyCount": decisions["PROBE_READY"],
        "watchCount": decisions["WATCH"], "blockedCount": decisions["BLOCKED"],
        "topBlockingReasons": hard.most_common(5),
        "topSoftPenalties": soft.most_common(5),
        "gateRejectionStatistics": [
            {"code": code, "count": count,
             "percent": round(count / rejection_total * 100, 1)}
            for code, count in hard.most_common()],
        "softPenaltyStatistics": [
            {"code": code, "count": count,
             "percent": round(count / penalty_total * 100, 1)}
            for code, count in soft.most_common()],
    }


def evaluate_entry_starvation(decision: dict, *, previous: dict | None = None,
                              evaluated_at: str | None = None) -> tuple[dict, list[dict]]:
    settings = get_settings()
    previous = previous or {}
    now = _time(evaluated_at or decision.get("evaluatedAt"))
    gate = decision.get("entryOpportunityGate") or {}
    selected = gate.get("selected") or {}
    observation = {
        "at": now.isoformat(), "state": str(gate.get("entryState") or "WATCH"),
        "candidateCount": len(gate.get("candidateEvaluations") or []),
        "hardBlocks": list(selected.get("hardBlocks") or []),
        "softFilters": list(selected.get("softFilters") or []),
        "longScore": gate.get("longScore", 0), "shortScore": gate.get("shortScore", 0),
        "scenarioId": selected.get("scenarioId"),
        "candidateEvaluations": list(gate.get("candidateEvaluations") or []),
    }
    observations = [row for row in previous.get("observations") or []
                    if _time(row.get("at")) >= now-timedelta(hours=24)]
    # Re-evaluation of one timestamp updates the diagnostic snapshot instead
    # of inflating funnel counts.
    observations = [row for row in observations if row.get("at") != observation["at"]]
    observations.append(observation)
    windows = {}
    for label, hours in (("1h", 1), ("3h", 3), ("6h", 6), ("session", 12)):
        rows = [row for row in observations if _time(row.get("at")) >= now-timedelta(hours=hours)]
        windows[label] = _summary(rows)
    one_hour = windows["1h"]
    starved = bool(
        one_hour["candidateCount"] >= settings.entry_starvation_min_candidates and
        one_hour["entryReadyCount"] + one_hour["probeReadyCount"] == 0)
    daily = _summary(observations)
    daily["starvation"] = bool(
        daily["candidateCount"] > 10 and
        daily["entryReadyCount"] + daily["probeReadyCount"] == 0)
    state = {
        "schemaVersion": "entry-starvation-monitor-v1", "evaluatedAt": now.isoformat(),
        "starvationWarning": starved, "windows": windows, "dailyEntryFunnel": daily,
        "observations": observations[-1000:],
        "thresholdPolicy": "DIAGNOSTIC_ONLY_NEVER_RELAX_SAFETY",
    }
    events = []
    if starved and not previous.get("starvationWarning"):
        events.append({
            "event_type": "ENTRY_STARVATION_WARNING", "currentState": "STARVATION",
            "previousState": "NORMAL", "notificationRoute": "LOG_ONLY",
            "transitionReason": "持續存在候選機會，但沒有 ENTRY_READY 或 PROBE_READY",
            "topBlockingReasons": one_hour["topBlockingReasons"],
            "topSoftPenalties": one_hour["topSoftPenalties"],
        })
    return state, events
