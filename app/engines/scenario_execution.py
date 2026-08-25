"""Scenario validity and the single fail-closed execution gate.

This additive layer deliberately does not infer market direction.  It answers
only whether one already-produced scenario is still valid and executable.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

SCENARIO_VALIDITY_STATES = {
    "ACTIVE", "PENDING_CONFIRMATION", "STALE", "INVALIDATED",
    "BLOCKED_BY_DATA",
}
TERMINAL_LIFECYCLES = {"INVALIDATED", "EXPIRED", "ARCHIVED", "MISSED"}


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def candidate_crossed_invalidation(
    *, direction: str, current_price: float | None,
    invalidation_price: float | None,
) -> bool:
    """Use a directional comparison; never use absolute distance."""
    current = _number(current_price)
    invalidation = _number(invalidation_price)
    side = str(direction).upper()
    if current is None or invalidation is None:
        return False
    if side == "LONG":
        return current <= invalidation
    if side == "SHORT":
        return current >= invalidation
    return False


def candidate_is_fresh(*, lifecycle_state: str, expires_at: str | None,
                       evaluated_at: str | None) -> bool:
    if str(lifecycle_state).upper() in TERMINAL_LIFECYCLES:
        return False
    if not expires_at or not evaluated_at:
        return True
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        evaluated = datetime.fromisoformat(str(evaluated_at).replace("Z", "+00:00"))
        return evaluated <= expiry
    except ValueError:
        # An unparsable expiry is not allowed to grant execution.
        return False


def resolve_scenario_validity(
    *, direction: str, current_price: float | None,
    invalidation_price: float | None, lifecycle_state: str,
    data_health: str, entry_confirmation: str,
    expires_at: str | None = None, evaluated_at: str | None = None,
) -> dict:
    """Classify validity without changing the higher-timeframe market bias."""
    health = str(data_health).upper()
    confirmation = str(entry_confirmation).upper()
    lifecycle = str(lifecycle_state).upper()
    fresh = candidate_is_fresh(
        lifecycle_state=lifecycle, expires_at=expires_at, evaluated_at=evaluated_at)
    crossed = candidate_crossed_invalidation(
        direction=direction, current_price=current_price,
        invalidation_price=invalidation_price)

    if health not in {"HEALTHY", "RECOVERING"} or confirmation == "BLOCKED_BY_DATA":
        validity, reason = "BLOCKED_BY_DATA", "DATA_NOT_EXECUTABLE"
    elif crossed or lifecycle in {"INVALIDATED", "ARCHIVED", "MISSED"}:
        validity, reason = "INVALIDATED", (
            "CANDIDATE_INVALIDATION_CROSSED" if crossed else "LIFECYCLE_INVALIDATED")
    elif not fresh or lifecycle == "EXPIRED":
        validity, reason = "STALE", "CANDIDATE_EXPIRED"
    elif confirmation != "READY" or lifecycle not in {"ENTRY_READY", "CONFIRMED"}:
        validity, reason = "PENDING_CONFIRMATION", "CONFIRMATION_INCOMPLETE"
    else:
        validity, reason = "ACTIVE", "VALID"
    return {
        "scenarioValidity": validity,
        "validityReason": reason,
        "candidateInvalidated": crossed,
        "scenarioInvalidated": validity in {"INVALIDATED", "STALE"},
        # Scenario invalidation is intentionally orthogonal to market bias.
        "marketBiasChanged": False,
        "candidateFresh": fresh,
    }


def can_execute_scenario(
    *, direction: str, current_price: float | None,
    invalidation_price: float | None, lifecycle_state: str,
    data_health: str, entry_confirmation: str,
    closed_candle_confirmed: bool, in_executable_zone: bool,
    risk_valid: bool, rr_valid: bool, stop_valid: bool,
    expires_at: str | None = None, evaluated_at: str | None = None,
) -> dict:
    """Return the one auditable permission used by API, UI and notifications."""
    validity = resolve_scenario_validity(
        direction=direction, current_price=current_price,
        invalidation_price=invalidation_price, lifecycle_state=lifecycle_state,
        data_health=data_health, entry_confirmation=entry_confirmation,
        expires_at=expires_at, evaluated_at=evaluated_at)
    checks = {
        "dataHealth": str(data_health).upper() in {"HEALTHY", "RECOVERING"},
        "entryConfirmation": str(entry_confirmation).upper() == "READY",
        "scenarioValidity": validity["scenarioValidity"] == "ACTIVE",
        "closedCandleConfirmation": bool(closed_candle_confirmed),
        "inExecutableZone": bool(in_executable_zone),
        "riskValidation": bool(risk_valid),
        "rrValidation": bool(rr_valid),
        "candidateFreshness": bool(validity["candidateFresh"]),
        "stopValidation": bool(stop_valid),
    }
    blocked = [name for name, passed in checks.items() if not passed]
    return {
        **validity, "executionAllowed": not blocked,
        "checks": checks, "blockedReasons": blocked,
    }
