from datetime import datetime, timezone

from app.engines.short_alert_state import (
    ShortAlertState,
    evaluate_short_alert,
    validate_alert_zones,
)


def payload(state: str, *, level: float = 4400, price: float = 4395,
            atr: float = 10, closed: str = "2026-08-19T01:00:00+00:00",
            closed_price: float | None = 4394) -> dict:
    return {
        "supportState": state,
        "currentPrice": price,
        "atr15": atr,
        "lastClosedCandleTimestamp": closed,
        "lastClosedCandlePrice": closed_price,
        "confirmationLevels": [
            {"kind": "support", "timeframe": "15M", "price": level, "buffer": 2},
            {"kind": "resistance", "timeframe": "15M", "price": level + 30, "buffer": 2},
        ],
    }


def test_confirmed_state_does_not_regress_to_old_bullish_summary_state():
    first = evaluate_short_alert(payload("confirmed_breakdown"))
    assert first.state.status == "SHORT_CONFIRMED"
    later = evaluate_short_alert(payload("none", price=4390), first.state)
    assert later.state.status == "SHORT_CONFIRMED"
    assert later.should_notify is False


def test_intrabar_is_watch_and_closed_reclaim_invalidates_confirmed():
    watch = evaluate_short_alert(payload("intrabar_breach", closed="", closed_price=None))
    assert watch.state.status == "SHORT_WATCH"
    confirmed = evaluate_short_alert(payload("confirmed_breakdown"), watch.state)
    assert confirmed.state.status == "SHORT_CONFIRMED"
    invalidated = evaluate_short_alert(
        payload("failed_breakdown", price=4404, closed_price=4403), confirmed.state)
    assert invalidated.state.status == "SHORT_INVALIDATED"
    assert "【狀態】空方失效" in invalidated.message
    retained = evaluate_short_alert(payload("none", price=4405), invalidated.state)
    assert retained.state.status == "SHORT_INVALIDATED"


def test_new_lower_breakdown_replaces_expired_old_level():
    old = evaluate_short_alert(payload("confirmed_breakdown", level=4400)).state
    newer = evaluate_short_alert(
        payload("confirmed_breakdown", level=4370, price=4365,
                closed="2026-08-19T01:15:00+00:00", closed_price=4364), old)
    assert newer.should_notify is True
    assert newer.state.level == 4370
    assert newer.state.generation > old.generation
    assert "4400.00" not in newer.message


def test_zone_wider_than_one_and_half_atr_blocks_trade_alert():
    data = payload("confirmed_breakdown", atr=10)
    data["confirmationLevels"][0]["buffer"] = 8  # width 16 > 15
    assert "1.5" in validate_alert_zones(data)
    result = evaluate_short_alert(data)
    assert result.should_notify is False
    assert result.state.status == "NEUTRAL"


def test_same_event_direction_and_level_is_not_notified_twice():
    now = datetime(2026, 8, 19, 1, tzinfo=timezone.utc)
    first = evaluate_short_alert(payload("intrabar_breach", closed="", closed_price=None), now=now)
    duplicate = evaluate_short_alert(
        payload("intrabar_breach", price=4393, closed="", closed_price=None), first.state, now=now)
    assert first.should_notify is True
    assert duplicate.should_notify is False


def test_five_minute_volatility_cannot_create_direction():
    data = payload("none")
    data["fiveMinuteVolatilityExpanded"] = True
    result = evaluate_short_alert(data)
    assert result.state.status == "NEUTRAL"
    assert result.should_notify is False
