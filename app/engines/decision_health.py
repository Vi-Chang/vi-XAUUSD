"""Additive data-health and defense-confirmation facts.

This module never chooses a trade direction.  It preserves the higher-timeframe
bias while separately deciding whether a fresh closed 15M candle is available
for a new entry and whether a runtime defense level has actually failed.
"""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.utils.timeutils import iso_utc, parse_utc

DATA_HEALTH_STATES = {"HEALTHY", "DEGRADED", "DEGRADED_15M", "STALE"}
ENTRY_CONFIRMATION_STATES = {
    "READY", "WAIT_15M_CLOSE", "WAIT_NEW_STRUCTURE", "BLOCKED_BY_DATA",
}
DEFENSE_STATES = {
    "APPROACHING", "TESTING", "BROKEN_PENDING_CLOSE", "RECLAIMED", "HELD",
    "BROKEN_CONFIRMED", "INACTIVE",
}

logger = logging.getLogger(__name__)

TERMINAL_SCENARIO_STATES = {"INVALIDATED", "SCENARIO_INVALIDATED"}
CONFIRMED_EVENT_TYPES = {"DEFENSE_BROKEN_CONFIRMED"}
DEFENSE_TRANSITIONS = {
    "": DEFENSE_STATES,
    "INACTIVE": {"INACTIVE", "APPROACHING", "TESTING", "BROKEN_PENDING_CLOSE",
                 "BROKEN_CONFIRMED"},
    "APPROACHING": {"INACTIVE", "APPROACHING", "TESTING", "BROKEN_PENDING_CLOSE",
                    "BROKEN_CONFIRMED"},
    "TESTING": {"INACTIVE", "APPROACHING", "TESTING", "BROKEN_PENDING_CLOSE",
                "RECLAIMED", "BROKEN_CONFIRMED"},
    "BROKEN_PENDING_CLOSE": {"BROKEN_PENDING_CLOSE", "RECLAIMED",
                             "BROKEN_CONFIRMED"},
    "RECLAIMED": {"RECLAIMED", "HELD", "BROKEN_PENDING_CLOSE",
                  "BROKEN_CONFIRMED"},
    "HELD": {"HELD", "TESTING", "BROKEN_PENDING_CLOSE", "BROKEN_CONFIRMED"},
    # A confirmed break is immutable inside the same scenario lifecycle.
    "BROKEN_CONFIRMED": {"BROKEN_CONFIRMED"},
}


def is_allowed_scenario_transition(previous_state: str, next_state: str,
                                   *, scenario_terminal: bool = False) -> bool:
    """Return whether one scenario may move to ``next_state``.

    Price can move around before confirmation, but a confirmed break or an
    invalidated scenario is a one-way fact.  A reclaim after that fact belongs
    to a new scenario, never to the old lifecycle.
    """
    old, new = str(previous_state or ""), str(next_state or "")
    if scenario_terminal or old == "BROKEN_CONFIRMED":
        return new == "BROKEN_CONFIRMED"
    return new in DEFENSE_TRANSITIONS.get(old, {old})


def persist_confirmed_strategy_event(previous_events: list[dict] | None,
                                     event: dict | None) -> list[dict]:
    """Append one immutable confirmed event, deduplicated by event identity."""
    events = [dict(item) for item in (previous_events or [])]
    if not event:
        return events
    payload = dict(event)
    event_id = str(payload.get("eventId") or "")
    if not event_id:
        raw = "|".join(str(payload.get(key) or "") for key in (
            "scenarioId", "structureVersion", "eventType", "level",
            "closedBarTimestamp", "confirmedClose",
        ))
        event_id = hashlib.sha256(raw.encode()).hexdigest()[:32]
        payload["eventId"] = event_id
    if not any(str(item.get("eventId") or "") == event_id for item in events):
        events.append(payload)
    return events[-50:]


def _confirmed_break_event(events: list[dict], scenario_id: str,
                           structure_version: str) -> dict | None:
    for event in reversed(events):
        if (str(event.get("eventType")) in CONFIRMED_EVENT_TYPES
                and str(event.get("scenarioId") or "") == scenario_id
                and str(event.get("structureVersion") or "1") == structure_version):
            return event
    return None


