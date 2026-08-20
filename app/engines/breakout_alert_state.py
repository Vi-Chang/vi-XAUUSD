"""Close-driven bullish breakout lifecycle independent from touch cooldowns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class BreakoutAlertState:
    status: str = "NEUTRAL"
    zone_low: float | None = None
    zone_high: float | None = None
    breakout_time: str = ""
    confirmation_timeframe: str = "15M"
    consecutive_closes: int = 0
    last_candle: str = ""
    last_event: str = ""
    trend: str = "震盪"
    action: str = "等待回踩"


def evaluate_breakout_alert(
    normalized: dict,
    previous: BreakoutAlertState | None = None,
    *,
    h1_close: float | None = None,
    higher_low_broken: bool = False,
    macd_declining: bool = False,
) -> tuple[BreakoutAlertState, dict | None]:
    previous = previous or BreakoutAlertState()
    resistance = next(
        (
            x
            for x in normalized.get("confirmationLevels", [])
            if x.get("kind") == "resistance" and x.get("timeframe") == "15M"
        ),
        None,
    )
    if not resistance and previous.zone_high is None:
        return previous, None
    center = float(resistance["price"]) if resistance else float(previous.zone_high)
    buffer = float(resistance.get("buffer") or 0) if resistance else 0.0
    low, high = center - buffer, center + buffer
    price = float(normalized.get("currentPrice") or center)
    closed = normalized.get("lastClosedCandlePrice")
    candle = str(normalized.get("lastClosedCandleTimestamp") or "")
    event = ""
    state = previous
    if (
        price > high
        and (not isinstance(closed, (int, float)) or closed <= high)
        and previous.status == "NEUTRAL"
    ):
        state = BreakoutAlertState(
            "PENDING_BREAKOUT",
            low,
            high,
            str(normalized.get("marketDataTimestamp") or candle),
            "15M",
            0,
            candle,
            "",
            "多頭",
            "等待確認",
        )
        event = "PENDING_BREAKOUT"
    if (
        isinstance(closed, (int, float))
        and closed > high
        and candle != previous.last_candle
    ):
        count = previous.consecutive_closes + 1 if previous.zone_high == high else 1
        event = "BREAKOUT_CONFIRMED" if count == 1 else "BULLISH_CONTINUATION"
        if h1_close is not None and h1_close > high:
            event, count = "BULLISH_CONTINUATION", max(2, count)
        state = BreakoutAlertState(
            "BULLISH_CONTINUATION" if count >= 2 else "BREAKOUT_CONFIRMED",
            low,
            high,
            previous.breakout_time or candle,
            "15M",
            count,
            candle,
            event,
            "多頭",
            "等待回踩",
        )
    elif (
        previous.status in ("BREAKOUT_CONFIRMED", "BULLISH_CONTINUATION")
        and isinstance(closed, (int, float))
        and closed < low
        and candle != previous.last_candle
    ):
        truly_weak = higher_low_broken and macd_declining
        event = "BREAKOUT_FAILED" if truly_weak else "BREAKOUT_RETEST"
        state = replace(
            previous,
            status=event,
            last_candle=candle,
            last_event=event,
            action="突破失敗" if truly_weak else "等待回踩",
        )
    if not event:
        return state, None
    return state, {
        "event_type": event,
        "zone_low": low,
        "zone_high": high,
        "candle_close_time": candle,
        "trend": state.trend,
        "action": state.action,
        "topic": f"breakout:{event}:{high:.2f}:{candle}",
        "message": (
            f"價格已突破候選壓力 {high:.2f}，等待 15M 收盤確認。"
            if event == "PENDING_BREAKOUT"
            else f"突破成立：15M 收盤站上 {high:.2f}，等待回踩。"
            if event == "BREAKOUT_CONFIRMED"
            else f"多頭延續確認：價格連續站穩 {high:.2f}。"
            if event == "BULLISH_CONTINUATION"
            else f"突破區 {low:.2f}–{high:.2f} 回測中，尚未構成短線轉弱。"
            if event == "BREAKOUT_RETEST"
            else f"突破失敗：結構、MACD 與收盤共同確認跌回 {low:.2f} 下方。"
        ),
    }


def breakout_view(state: BreakoutAlertState, event: dict | None) -> dict:
    return {**asdict(state), "event": event or {}}
