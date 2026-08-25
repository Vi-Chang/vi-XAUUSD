"""Additive data-health and defense-confirmation facts.

This module never chooses a trade direction.  It preserves the higher-timeframe
bias while separately deciding whether a fresh closed 15M candle is available
for a new entry and whether a runtime defense level has actually failed.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.utils.timeutils import iso_utc, parse_utc

DATA_HEALTH_STATES = {"HEALTHY", "DEGRADED_15M", "STALE", "RECOVERING"}
ENTRY_CONFIRMATION_STATES = {"READY", "WAIT_15M_CLOSE", "BLOCKED_BY_DATA"}
DEFENSE_STATES = {
    "APPROACHING", "TESTING", "HELD", "BROKEN_PENDING_CLOSE",
    "BROKEN_CONFIRMED", "INACTIVE",
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
        previous_health = str(previous.get("dataHealth") or "")
        health = ("RECOVERING" if previous_health in {"DEGRADED_15M", "STALE"}
                  else "HEALTHY")
        return {
            "dataHealth": health, "entryConfirmation": "READY",
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
) -> dict:
    """Classify a live defense test while requiring close confirmation to fail."""
    previous = previous or {}
    level = _number(defense_level)
    current = _number(current_price)
    direction = str(side).upper()
    if level is None or current is None or direction not in {"LONG", "SHORT"}:
        return {"defenseState": "INACTIVE", "defenseLevel": level,
                "falseBreakDetected": False, "longScenarioInvalidated": False,
                "shortScenarioInvalidated": False, "shortNow": False,
                "entryConfirmation": entry_confirmation}
    settings = get_settings()
    atr = max(_number(atr15) or 0.0, 0.01)
    buffer = atr * float(settings.defense_confirmation_buffer_atr_mult)
    approach = max(atr * float(settings.defense_approaching_atr_mult), current * 0.0001)
    broken_live = current < level if direction == "LONG" else current > level
    confirmed = (entry_confirmation == "READY" and
                 is_confirmed_break(level, direction, closed_context, buffer=buffer))
    old_state = str(previous.get("defenseState") or "")
    closed_price = _number((closed_context or {}).get("close"))
    closed_time = str((closed_context or {}).get("closeTime") or "")
    previous_basis_time = str(previous.get("defenseBasisCandleTime") or "")
    recovered = bool(
        old_state in {"TESTING", "BROKEN_PENDING_CLOSE"}
        and entry_confirmation == "READY" and closed_price is not None
        and closed_time and closed_time != previous_basis_time
        and ((direction == "LONG" and closed_price > level)
             or (direction == "SHORT" and closed_price < level))
    )
    if confirmed:
        state = "BROKEN_CONFIRMED"
    elif recovered:
        state = "HELD"
    elif broken_live:
        state = "BROKEN_PENDING_CLOSE"
    else:
        distance = abs(current - level)
        state = ("TESTING" if distance <= approach else
                 "APPROACHING" if distance <= approach * 2 else "INACTIVE")
    return {
        "defenseState": state, "defenseLevel": level,
        "confirmationBuffer": round(buffer, 6),
        "falseBreakDetected": recovered,
        "longScenarioInvalidated": state == "BROKEN_CONFIRMED" and direction == "LONG",
        "shortScenarioInvalidated": state == "BROKEN_CONFIRMED" and direction == "SHORT",
        # A defense failure cancels that scenario. It never grants the opposite entry.
        "shortNow": False, "side": direction,
        "defenseBasisCandleTime": closed_time,
        "entryConfirmation": ("WAIT_15M_CLOSE" if state in {
            "TESTING", "BROKEN_PENDING_CLOSE"} else entry_confirmation),
    }


def evaluate_decision_health(data: dict, *, previous: dict | None = None,
                             now: datetime | str | None = None) -> dict:
    closed = get_latest_valid_closed_15m(data, previous=previous, now=now)
    return {**closed, "marketBias": resolve_market_bias(data),
            "evaluatedAt": iso_utc(now or data.get("timestamp_utc") or
                                   datetime.now(timezone.utc))}