def _structure_label(value: Any) -> str:
    raw = str(value or "").upper()
    if "BEAR" in raw or "DOWN" in raw:
        return "BEARISH"
    if "BULL" in raw or "UP" in raw:
        return "BULLISH"
    if "RANGE" in raw or "SIDE" in raw:
        return "RANGE"
    return "UNKNOWN"


def resolve_market_context(data: dict, *, htf_bias: str,
                           active_scenario_direction: str = "NONE") -> dict:
    """Keep HTF bias, 1H/15M structure and active scenario independent."""
    normalized = data.get("normalized_analysis") or {}
    assessments = {
        str(row.get("timeframe") or "").upper(): row
        for row in normalized.get("timeframeAssessments") or []
    }
    h1 = assessments.get("1H") or {}
    m15 = assessments.get("15M") or {}
    structure_1h = _structure_label(
        h1.get("structure") or h1.get("trend") or h1.get("momentum"))
    structure_15m = _structure_label(
        m15.get("structure") or m15.get("trend") or m15.get("momentum"))
    if htf_bias == "BULLISH" and structure_1h == "BEARISH":
        structure_1h = "BEARISH_CORRECTION"
    elif htf_bias == "BEARISH" and structure_1h == "BULLISH":
        structure_1h = "BULLISH_CORRECTION"
    short_term = (
        "CORRECTIVE_BEARISH"
        if htf_bias == "BULLISH" and (
            structure_1h == "BEARISH_CORRECTION" or structure_15m == "BEARISH")
        else "CORRECTIVE_BULLISH"
        if htf_bias == "BEARISH" and (
            structure_1h == "BULLISH_CORRECTION" or structure_15m == "BULLISH")
        else structure_15m
    )
    return {
        "htfBias": htf_bias,
        "structure1h": structure_1h,
        "structure15m": structure_15m,
        "shortTermState": short_term,
        "activeScenarioDirection": str(active_scenario_direction or "NONE").upper(),
    }


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def resolve_market_bias(data: dict) -> str:
    """Return an HTF-derived bias without consulting 15M availability."""
    normalized = data.get("normalized_analysis") or {}
    behavior = data.get("market_behavior_engine") or {}
    raw = str(behavior.get("market_bias") or normalized.get("trendBias") or "").upper()
    if "BULL" in raw:
        return "BULLISH"
    if "BEAR" in raw:
        return "BEARISH"
    votes: list[str] = []
    for row in normalized.get("timeframeAssessments") or []:
        if str(row.get("timeframe")) not in {"1D", "4H", "1H"}:
            continue
        trend = str(row.get("trend") or "").upper()
        if "BULL" in trend:
            votes.append("BULLISH")
        elif "BEAR" in trend:
            votes.append("BEARISH")
    if votes.count("BULLISH") > votes.count("BEARISH"):
        return "BULLISH"
    if votes.count("BEARISH") > votes.count("BULLISH"):
        return "BEARISH"
    return "NEUTRAL"


def _closed_snapshot(snapshot: dict | None) -> dict | None:
    snapshot = snapshot or {}
    close = _number(snapshot.get("close_price") if "close_price" in snapshot
                    else snapshot.get("close"))
    close_time = str(snapshot.get("close_time") or snapshot.get("closeTime") or "")
    if close is None or not close_time:
        return None
    return {
        "timeframe": "15M", "close": close, "closeTime": iso_utc(close_time),
        "source": str(snapshot.get("source") or ""),
    }


