"""Classify canonical disagreements without mistaking market divergence for bugs."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

CONFLICT_TYPES = {
    "NO_CONFLICT", "TIMEFRAME_DIVERGENCE", "BIAS_TRANSITION",
    "DATA_VERSION_MISMATCH", "STALE_ENGINE_RESULT",
    "DATA_DEGRADED_CONDITION", "SCORE_NEAR_TIE",
    "CANONICAL_INVARIANT_VIOLATION", "TRUE_ENGINE_CONFLICT",
}


def _text(value: Any) -> str:
    return str(value or "")


def _side(value: Any) -> str:
    text = _text(value).upper()
    if any(token in text for token in ("LONG", "BULL")):
        return "LONG"
    if any(token in text for token in ("SHORT", "BEAR")):
        return "SHORT"
    return "NEUTRAL"


def _closed(data: dict, timeframe: str) -> tuple[str, str]:
    closed = dict((data.get("closed_candles") or {}).get(timeframe) or {})
    close_time = _text(closed.get("close_time"))
    candle_id = _text(closed.get("candle_id") or closed.get("id"))
    if not candle_id and close_time:
        candle_id = f"{timeframe}:{close_time}"
    return candle_id, close_time


def build_canonical_market_snapshot(data: dict) -> dict:
    """Freeze the market identity used by every engine in one evaluation."""
    normalized = dict(data.get("normalized_analysis") or {})
    symbol = _text(data.get("symbol") or "XAUUSD")
    price = normalized.get("currentPrice")
    price_time = _text(normalized.get("marketDataTimestamp") or
                       (data.get("current_price") or {}).get("last_update") or
                       data.get("timestamp_utc"))
    ids, times = {}, {}
    for timeframe in ("15M", "1H", "4H"):
        ids[timeframe], times[timeframe] = _closed(data, timeframe)
    if not times["15M"]:
        times["15M"] = _text(normalized.get("lastClosedCandleTimestamp"))
        ids["15M"] = f"15M:{times['15M']}" if times["15M"] else ""
    data_version = int(data.get("version") or 0)
    health = _text((data.get("data_health") or {}).get("status") or
                   normalized.get("marketDataStatus") or "STALE").upper()
    if health in {"GOOD", "HEALTHY", "OK"}:
        health = "DATA_OK"
    elif health in {"STALE", "DISCONNECTED"}:
        health = "DATA_STALE"
    elif health in {"FAILED", "INVALID"}:
        health = "DATA_INVALID"
    else:
        health = "DATA_DEGRADED"
    missing_core = []
    if not isinstance(price, (int, float)):
        missing_core.append("PRICE")
    if not price_time:
        missing_core.append("PRICE_TIMESTAMP")
    if not times["15M"]:
        missing_core.append("CLOSED_15M")
    missing_context = [name for name in ("1H", "4H") if not times[name]]
    if missing_core and health in {"DATA_STALE", "DATA_INVALID"}:
        completeness = "INCOMPLETE"
    elif missing_core or missing_context or health != "DATA_OK":
        completeness = "DEGRADED"
    else:
        completeness = "COMPLETE"
    identity = "|".join((symbol, str(data_version), price_time,
                         times["15M"], times["1H"], times["4H"]))
    snapshot_id = "CMS-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    return {
        "schemaVersion": "canonical-market-snapshot-v1",
        "snapshotId": snapshot_id, "snapshot_id": snapshot_id,
        "symbol": symbol,
        "evaluationTimestamp": _text(data.get("timestamp_utc") or
                                     datetime.now(timezone.utc).isoformat()),
        "price": price, "priceTimestamp": price_time,
        "lastClosed15mId": ids["15M"], "lastClosed15mTime": times["15M"],
        "lastClosed1hId": ids["1H"], "lastClosed1hTime": times["1H"],
        "lastClosed4hId": ids["4H"], "lastClosed4hTime": times["4H"],
        "dataVersion": data_version, "marketDataHealth": health,
        "snapshotCompleteness": completeness,
        "missingCore": missing_core, "missingContext": missing_context,
    }


def engine_result_envelope(engine: str, result: dict, snapshot: dict,
                           engine_version: str = "v1") -> dict:
    return {
        "engine": engine, "snapshotId": snapshot.get("snapshotId"),
        "marketStateVersion": snapshot.get("dataVersion"),
        "evaluationTimestamp": snapshot.get("evaluationTimestamp"),
        "engineVersion": engine_version, "result": result,
    }


def stamp_engine_result(result: dict, snapshot: dict, *, engine: str,
                        engine_version: str = "v1") -> dict:
    stamped = dict(result)
    stamped.update({
        "snapshotId": snapshot.get("snapshotId"),
        "marketStateVersion": snapshot.get("dataVersion"),
        "evaluationTimestamp": snapshot.get("evaluationTimestamp"),
        "engineVersion": engine_version, "evidenceEngine": engine,
    })
    return stamped


def _timeframe_state(decision: dict) -> dict:
    source = dict(decision.get("multiTimeframeBias") or {})
    return {timeframe: _text(source.get(field) or "UNKNOWN") for timeframe, field in (
        ("15M", "bias15m"), ("1H", "bias1h"),
        ("4H", "bias4h"), ("1D", "bias1d"))}


def _is_timeframe_divergence(states: dict) -> bool:
    sides = {_side(value) for value in states.values()} - {"NEUTRAL"}
    return len(sides) > 1


def _is_bias_transition(decision: dict) -> bool:
    structural = _side(decision.get("structuralBias"))
    live = _side(decision.get("liveMomentum"))
    live_state = _text(decision.get("liveBiasState")).upper()
    return ((structural != "NEUTRAL" and live != "NEUTRAL" and structural != live)
            or live_state in {"INVALIDATING", "REVERSAL_CANDIDATE", "SUSPENDED"})


def _normalize_stale_nested_results(decision: dict, errors: list[str]) -> tuple[dict, list[str]]:
    """Forced deterministic recompute for stale nested setup projections."""
    result = dict(decision)
    remaining = list(errors)
    selected = dict((result.get("newEntryDecision") or {}).get("selectedSetup") or {})
    selected_id = _text(selected.get("setupId"))
    if "ACTIVE_SETUP_SELECTION_CONFLICT" in remaining and selected_id:
        result["activeSetupId"] = selected_id
        remaining.remove("ACTIVE_SETUP_SELECTION_CONFLICT")
    if "ENGINE_CANONICAL_SETUP_CONFLICT" in remaining:
        result["engineSelectedSetupId"] = result.get("activeSetupId")
        remaining.remove("ENGINE_CANONICAL_SETUP_CONFLICT")
    if "NEXT_TRIGGER_SETUP_CONFLICT" in remaining:
        trigger = dict(result.get("canonicalNextTrigger") or {})
        trigger.update({"setupId": result.get("activeSetupId"), "status": "PENDING",
                        "label": "舊觸發已淘汰，等待目前劇本的下一個有效條件"})
        result["canonicalNextTrigger"] = trigger
        result["primaryNextTrigger"] = trigger
        remaining.remove("NEXT_TRIGGER_SETUP_CONFLICT")
    if "COMPLETED_TRIGGER_EXPOSED_AS_NEXT" in remaining:
        trigger = {
            "setupId": result.get("activeSetupId"), "timeframe": "15M",
            "condition": "waitForNewStructure", "status": "PENDING",
            "source": "CLOSED_CANDLE", "label": "原條件已完成，等待新結構形成",
        }
        result["canonicalNextTrigger"] = trigger
        result["primaryNextTrigger"] = trigger
        remaining.remove("COMPLETED_TRIGGER_EXPOSED_AS_NEXT")
    if "MULTIPLE_EXECUTABLE_SETUPS" in remaining:
        alternatives = []
        for item in result.get("alternativeSetups") or []:
            alternatives.append({**item, "canEnter": False, "executionAllowed": False})
        result["alternativeSetups"] = alternatives
        remaining.remove("MULTIPLE_EXECUTABLE_SETUPS")
    return result, remaining


def resolve_canonical_conflict(
    decision: dict, *, market_snapshot: dict, previous: dict | None = None,
    engine_results: list[dict] | None = None, consistency_errors: list[str] | None = None,
) -> dict:
    """Return one classified, safe Canonical Strategy Snapshot."""
    result = dict(decision)
    previous = dict(previous or {})
    engine_results = list(engine_results or [])
    errors = list(consistency_errors or [])
    snapshot_id = _text(market_snapshot.get("snapshotId"))
    version_mismatch = [item for item in engine_results
                        if (_text(item.get("snapshotId")) == snapshot_id and
                            int(item.get("marketStateVersion") or -1) != int(
                                market_snapshot.get("dataVersion") or 0))]
    discarded = [item for item in engine_results
                 if (_text(item.get("snapshotId")) != snapshot_id or
                     item in version_mismatch)]
    accepted = [item for item in engine_results
                if (_text(item.get("snapshotId")) == snapshot_id and
                    item not in version_mismatch)]
    data_health = _text(market_snapshot.get("marketDataHealth"))
    completeness = _text(market_snapshot.get("snapshotCompleteness"))
    data_blocking = (completeness == "INCOMPLETE" or
                     data_health in {"DATA_STALE", "DATA_INVALID"})
    timeframe_state = _timeframe_state(result)
    long_score = int(result.get("longScore") or 0)
    short_score = int(result.get("shortScore") or 0)
    resolver_outputs = {_side(item) for item in result.get("resolverOutputs") or []}
    resolver_outputs.discard("NEUTRAL")
    recompute_attempted = False
    recompute_result = "NOT_REQUIRED"

    result, remaining_errors = _normalize_stale_nested_results(result, errors)
    if len(resolver_outputs) > 1:
        recompute_attempted = True
        recomputed = _side(result.get("executionBias"))
        recompute_result = ("RESOLVED_AFTER_RECOMPUTE" if recomputed in resolver_outputs
                            else "STILL_CONFLICTING")
    true_conflict = len(resolver_outputs) > 1 and recompute_result == "STILL_CONFLICTING"

    if true_conflict:
        conflict_type = "TRUE_ENGINE_CONFLICT"
    elif remaining_errors:
        conflict_type = "CANONICAL_INVARIANT_VIOLATION"
    elif version_mismatch:
        conflict_type = "DATA_VERSION_MISMATCH"
    elif discarded:
        conflict_type = "STALE_ENGINE_RESULT"
    elif _is_bias_transition(result):
        conflict_type = "BIAS_TRANSITION"
    elif _is_timeframe_divergence(timeframe_state):
        conflict_type = "TIMEFRAME_DIVERGENCE"
    elif long_score and short_score and abs(long_score-short_score) <= 3:
        conflict_type = "SCORE_NEAR_TIE"
    elif completeness != "COMPLETE" or data_health != "DATA_OK":
        conflict_type = "DATA_DEGRADED_CONDITION"
    else:
        conflict_type = "NO_CONFLICT"

    last_confirmed = _side(previous.get("lastConfirmedBias") or
                           previous.get("executionBias") or previous.get("marketBias"))
    if last_confirmed == "NEUTRAL":
        last_confirmed = _side(result.get("executionBias") or result.get("marketBias"))
    if (data_blocking or conflict_type in {
            "SCORE_NEAR_TIE", "TRUE_ENGINE_CONFLICT",
            "CANONICAL_INVARIANT_VIOLATION"}):
        entry = dict(result.get("newEntryDecision") or {})
        entry.update({"action": "WAIT", "canEnter": False,
                      "tradeStatus": ("WAIT_DATA_CONFIRMATION"
                                      if data_blocking
                                      else "WAIT_CONFIRMATION")})
        result.update({"primaryAction": "WAIT", "executionAllowed": False,
                       "canEnter": False, "newEntryDecision": entry})
    if conflict_type == "SCORE_NEAR_TIE":
        result["executionBias"] = "NEUTRAL"
    live_state = _text(result.get("liveBiasState")).upper()
    bias_transition_state = (
        "BIAS_CONFIRMED_FLIP" if result.get("marketBiasChanged") else
        "BIAS_TRANSITION" if conflict_type == "BIAS_TRANSITION" else
        "BIAS_INVALIDATING" if live_state in {"INVALIDATING", "SUSPENDED"} else
        "BIAS_WEAKENING" if live_state in {"WEAKENING", "REVERSAL_CANDIDATE"} else
        "BIAS_ALIGNED")
    result.update({
        "schemaVersion": "canonical-strategy-snapshot-v1",
        "snapshotId": snapshot_id, "canonicalMarketSnapshot": market_snapshot,
        "canonicalStateVersion": result.get("decisionVersion", 0),
        "timeframeState": timeframe_state, "longScore": long_score,
        "shortScore": short_score,
        "canonicalDataHealthState": data_health,
        "snapshotCompleteness": completeness, "conflictType": conflict_type,
        "biasTransitionState": bias_transition_state,
        "lastConfirmedBias": last_confirmed, "allowLong": bool(
            result.get("executionAllowed") and _side(result.get("executionBias")) == "LONG"),
        "allowShort": bool(
            result.get("executionAllowed") and _side(result.get("executionBias")) == "SHORT"),
        "tradePermission": ("BLOCKED_DATA" if data_blocking
                            else "BLOCKED_SYSTEM" if conflict_type in {
                                "TRUE_ENGINE_CONFLICT", "CANONICAL_INVARIANT_VIOLATION"}
                            else "WATCH" if not result.get("executionAllowed") else "ALLOWED"),
        "conflictReasonTrace": {
            "snapshotId": snapshot_id,
            "canonicalStateVersion": result.get("decisionVersion", 0),
            "conflictType": conflict_type, "timeframeState": timeframe_state,
            "structuralBias": result.get("structuralBias"),
            "liveBias": result.get("liveMomentum"),
            "executionBias": result.get("executionBias"),
            "longScore": long_score, "shortScore": short_score,
            "dataHealth": data_health,
            "engineResults": accepted,
            "discardedEngineResults": discarded,
            "consistencyErrors": remaining_errors,
            "resolutionAction": ("DISCARD_STALE_RESULT" if discarded else
                                 "BLOCK_NEW_ENTRY" if conflict_type in {
                                     "TRUE_ENGINE_CONFLICT", "CANONICAL_INVARIANT_VIOLATION"}
                                 else "CLASSIFIED_MARKET_STATE"),
            "recomputeAttempted": recompute_attempted,
            "recomputeResult": recompute_result,
        },
    })
    return result
