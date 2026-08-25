from app.engines.decision_presentation import format_decision_message
from app.engines.pullback_zone_semantics import (
    normalize_pullback_zones,
    validate_pullback_zone_order,
)
from app.services.semantic_decision import (
    build_decision_signature,
    detect_meaningful_transition,
)


def _wait(price: float, *, trigger_status: str = "NOT_CONFIRMED",
          rr: float = 1.46) -> dict:
    return {
        "symbol": "XAUUSD", "event_type": "DECISION_UPDATED",
        "currentPrice": price, "currentState": "WAIT_CONFIRMATION",
        "finalDecision": "WAIT", "marketBias": "BULLISH",
        "scenarioId": "SCN-1", "triggerStatus": trigger_status,
        "multiTimeframeBias": {
            "shortTermBias": "SHORT_TERM_BULLISH", "bias15m": "BULLISH",
            "bias1h": "BULLISH_CORRECTION", "bias4h": "BULLISH",
        },
        "canonicalDecision": {
            "primaryAction": "WAIT", "entryConfirmation": "WAIT_CONFIRMATION",
            "scenarioState": "WAIT_CONFIRMATION", "marketBias": "BULLISH",
            "primaryReason": "等待15M收盤確認", "minimumRR": 1.5,
            "canonicalNextTrigger": {
                "condition": "closeAbove", "status": trigger_status,
                "label": "15M 收盤站上 4642.53",
            },
            "newEntryDecision": {
                "action": "WAIT", "canEnter": False,
                "selectedSetup": {"estimatedRR": rr},
            },
        },
    }


def test_wait_price_changes_have_one_semantic_identity():
    events = [_wait(4637.90), _wait(4640.73), _wait(4639.61)]
    assert len({build_decision_signature(event) for event in events}) == 1
    assert detect_meaningful_transition(None, events[0]) == "FIRST_NOTIFICATION"
    assert detect_meaningful_transition(events[0], events[1]) is None
    assert detect_meaningful_transition(events[1], events[2]) is None


def test_closed_trigger_confirmation_is_meaningful():
    before = _wait(4640.0)
    after = _wait(4643.0, trigger_status="CONFIRMED")
    assert detect_meaningful_transition(before, after) == "TRIGGER_CONFIRMED"


def test_rr_only_notifies_when_threshold_state_changes():
    low = _wait(4640.0, rr=1.46)
    still_low = _wait(4640.0, rr=1.48)
    valid = _wait(4640.0, rr=1.52)
    assert detect_meaningful_transition(low, still_low) is None
    assert detect_meaningful_transition(still_low, valid) == "RR_BECAME_VALID"


def test_pullback_zones_are_labeled_by_reference_distance():
    zones = [
        {"type": "SHALLOW_PULLBACK",
         "entry_zone": {"lower": 4629.44, "upper": 4633.15}},
        {"type": "DEEP_PULLBACK",
         "entry_zone": {"lower": 4633.55, "upper": 4637.26}},
    ]
    result = normalize_pullback_zones("LONG", 4642.53, zones)
    assert validate_pullback_zone_order(result)
    assert result[0]["entry_zone"] == {"lower": 4633.55, "upper": 4637.26}
    assert result[0]["semanticPullbackType"] == "SHALLOW"
    assert result[-1]["entry_zone"] == {"lower": 4629.44, "upper": 4633.15}
    assert result[-1]["semanticPullbackType"] == "DEEP"


def test_direction_first_telegram_has_no_internal_nulls():
    event = _wait(4639.61)
    event["canonicalDecision"]["newEntryDecision"]["normalizedPullbackZones"] = [
        {"semanticPullbackType": "SHALLOW",
         "entry_zone": {"lower": 4633.55, "upper": 4637.26}},
        {"semanticPullbackType": "DEEP",
         "entry_zone": {"lower": 4629.44, "upper": 4633.15}},
    ]
    message = format_decision_message(event)
    assert message.splitlines()[0] == "🟡【XAUUSD｜短線偏多｜等突破確認】"
    assert "短線：🟢 偏多" in message
    assert "15M：🟢 偏多" in message
    assert "1H：🟠 多頭修正" in message
    assert "4H：🟢 偏多" in message
    assert message.index("淺回踩") < message.index("深度備案")
    for forbidden in ("None", "null", "undefined", "UNKNOWN_INTERNAL"):
        assert forbidden not in message
