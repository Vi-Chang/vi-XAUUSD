"""Best-effort market direction that is independent from entry permission."""
from __future__ import annotations


def _normalize(value) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"UP", "BULLISH", "STRONG_BULLISH", "LONG", "多", "偏多"}:
        return "BULLISH"
    if text in {"DOWN", "BEARISH", "STRONG_BEARISH", "SHORT", "空", "偏空"}:
        return "BEARISH"
    if text in {"RANGE", "NEUTRAL", "SIDEWAYS", "震盪", "盤整"}:
        return "NEUTRAL"
    return None


def resolve_market_direction(data: dict, previous: dict | None = None) -> dict:
    """Resolve direction through the required fallback hierarchy.

    A missing entry signal never turns a valid market direction into UNKNOWN.
    """
    previous = previous or {}
    normalized = data.get("normalized_analysis") or {}
    timeframes = data.get("timeframes") or {}
    bias = data.get("bias_analysis") or {}
    candidates = (
        (normalized.get("trendBias"), "CANONICAL_MARKET_STATE"),
        (normalized.get("marketRegime"), "CANONICAL_MARKET_STATE"),
        ((timeframes.get("h4") or {}).get("trend"), "LATEST_STRUCTURAL_STATE"),
        ((timeframes.get("h1") or {}).get("trend"), "LATEST_STRUCTURAL_STATE"),
        (bias.get("direction") or bias.get("bias"), "LATEST_DIRECTIONAL_BIAS"),
        (data.get("market_state"), "LATEST_DIRECTIONAL_BIAS"),
        (previous.get("marketDirection") or previous.get("direction"),
         "PREVIOUS_CONFIRMED_STATE"),
    )
    for raw, source in candidates:
        resolved = _normalize(raw)
        if resolved:
            return {"direction": resolved, "source": source, "available": True}
    return {"direction": "UNKNOWN", "source": "NO_VALID_DIRECTION", "available": False}
