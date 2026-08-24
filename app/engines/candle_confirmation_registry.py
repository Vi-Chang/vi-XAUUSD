"""Point-in-time closed-candle confirmation registry."""
from __future__ import annotations

from datetime import datetime


def _at_or_before(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return datetime.fromisoformat(left.replace("Z", "+00:00")) <= datetime.fromisoformat(
            right.replace("Z", "+00:00"))
    except ValueError:
        return False


def confirmation_record(*, symbol: str, timeframe: str, level: float,
                        direction: str, live_price: float | None,
                        last_closed_price: float | None,
                        candle_close_time: str, decision_timestamp: str) -> dict:
    above = str(direction).upper() in {"LONG", "ABOVE"}
    closed_cross = (last_closed_price is not None and
                    (last_closed_price > level if above else last_closed_price < level))
    live_cross = (live_price is not None and
                  (live_price > level if above else live_price < level))
    temporally_valid = _at_or_before(candle_close_time, decision_timestamp)
    if closed_cross and temporally_valid:
        status, confirmed_at = "CLOSED_CONFIRMED", candle_close_time
    elif live_cross:
        status, confirmed_at = "IN_PROGRESS", None
    else:
        status, confirmed_at = "NOT_REACHED", None
    side = "ABOVE" if above else "BELOW"
    return {
        "key": f"{symbol}:{timeframe}:{level:.2f}:{side}",
        "level": level, "direction": side, "timeframe": timeframe,
        "status": status, "confirmedAt": confirmed_at,
        "sourceCandleCloseTime": candle_close_time,
        "lastClosedCandleClose": last_closed_price, "livePrice": live_price,
        "decisionTimestamp": decision_timestamp,
        "temporalValid": temporally_valid,
    }


def build_confirmation_registry(*, symbol: str, candidates: list[dict],
                                live_price: float | None,
                                last_closed_price: float | None,
                                candle_close_time: str,
                                decision_timestamp: str) -> dict[str, dict]:
    records = {}
    for candidate in candidates:
        level = candidate.get("confirmationLevel")
        if not isinstance(level, (int, float)):
            continue
        record = confirmation_record(
            symbol=symbol, timeframe="15M", level=float(level),
            direction=str(candidate.get("direction") or "LONG"),
            live_price=live_price, last_closed_price=last_closed_price,
            candle_close_time=candle_close_time,
            decision_timestamp=decision_timestamp,
        )
        records[record["key"]] = record
    return records
