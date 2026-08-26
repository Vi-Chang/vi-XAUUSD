from datetime import datetime, timezone

from app.engines.close_gate import (
    build_close_gate,
    closed_candle_identity,
    next_candle_close_boundary,
)
from app.engines.decision_health import evaluate_defense
from app.engines.decision_presentation import format_decision_message
from app.services.semantic_decision import (
    build_decision_signature,
    detect_meaningful_transition,
)


def _wait(price: float, evaluated_at: str, target: str) -> dict:
    return {
        "symbol": "XAUUSD", "currentPrice": price,
        "currentState": "WAIT", "finalDecision": "WAIT",
        "calculatedAt": evaluated_at,
        "canonicalDecision": {
            "primaryAction": "WAIT", "entryConfirmation": "WAIT_15M_CLOSE",
            "waitReason": "WAIT_FOR_15M_CLOSE", "scenarioState": "ACTIVE",
            "marketBias": "BULLISH", "activeSetupId": "LONG-1",
            "activeStrategySide": "LONG",
            "closeGate": {
                "symbol": "XAUUSD", "timeframe": "15M",
                "targetCandleCloseTime": target, "strategyId": "LONG-1",
                "direction": "LONG", "status": "WAITING",
            },
        },
    }


def test_fixed_15m_close_boundaries():
    assert next_candle_close_boundary("2026-08-25T19:19:00+08:00").astimezone(
        timezone.utc) == datetime(2026, 8, 25, 11, 30, tzinfo=timezone.utc)
    assert next_candle_close_boundary("2026-08-25T19:34:00+08:00").astimezone(
        timezone.utc) == datetime(2026, 8, 25, 11, 45, tzinfo=timezone.utc)
    gate = build_close_gate(
        symbol="XAUUSD", evaluated_at="2026-08-25T19:19:00+08:00",
        strategy_id="LONG-1", direction="LONG",
        trigger_or_defense_reference=4645.50,
    )
    assert gate["targetCandleCloseTime"] == "2026-08-25T11:30:00+00:00"


def test_same_close_gate_wait_ignores_live_quote_changes():
    target = "2026-08-25T11:30:00+00:00"
    first = _wait(4644.29, "2026-08-25T11:19:00+00:00", target)
    second = _wait(4642.81, "2026-08-25T11:24:00+00:00", target)
    third = _wait(4644.10, "2026-08-25T11:29:00+00:00", target)
    assert build_decision_signature(first) == build_decision_signature(second)
    assert detect_meaningful_transition(first, second) is None
    assert detect_meaningful_transition(second, third) is None


def test_new_closed_candle_without_decision_change_stays_silent():
    before = _wait(4644.10, "2026-08-25T11:29:00+00:00",
                   "2026-08-25T11:30:00+00:00")
    after = _wait(4644.20, "2026-08-25T11:31:00+00:00",
                  "2026-08-25T11:45:00+00:00")
    assert detect_meaningful_transition(before, after) is None


def test_closed_candle_identity_is_stable_and_complete():
    value = closed_candle_identity(
        "XAUUSD", "15M", "2026-08-25T11:30:00+00:00")
    assert value == (
        "XAUUSD|15M|2026-08-25T11:15:00+00:00|"
        "2026-08-25T11:30:00+00:00")


def test_long_and_short_defense_are_directional():
    long_safe = evaluate_defense(
        side="LONG", current_price=4646.20, closed_candle=None,
        defense_level=4645.50, atr15=2.0)
    long_breach = evaluate_defense(
        side="LONG", current_price=4644.29, closed_candle=None,
        defense_level=4645.50, atr15=2.0)
    short_safe = evaluate_defense(
        side="SHORT", current_price=4644.29, closed_candle=None,
        defense_level=4645.50, atr15=2.0)
    short_breach = evaluate_defense(
        side="SHORT", current_price=4646.20, closed_candle=None,
        defense_level=4645.50, atr15=2.0)
    assert long_safe["state"] in {"SAFE", "APPROACHING"}
    assert long_breach["state"] == "INTRABAR_BREACH"
    assert short_safe["state"] in {"SAFE", "APPROACHING"}
    assert short_breach["state"] == "INTRABAR_BREACH"


def test_closed_candle_confirms_defense_breach():
    result = evaluate_defense(
        side="LONG", current_price=4644.29,
        closed_candle={"close": 4644.0}, defense_level=4645.50,
        confirmation_mode="CLOSED_CANDLE", atr15=2.0)
    assert result["state"] == "CONFIRMED_BREACH"


def test_stale_defense_binding_is_rejected():
    wrong_side = evaluate_defense(
        side="LONG", current_price=4644.29, closed_candle=None,
        defense_level=4645.50, active_strategy_id="LONG-1",
        defense_strategy_id="LONG-1", defense_side="SHORT")
    wrong_strategy = evaluate_defense(
        side="LONG", current_price=4644.29, closed_candle=None,
        defense_level=4645.50, active_strategy_id="LONG-2",
        defense_strategy_id="LONG-1", defense_side="LONG")
    assert wrong_side["state"] == "REJECT_STALE_DEFENSE"
    assert wrong_strategy["state"] == "REJECT_STALE_DEFENSE"


def test_wait_message_shows_exact_target_and_closed_candle():
    event = _wait(4644.29, "2026-08-25T11:19:00+00:00",
                  "2026-08-25T11:30:00+00:00")
    event["canonicalDecision"]["closedCandle"] = {
        "available": True, "open_time": "2026-08-25T11:00:00+00:00",
        "close_time": "2026-08-25T11:15:00+00:00", "close_price": 4644.80,
    }
    text = format_decision_message(event)
    assert "目前交易劇本：做多" in text
    assert "等待：15M 19:30 收盤" in text
    assert "19:00–19:15｜收盤 4644.80" in text
