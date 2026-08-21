"""Session and executable-cost context for XAUUSD decisions."""
from __future__ import annotations

from datetime import datetime, timezone


def market_session(timestamp: str | None) -> dict:
    try:
        now = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        now = datetime.now(timezone.utc)
    hour = now.astimezone(timezone.utc).hour
    if 12 <= hour < 16:
        name, weight = "LONDON_NEW_YORK_OVERLAP", 1.0
    elif 7 <= hour < 12:
        name, weight = "LONDON", .9
    elif 16 <= hour < 21:
        name, weight = "NEW_YORK", .9
    elif 0 <= hour < 7:
        name, weight = "ASIA", .7
    else:
        name, weight = "OFF_HOURS", .5
    return {"name": name, "qualityWeight": weight, "utcHour": hour}


def execution_cost(data: dict, *, slippage_abs: float) -> dict:
    price = data.get("current_price") or {}
    spread = max(0.0, float(price.get("spread") or 0))
    total = spread + max(0.0, slippage_abs)
    return {"spread": round(spread, 4), "estimatedSlippage": round(slippage_abs, 4),
            "estimatedRoundTripCost": round(total, 4), "currency": "USD_PER_OUNCE",
            "version": "execution-cost-v1"}