def get_latest_valid_closed_15m(
    data: dict, *, previous: dict | None = None, now: datetime | str | None = None,
) -> dict:
    """Separate a current confirmation candle from last-known-good context."""
    previous = previous or {}
    settings = get_settings()
    current_time = parse_utc(now or data.get("timestamp_utc") or
                             datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    explicit = (data.get("closed_candles") or {}).get("15M")
    current = _closed_snapshot(explicit) if explicit and explicit.get("available") else None
    # Backward-compatible callers without the canonical snapshot may still
    # provide a valid normalized closed candle. An explicitly unavailable
    # snapshot is never promoted to a new confirmation.
    normalized = data.get("normalized_analysis") or {}
    normalized_snapshot = _closed_snapshot({
        "close_price": normalized.get("lastClosedCandlePrice"),
        "close_time": normalized.get("lastClosedCandleTimestamp"),
        "source": "normalized_analysis",
    })
    if explicit is None:
        current = normalized_snapshot
    fallback = (_closed_snapshot(previous.get("lastKnownGoodClosed15m")) or
                normalized_snapshot)
    allowed = int(settings.closed_15m_context_max_staleness_seconds)

    def age_seconds(item: dict | None) -> float | None:
        stamp = parse_utc((item or {}).get("closeTime"))
        return max(0.0, (current_time - stamp).total_seconds()) if stamp else None

    current_age = age_seconds(current)
    if current and current_age is not None and current_age <= allowed:
        return {
            "dataHealth": "HEALTHY", "entryConfirmation": "READY",
            "latestClosed15m": current, "contextClosed15m": current,
            "lastKnownGoodClosed15m": current, "usingFallbackForContext": False,
            "allowedStalenessSeconds": allowed, "closedCandleAgeSeconds": current_age,
            "reason": "最新已收盤 15M 可用",
        }
    fallback_age = age_seconds(fallback)
    if fallback and fallback_age is not None and fallback_age <= allowed:
        return {
            "dataHealth": "DEGRADED_15M", "entryConfirmation": "WAIT_15M_CLOSE",
            "latestClosed15m": None, "contextClosed15m": fallback,
            "lastKnownGoodClosed15m": fallback, "usingFallbackForContext": True,
            "allowedStalenessSeconds": allowed, "closedCandleAgeSeconds": fallback_age,
            "reason": "最新 15M 收盤暫缺；保留最近有效收盤作背景，不用於新進場確認",
        }
    return {
        "dataHealth": "STALE", "entryConfirmation": "BLOCKED_BY_DATA",
        "latestClosed15m": None, "contextClosed15m": fallback,
        "lastKnownGoodClosed15m": fallback, "usingFallbackForContext": bool(fallback),
        "allowedStalenessSeconds": allowed, "closedCandleAgeSeconds": fallback_age,
        "reason": "沒有時效內的已收盤 15M，暫停新進場",
    }


def _canonical_health(value: object) -> str:
    state = str(value or "").upper()
    if state in {"DEGRADED", "DEGRADED_15M", "RECOVERING"}:
        return "DEGRADED"
    return state if state in {"HEALTHY", "STALE"} else ""


def _market_timestamp(data: dict) -> str:
    normalized = data.get("normalized_analysis") or {}
    current = data.get("current_price") or {}
    return iso_utc(
        normalized.get("marketDataTimestamp") or current.get("last_update") or
        data.get("snapshot_ts") or "")


def resolve_data_health_hysteresis(
    observation: dict, *, data: dict, previous: dict | None = None,
    now: datetime | str | None = None,
) -> dict:
    """Turn polling observations into durable health transitions.

    A successful response is only recovery evidence when its market timestamp
    advances and both the quote and closed-candle freshness checks pass.
    """
    previous = previous or {}
    settings = get_settings()
    evaluated = parse_utc(now or data.get("timestamp_utc") or
                          datetime.now(timezone.utc)) or datetime.now(timezone.utc)
    raw_health = _canonical_health(observation.get("dataHealth")) or "STALE"
    previous_health = _canonical_health(previous.get("canonicalDataHealth") or
                                        previous.get("dataHealth"))
    market_timestamp = _market_timestamp(data)
    closed_timestamp = str(((observation.get("latestClosed15m") or {}).get(
        "closeTime")) or "")
    market_dt = parse_utc(market_timestamp)
    market_age = (max(0.0, (evaluated - market_dt).total_seconds())
                  if market_dt else None)
    max_age = int(settings.closed_15m_context_max_staleness_seconds)
    explicit_15m = (data.get("closed_candles") or {}).get("15M")
    normalized = data.get("normalized_analysis") or {}
    observation_known = bool(
        explicit_15m is not None or
        (normalized.get("lastClosedCandlePrice") is not None and
         normalized.get("lastClosedCandleTimestamp")))
    api_ok = bool(data.get("api_ok", observation.get("latestClosed15m")))
    data_fresh = bool(api_ok and market_age is not None and market_age <= max_age)
    candle_fresh = bool(api_ok and
                        observation.get("closedCandleAgeSeconds") is not None and
                        float(observation["closedCandleAgeSeconds"]) <= max_age)
    previous_market = parse_utc(previous.get("lastObservedMarketTimestamp"))
    market_advanced = bool(market_dt and
                           (previous_market is None or market_dt > previous_market))
    recovery_evidence = bool(
        api_ok and data_fresh and candle_fresh and market_advanced)

    failure_count = int(previous.get("freshnessFailureCount") or 0)
    success_count = int(previous.get("freshnessSuccessCount") or 0)
    transition = "NONE"
    health = previous_health or raw_health

    if not observation_known:
        health = previous_health or "HEALTHY"
        transition = "INITIAL_TO_HEALTHY" if not previous_health else "NONE"
    elif not previous_health:
        health = raw_health
        failure_count = 0 if raw_health == "HEALTHY" else 1
        success_count = 0
        transition = f"INITIAL_TO_{health}"
    elif previous_health == "HEALTHY":
        success_count = 0
        if raw_health == "HEALTHY" and data_fresh and candle_fresh:
            failure_count = 0
        else:
            failure_count += 1
            if failure_count >= max(1, settings.data_health_degrade_confirm_count):
                health = "STALE" if raw_health == "STALE" else "DEGRADED"
                transition = f"HEALTHY_TO_{health}"
    else:
        if recovery_evidence:
            success_count += 1
            failure_count = 0
            if success_count >= max(1, settings.data_health_recovery_confirm_count):
                health = "HEALTHY"
                transition = f"{previous_health}_TO_HEALTHY"
                success_count = 0
        else:
            success_count = 0
            failure_count += 1
            if raw_health == "STALE" and previous_health != "STALE":
                health = "STALE"
                transition = f"{previous_health}_TO_STALE"

    incident_id = str(previous.get("dataIncidentId") or "")
    delay_notified = bool(previous.get("delayNotified"))
    recovery_notified = bool(previous.get("recoveryNotified"))
    entered_incident = transition in {
        "INITIAL_TO_DEGRADED", "INITIAL_TO_STALE",
        "HEALTHY_TO_DEGRADED", "HEALTHY_TO_STALE",
    }
    if entered_incident:
        incident_id = f"DATA-{evaluated.strftime('%Y%m%d-%H%M%S')}"
        delay_notified, recovery_notified = False, False

    health_event = None
    delay_confirmed = (
        entered_incident and not transition.startswith("INITIAL_") or
        (health != "HEALTHY" and bool(incident_id) and
         failure_count >= max(1, settings.data_health_degrade_confirm_count))
    )
    if delay_confirmed and not delay_notified:
        delay_notified = True
        health_event = {
            "event_type": "DATA_DELAYED", "dataIncidentId": incident_id,
            "dataHealthEventKey": f"DATA_DELAYED:{incident_id}",
            "previousDataHealth": previous_health or "UNKNOWN",
            "currentDataHealth": health, "currentState": health,
        }
    elif transition in {"DEGRADED_TO_HEALTHY", "STALE_TO_HEALTHY"} and not recovery_notified:
        recovery_notified = True
        health_event = {
            "event_type": "DATA_RECOVERED", "dataIncidentId": incident_id,
            "dataHealthEventKey": f"DATA_RECOVERED:{incident_id}",
            "previousDataHealth": previous_health, "currentDataHealth": "HEALTHY",
            "currentState": "HEALTHY", "closedBarTimestamp": closed_timestamp,
            "latestClosedCandlePrice": ((observation.get("latestClosed15m") or {}).get(
                "close")),
        }

    # During the recovery confirmation window, fresh data may be used as
    # context but never as a new-entry confirmation.
    entry_confirmation = str(observation.get("entryConfirmation") or "BLOCKED_BY_DATA")
    if health != "HEALTHY":
        entry_confirmation = ("BLOCKED_BY_DATA" if health == "STALE"
                              else "WAIT_15M_CLOSE")
    return {
        **observation,
        "dataHealth": health,
        "canonicalDataHealth": health,
        "observedDataHealth": raw_health,
        "entryConfirmation": entry_confirmation,
        "apiOk": api_ok, "dataFresh": data_fresh, "candleFresh": candle_fresh,
        "healthObservationKnown": observation_known,
        "marketTimestampAdvanced": market_advanced,
        "recoveryEvidenceAccepted": recovery_evidence,
        "freshnessFailureCount": failure_count,
        "freshnessSuccessCount": success_count,
        "lastObservedMarketTimestamp": market_timestamp,
        "lastClosed15mTimestamp": closed_timestamp or str(
            previous.get("lastClosed15mTimestamp") or ""),
        "dataIncidentId": incident_id,
        "delayNotified": delay_notified,
        "recoveryNotified": recovery_notified,
        "healthTransition": transition,
        "dataHealthEvent": health_event,
    }


def is_confirmed_break(level: float, side: str, closed_candle: dict | None,
                       *, buffer: float = 0.0) -> bool:
    """Confirm defense invalidation from a closed candle, never live price."""
    close = _number((closed_candle or {}).get("close") if closed_candle else None)
    if close is None:
        close = _number((closed_candle or {}).get("close_price") if closed_candle else None)
    direction = str(side).upper()
    if close is None:
        return False
    if direction in {"LONG", "BULLISH"}:
        return close < float(level) - max(0.0, buffer)
    if direction in {"SHORT", "BEARISH"}:
        return close > float(level) + max(0.0, buffer)
    return False


def evaluate_defense_state(
    *, defense_level: float | None, side: str, current_price: float | None,
    atr15: float = 0.0, closed_context: dict | None = None,
    entry_confirmation: str = "READY", previous: dict | None = None,
    reclaim_level: float | None = None, scenario_id: str = "",
    scenario_version: int = 1, structure_version: str = "1",
) -> dict:
    """Classify a live defense test while requiring close confirmation to fail."""
    previous = previous or {}
    scenario_id = str(scenario_id or previous.get("scenarioId") or "UNSCOPED")
    structure_version = str(structure_version or previous.get("structureVersion") or "1")
    previous_scenario_id = str(previous.get("scenarioId") or scenario_id)
    same_scenario = previous_scenario_id == scenario_id
    previous_scenario_state = str(previous.get("scenarioState") or "ACTIVE")
    scenario_terminal = bool(
        same_scenario and previous_scenario_state in TERMINAL_SCENARIO_STATES)
    confirmed_events = list(previous.get("confirmedStrategyEvents") or [])
    persisted_break = _confirmed_break_event(
        confirmed_events, scenario_id, structure_version)
    if persisted_break is not None:
        scenario_terminal = True
    level = _number(defense_level)
    current = _number(current_price)
    direction = str(side).upper()
    if level is None or current is None or direction not in {"LONG", "SHORT"}:
        if scenario_terminal and persisted_break is not None:
            level = _number(persisted_break.get("level"))
            direction = str(persisted_break.get("direction") or direction).upper()
            current = current if current is not None else level
        else:
            return {"defenseState": "INACTIVE", "defenseLevel": level,
                    "falseBreakDetected": False, "longScenarioInvalidated": False,
                    "shortScenarioInvalidated": False, "shortNow": False,
                    "activeLongScenario": "ACTIVE", "activeShortScenario": "ACTIVE",
                    "shortTermStructure": "STABLE", "searchNextScenario": False,
                    "nextScenarioCandidates": [], "scenarioId": scenario_id,
                    "scenarioVersion": scenario_version,
                    "structureVersion": structure_version,
                    "scenarioState": "ACTIVE", "confirmedStrategyEvents": confirmed_events,
                    "entryConfirmation": entry_confirmation}
    assert level is not None and current is not None
    settings = get_settings()
    atr = max(_number(atr15) or 0.0, 0.01)
    buffer = atr * float(settings.defense_confirmation_buffer_atr_mult)
    approach = max(atr * float(settings.defense_approaching_atr_mult), current * 0.0001)
    broken_live = current < level if direction == "LONG" else current > level
    confirmed = (entry_confirmation == "READY" and
                 is_confirmed_break(level, direction, closed_context, buffer=buffer))
    # A different scenario id is a fresh lifecycle boundary.  The historical
    # event ledger remains available, but its terminal defense state is not
    # copied into the new scenario.
    old_state = str(previous.get("defenseState") or "") if same_scenario else ""
    closed_price = _number((closed_context or {}).get("close"))
    closed_time = str((closed_context or {}).get("closeTime") or "")
    previous_basis_time = str(previous.get("defenseBasisCandleTime") or "")
    holds_defense = bool(
        closed_price is not None
        and ((direction == "LONG" and closed_price > level)
             or (direction == "SHORT" and closed_price < level))
    )
    first_reclaim = bool(
        old_state in {"TESTING", "BROKEN_PENDING_CLOSE"}
        and entry_confirmation == "READY" and closed_price is not None
        and closed_time and closed_time != previous_basis_time
        and holds_defense
    )
    continued_hold = bool(
        old_state == "RECLAIMED" and entry_confirmation == "READY"
        and closed_time and closed_time != previous_basis_time and holds_defense
    )
    same_reclaim_candle = bool(
        old_state == "RECLAIMED" and closed_time
        and closed_time == previous_basis_time and holds_defense
    )
    reclaim = _number(reclaim_level)
    reclaimed_structure = bool(
        first_reclaim and reclaim is not None and closed_price is not None
        and ((direction == "LONG" and closed_price > reclaim)
             or (direction == "SHORT" and closed_price < reclaim))
    )
    if scenario_terminal:
        # A later stale/live snapshot may describe an earlier market phase, but
        # it cannot revoke an already persisted closed-candle fact.
        state = "BROKEN_CONFIRMED"
    elif confirmed:
        state = "BROKEN_CONFIRMED"
    elif continued_hold or reclaimed_structure:
        state = "HELD"
    elif first_reclaim or same_reclaim_candle:
        state = "RECLAIMED"
    elif (old_state == "BROKEN_PENDING_CLOSE" and closed_time
          and closed_time == previous_basis_time):
        # Live price moving back above/below the line is not a closed-candle
        # reclaim. Keep the pending state until a newer 15M candle is final.
        state = "BROKEN_PENDING_CLOSE"
    elif broken_live:
        state = "BROKEN_PENDING_CLOSE"
    else:
        distance = abs(current - level)
        state = ("TESTING" if distance <= approach else
                 "APPROACHING" if distance <= approach * 2 else "INACTIVE")
    proposed_state = state
    if not is_allowed_scenario_transition(
            old_state, proposed_state, scenario_terminal=scenario_terminal):
        logger.warning(
            "STATE_REGRESSION_BLOCKED scenario=%s previous=%s proposed=%s",
            scenario_id, old_state, proposed_state,
        )
        state = "BROKEN_CONFIRMED" if (
            scenario_terminal or old_state == "BROKEN_CONFIRMED") else old_state
    scenario_broken = state == "BROKEN_CONFIRMED"
    confirmed_event = None
    if scenario_broken and persisted_break is None and confirmed and not scenario_terminal:
        confirmed_event = {
            "scenarioId": scenario_id,
            "scenarioVersion": scenario_version,
            "structureVersion": structure_version,
            "eventType": "DEFENSE_BROKEN_CONFIRMED",
            "level": level,
            "direction": direction,
            "closedBarTimestamp": closed_time,
            "confirmedClose": closed_price,
            "confirmedAt": closed_time or datetime.now(timezone.utc).isoformat(),
        }
        confirmed_events = persist_confirmed_strategy_event(
            confirmed_events, confirmed_event)
    false_break = bool(
        first_reclaim or continued_hold or same_reclaim_candle
        or (previous.get("falseBreakDetected") and state in {"RECLAIMED", "HELD"})
    )
    if entry_confirmation == "BLOCKED_BY_DATA":
        # Data health blocks new confirmation only.  It does not mutate the
        # already confirmed strategy timeline.
        resolved_confirmation = "BLOCKED_BY_DATA"
    elif state in {"TESTING", "BROKEN_PENDING_CLOSE"}:
        resolved_confirmation = "WAIT_15M_CLOSE"
    elif state in {"RECLAIMED", "BROKEN_CONFIRMED"}:
        resolved_confirmation = "WAIT_NEW_STRUCTURE"
    else:
        resolved_confirmation = entry_confirmation
    reclaim_after_terminal = bool(
        scenario_terminal and holds_defense and closed_time
        and closed_time != str((persisted_break or {}).get("closedBarTimestamp") or "")
    )
    proposed_new_scenario_id = ""
    reclaim_event: dict | None = None
    if reclaim_after_terminal:
        raw = f"{scenario_id}|RECLAIM|{closed_time}|{structure_version}"
        proposed_new_scenario_id = f"RECLAIM-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"
        reclaim_event = {
            "eventType": "NEW_RECLAIM_EVENT",
            "previousScenarioId": scenario_id,
            "newScenarioId": proposed_new_scenario_id,
            "historicalDefenseLevel": level,
            "closedBarTimestamp": closed_time,
            "confirmedClose": closed_price,
            "state": "WAIT_NEW_STRUCTURE",
            "requiresFullTradePlanRecalculation": True,
            # Explicitly empty: old executable prices are never copied.
            "entry": None, "stopLoss": None, "targets": [], "riskReward": None,
        }
    return {
        "defenseState": state, "defenseLevel": level,
        "confirmationBuffer": round(buffer, 6),
        "falseBreakDetected": false_break,
        "longScenarioInvalidated": scenario_broken and direction == "LONG",
        "shortScenarioInvalidated": scenario_broken and direction == "SHORT",
        "activeLongScenario": ("INVALIDATED" if scenario_broken and direction == "LONG"
                               else "ACTIVE"),
        "activeShortScenario": ("INVALIDATED" if scenario_broken and direction == "SHORT"
                                else "ACTIVE"),
        "shortTermStructure": ("CORRECTIVE" if scenario_broken else
                               "RECLAIMING" if state == "RECLAIMED" else
                               "STABLE" if state == "HELD" else
                               "TESTING" if state in {
                                   "APPROACHING", "TESTING", "BROKEN_PENDING_CLOSE"}
                               else "UNCHANGED"),
        "searchNextScenario": scenario_broken,
        "nextScenarioCandidates": (["DEEP_PULLBACK", "BREAKDOWN_RETEST"]
                                   if scenario_broken else []),
        "scenarioId": scenario_id, "scenarioVersion": scenario_version,
        "structureVersion": structure_version,
        "scenarioState": "INVALIDATED" if scenario_broken else "ACTIVE",
        "scenarioTerminal": scenario_broken,
        "canReopen": not scenario_broken,
        "confirmedStrategyEvents": confirmed_events,
        "confirmedStrategyEvent": confirmed_event,
        "historicalDefenseLevel": level if scenario_broken else None,
        "activeDefenseRole": "REFERENCE" if scenario_broken else "ACTIVE_DEFENSE",
        "reclaimEvent": reclaim_event,
        "pendingNewScenarioId": proposed_new_scenario_id,
        # A defense failure cancels that scenario. It never grants the opposite entry.
        "shortNow": False, "side": direction,
        "defenseBasisCandleTime": closed_time,
        "entryConfirmation": resolved_confirmation,
    }


def evaluate_decision_health(data: dict, *, previous: dict | None = None,
                             now: datetime | str | None = None) -> dict:
    previous = previous or {}
    observation = get_latest_valid_closed_15m(data, previous=previous, now=now)
    closed = resolve_data_health_hysteresis(
        observation, data=data, previous=previous, now=now)
    evaluated_at = iso_utc(now or data.get("timestamp_utc") or
                           datetime.now(timezone.utc))
    market_bias = resolve_market_bias(data)
    market_context = resolve_market_context(
        data, htf_bias=market_bias,
        active_scenario_direction=str(previous.get("side") or "NONE"))
    health_timeline = list(previous.get("dataHealthTimeline") or [])
    if (not health_timeline or
            str(health_timeline[-1].get("state") or "") != closed["dataHealth"]):
        health_timeline.append({
            "state": closed["dataHealth"], "at": evaluated_at,
            "reason": closed.get("reason"),
        })
    return {
        **closed, "marketBias": market_bias, "marketContext": market_context,
        "dataHealthTimeline": health_timeline[-50:],
        # The strategy event timeline is copied forward independently and may
        # only be appended by a closed-candle strategy confirmation.
        "confirmedStrategyEvents": list(
            previous.get("confirmedStrategyEvents") or []),
        "evaluatedAt": evaluated_at,
    }
