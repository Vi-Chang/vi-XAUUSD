from datetime import datetime, timezone

from app.engines.short_alert_state import evaluate_short_alert, validate_alert_zones


def payload(state: str, *, level: float = 4490.27, price: float = 4485,
            atr: float = 10, closed: str = "2026-08-20T01:00:00+00:00",
            closed_price: float | None = 4484) -> dict:
    return {
        "supportState": state, "currentPrice": price, "atr15": atr,
        "lastClosedCandleTimestamp": closed, "lastClosedCandlePrice": closed_price,
        "confirmationLevels": [
            {"kind": "support", "timeframe": "15M", "price": level, "buffer": 2},
            {"kind": "resistance", "timeframe": "15M", "price": level + 30, "buffer": 2},
        ],
    }


def short_entry(status: str = "ENTRY_READY") -> dict:
    return {
        "status": status, "direction": "SHORT", "zone_low": 4488,
        "zone_high": 4491, "stop_loss": 4495, "take_profit_1": 4478,
        "take_profit_2": 4468, "risk_reward": 2.0,
    }


def test_breakdown_starts_non_terminal_bearish_watch_with_conditional_exit_advice():
    result = evaluate_short_alert(payload("confirmed_breakdown"))
    assert result.event_type == "BREAKDOWN_CONFIRMED"
    assert result.state.status == "BEARISH_WATCH"
    assert "若持有多單：建議出場或降低風險" in result.message


def test_full_breakdown_retest_and_entry_ready_sequence_without_duplicates():
    breakdown = evaluate_short_alert(payload("confirmed_breakdown"))
    duplicate = evaluate_short_alert(payload("confirmed_breakdown"), breakdown.state)
    retest = evaluate_short_alert(payload(
        "retest_rejected", closed="2026-08-20T01:15:00+00:00", closed_price=4487),
        breakdown.state)
    ready = evaluate_short_alert(payload(
        "retest_rejected", closed="2026-08-20T01:30:00+00:00", closed_price=4483),
        retest.state, entry_plan=short_entry())
    assert [breakdown.event_type, retest.event_type, ready.event_type] == [
        "BREAKDOWN_CONFIRMED", "RETEST_REJECTED", "SHORT_ENTRY_READY"]
    assert [breakdown.state.status, retest.state.status, ready.state.status] == [
        "BEARISH_WATCH", "BEARISH_WATCH", "SHORT_ENTRY_READY"]
    assert duplicate.should_notify is False
    assert len({breakdown.topic, retest.topic, ready.topic}) == 3
    assert "【進場區】4488.00–4491.00" in ready.message
    assert "【停損】4495.00" in ready.message
    assert "【分批止盈】4478.00／4468.00" in ready.message


def test_new_lower_close_emits_bearish_continuation():
    first = evaluate_short_alert(payload("confirmed_breakdown"))
    continuation = evaluate_short_alert(payload(
        "confirmed_breakdown", price=4478, closed="2026-08-20T01:15:00+00:00",
        closed_price=4477), first.state)
    assert continuation.event_type == "BEARISH_CONTINUATION"
    assert continuation.state.status == "BEARISH_WATCH"


def test_closed_reclaim_emits_false_breakout_and_cancels_watch():
    bearish = evaluate_short_alert(payload("confirmed_breakdown"))
    reclaimed = evaluate_short_alert(payload(
        "failed_breakdown", price=4494, closed="2026-08-20T01:15:00+00:00",
        closed_price=4493), bearish.state)
    assert reclaimed.event_type == "FALSE_BREAKOUT"
    assert reclaimed.state.status == "SHORT_INVALIDATED"


def test_event_topic_contains_type_level_and_candle_close_time():
    result = evaluate_short_alert(payload("confirmed_breakdown"))
    assert result.topic == (
        "bearish:BREAKDOWN_CONFIRMED:4490.27:2026-08-20T01:00:00+00:00")


def test_zone_wider_than_one_and_half_atr_blocks_trade_alert():
    data = payload("confirmed_breakdown", atr=10)
    data["confirmationLevels"][0]["buffer"] = 8
    assert "1.5" in validate_alert_zones(data)
    result = evaluate_short_alert(data)
    assert result.should_notify is False
    assert result.state.status == "NEUTRAL"


def test_five_minute_volatility_cannot_create_direction():
    data = payload("none")
    data["fiveMinuteVolatilityExpanded"] = True
    result = evaluate_short_alert(data)
    assert result.state.status == "NEUTRAL"
    assert result.should_notify is False


def test_intrabar_is_only_watch():
    now = datetime(2026, 8, 20, 1, tzinfo=timezone.utc)
    result = evaluate_short_alert(
        payload("intrabar_breach", closed="", closed_price=None), now=now)
    assert result.state.status == "SHORT_WATCH"
    assert result.event_type == "INTRABAR_BREACH"
