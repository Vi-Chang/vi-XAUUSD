"""Low-cost production invariants for prices, stops and market events."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from app.utils.timeutils import parse_utc


def validate_quote(*, bid: float, ask: float) -> None:
    if not all(math.isfinite(value) and value > 0 for value in (bid, ask)):
        raise ValueError("bid/ask 必須是有限正數")
    if ask < bid:
        raise ValueError("ask 不得低於 bid")


def normalized_event_time(value: str | datetime) -> datetime:
    parsed = parse_utc(value)
    if parsed is None:
        raise ValueError("行情事件缺少有效 UTC 時間")
    return parsed.astimezone(timezone.utc)


def validate_trade_prices(direction: str, *, entry: float, stop: float,
                          targets: list[float] | tuple[float, ...] = ()) -> None:
    values = (entry, stop, *targets)
    if not all(math.isfinite(float(value)) and float(value) > 0 for value in values):
        raise ValueError("進場、停損與目標價必須是有限正數")
    if direction == "LONG":
        if stop >= entry:
            raise ValueError("多單停損必須低於進場價")
        if any(target <= entry for target in targets):
            raise ValueError("多單止盈必須高於進場價")
    elif direction == "SHORT":
        if stop <= entry:
            raise ValueError("空單停損必須高於進場價")
        if any(target >= entry for target in targets):
            raise ValueError("空單止盈必須低於進場價")
    else:
        raise ValueError("交易方向必須是 LONG 或 SHORT")


def validate_stop_update(direction: str, *, previous_stop: float | None,
                         new_stop: float) -> None:
    if not math.isfinite(new_stop) or new_stop <= 0:
        raise ValueError("新停損必須是有限正數")
    if previous_stop is None:
        return
    if direction == "LONG" and new_stop < previous_stop:
        raise ValueError("多單停損不得往下放寬")
    if direction == "SHORT" and new_stop > previous_stop:
        raise ValueError("空單停損不得往上放寬")
