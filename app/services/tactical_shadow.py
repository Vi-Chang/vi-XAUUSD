"""Build and identify paper-only tactical signals without changing live advice."""
from __future__ import annotations

from typing import Any

from app.schemas.analysis import NormalizedAnalysisState, TacticalShadowRecord

ACTIONABLE = ("LONG", "SHORT", "PREPARE_LONG", "PREPARE_SHORT")
SHADOW_STATES = ("LONG_WATCH", "SHORT_WATCH", "LONG_READY", "SHORT_READY", "NO_CHASE")


def build_tactical_shadow(
    normalized: NormalizedAnalysisState, *, current_price: float, created_at: str, settings: Any
) -> TacticalShadowRecord:
    state = normalized.setupState
    if state.startswith("LONG"):
        direction = "LONG"
    elif state.startswith("SHORT"):
        direction = "SHORT"
    elif state == "NO_CHASE" and normalized.tacticalBias in ("bullish", "bearish"):
        direction = "LONG" if normalized.tacticalBias == "bullish" else "SHORT"
    else:
        direction = "NONE"
    return TacticalShadowRecord(
        setupState=state,
        direction=direction,
        referencePrice=current_price if current_price > 0 else None,
        triggerLevel=normalized.triggerLevel,
        invalidationLevel=normalized.invalidationLevel,
        expiresAt=normalized.expiresAt,
        createdAt=created_at,
        eligibleForOutcome=state in SHADOW_STATES and direction != "NONE",
        parameters={
            "shortChaseAtrMultiplier": settings.tactical_short_chase_atr_mult,
            "minimumRiskReward": settings.tactical_min_rr,
            "setupExpiryBars": settings.tactical_setup_expiry_bars,
            "breakoutBufferAtrMultiplier": settings.breakout_close_buffer_atr_mult,
        },
    )


def outcome_action(row: Any) -> str | None:
    """Actual advice has priority; otherwise return an eligible shadow direction."""
    if row.decision_action in ACTIONABLE:
        return row.decision_action
    shadow = (row.result_json or {}).get("tactical_shadow") or {}
    if not shadow.get("enabled") or not shadow.get("eligibleForOutcome"):
        return None
    direction = shadow.get("direction")
    return direction if direction in ("LONG", "SHORT") else None


def signal_mode(row: Any) -> str | None:
    action = outcome_action(row)
    if action is None:
        return None
    return "LIVE" if row.decision_action in ACTIONABLE else "SHADOW"


def shadow_setup_state(row: Any) -> str:
    shadow = (row.result_json or {}).get("tactical_shadow") or {}
    return str(shadow.get("setupState") or "LIVE")
