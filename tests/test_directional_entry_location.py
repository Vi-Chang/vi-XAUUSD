from app.engines.decision_assistant import evaluate_decision_assistant
from app.engines.entry_location import classify_entry_location
from app.engines.final_decision_engine import evaluate_final_decision
from app.services.alert_aggregator import aggregate_signal_facts
from tests.test_decision_assistant import candle, data
from tests.test_final_decision_engine import market


def _short_case():
    value = data(price=4657.39, trend="bearish", regime="bearish")
    setup = value["breakout_setup_manager"]["activeSetup"]
    setup.update({
        "setupId": "SHORT-4651", "direction": "SHORT",
        "entryZoneLow": 4648.23, "entryZoneHigh": 4651.94,
        "maxChasePrice": 4645.00, "stopPrice": 4662.00,
        "tp1": 4640.00, "riskReward": 1.8,
    })
    value["normalized_analysis"]["timeframeAssessments"] = [
        {"timeframe": "4H", "trend": "bearish"},
        {"timeframe": "1H", "trend": "bearish"},
        {"timeframe": "15M", "trend": "neutral"},
    ]
    return value


def test_short_above_zone_is_wait_not_chase_or_entry_ready():
    result, events = evaluate_decision_assistant(
        _short_case(), latest_candle=candle(close=4657.39, opened=4656.8, high=4658))
    assert result["entryLocationState"] == "WAIT_BEARISH_RECONFIRMATION"
    assert result["tradeState"] == "SETUP_CONFIRMED"
    assert result["canEnter"] is False
    assert not any(event["event_type"] == "ENTRY_READY" for event in events)


def test_directional_chase_is_mirrored():
    assert classify_entry_location("LONG", 105, 100, 102, 104) == "CHASE_LONG"
    assert classify_entry_location("SHORT", 97, 100, 102, 98) == "CHASE_SHORT"
    assert classify_entry_location("SHORT", 105, 100, 102, 98) == "WAIT_BEARISH_RECONFIRMATION"


def test_final_decision_rejects_short_above_executable_zone():
    value = market(price=4657.39, regime="TREND_BEARISH")
    setup = value["breakout_setup_manager"]["activeSetup"]
    setup.update({
        "setupId": "SHORT-4651", "direction": "SHORT",
        "entryZoneLow": 4648.23, "entryZoneHigh": 4651.94,
        "maxChasePrice": 4645.00, "stopPrice": 4662.00,
        "tp1": 4640.00, "riskReward": 1.8,
    })
    value["decision_assistant"].update({
        "direction": "SHORT", "canEnter": False,
        "tradeState": "SETUP_CONFIRMED", "rewardRiskRatio": 1.8,
    })
    value["normalized_analysis"]["lastClosedCandlePrice"] = 4657.0
    decision, events = evaluate_final_decision(value)
    assert decision["finalAction"] == "WAIT"
    assert decision["primaryReason"] == "SETUP_CONFIRMED_WAIT_PRICE"
    assert not any(event["event_type"] == "ENTRY_READY" for event in events)


def test_one_canonical_alert_prefers_invalidation_over_entry_ready():
    facts = [{
        "evaluationCycleId": "cycle-1", "setupId": "S1",
        "currentState": "ENTRY_READY", "event_type": "ENTRY_READY",
    }, {
        "evaluationCycleId": "cycle-1", "setupId": "S1",
        "currentState": "INVALIDATED", "event_type": "SCENARIO_INVALIDATED",
    }]
    alerts = aggregate_signal_facts("XAUUSD", facts)
    assert len(alerts) == 1
    assert alerts[0]["currentState"] == "INVALIDATED"
    assert alerts[0]["factCount"] == 2


def test_long_take_profit_fact_never_becomes_short_entry():
    alerts = aggregate_signal_facts("XAUUSD", [{
        "evaluationCycleId": "cycle-tp", "tradePlanId": "LONG-1",
        "direction": "LONG", "currentState": "LONG_MANAGE",
        "event_type": "TAKE_PROFIT_1",
    }])
    assert len(alerts) == 1
    assert alerts[0]["alertCategory"] == "EXIT_WARNING"
    assert alerts[0]["event_type"] == "TAKE_PROFIT_1"
    assert alerts[0].get("direction") != "SHORT"
