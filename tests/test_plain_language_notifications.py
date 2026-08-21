from copy import deepcopy

import pytest

from app.engines.decision_presentation import (
    format_decision_message,
    plain_trade_status,
)
from app.engines.setup_lifecycle import evaluate_setup_lifecycle
from app.services.alert_aggregator import aggregate_signal_facts, semantic_key


def setup(status="WAIT_BREAKOUT_CONFIRMATION", *, price=4584.31, closed=4584.0):
    plan = {
        "setupId": "BO-4606", "direction": "LONG", "status": status,
        "breakoutTrigger": 4606.18, "entryZoneLow": 4606.18,
        "entryZoneHigh": 4609.88, "maxChasePrice": 4609.88,
        "retestZoneLow": 4598.0, "retestZoneHigh": 4603.0,
        "stopPrice": 4590.0, "tp1": 4620.0, "tp2": 4632.0, "tp3": 4650.0,
    }
    return {"eventId": f"event-{status}", "symbol": "XAUUSD", "setupId": plan["setupId"],
            "direction": "LONG", "currentState": status, "currentPrice": price,
            "latestClosedCandlePrice": closed,
            "candleCloseTime": "2026-08-21T14:00:00+00:00",
            "decisionBasisCandleCloseTime": "2026-08-21T14:00:00+00:00",
            "calculatedAt": "2026-08-21T14:01:00+00:00", "dataVersion": 2200,
            "breakoutSetupEvent": {"setupId": plan["setupId"], "currentState": status,
                                   "event_type": status, "setup": plan},
            "breakoutSetups": [plan]}


def test_before_breakout_is_waiting_not_missed():
    event = setup()
    message = format_decision_message(event)
    assert plain_trade_status(event["currentState"]) == "🟡 現在先不要進場"
    assert "收盤」站上 4606.18" in message
    assert "還沒到不代表錯過" in message
    assert "WAIT_" not in message


def test_intrabar_breakout_still_waits_for_closed_candle():
    message = format_decision_message(setup(price=4607.0, closed=4605.5))
    assert "現在先不要進場" in message
    assert "不是盤中瞬間碰到" in message


def test_closed_breakout_and_inside_zone_is_ready():
    message = format_decision_message(setup("ENTRY_READY_BREAKOUT", price=4607.2, closed=4607.0))
    assert message.startswith("🟢🟢【進場條件成立】")
    assert "方向：做多" in message and "建議進場區：4606.18–4609.88" in message
    assert "TP1：4620.00" in message


def test_above_chase_limit_waits_retest_with_exact_distance_and_zone():
    message = format_decision_message(setup("WAIT_RETEST", price=4612.3, closed=4608.0))
    assert "現在不要追" in message
    assert "離合理回踩區約 9.30" in message
    assert "4598.00–4603.00" in message


def test_retest_ready_is_explicit_entry_route():
    message = format_decision_message(setup("ENTRY_READY_RETEST", price=4601, closed=4602))
    assert "進場類型：回踩進場" in message
    assert "可以進場" in message


@pytest.mark.parametrize("state, expected", [
    ("INVALIDATED", "原本判斷已失效"),
    ("EXPIRED", "原本的進場條件已失效"),
])
def test_invalid_or_expired_setup_has_no_internal_status(state, expected):
    message = format_decision_message(setup(state))
    assert expected in message
    assert state not in message


def test_old_expired_and_new_setup_are_combined():
    event = setup("EXPIRED")
    newer = deepcopy(event["breakoutSetupEvent"]["setup"])
    newer.update({"setupId": "BO-4615", "status": "WAIT_BREAKOUT_CONFIRMATION",
                  "breakoutTrigger": 4615.0})
    event["breakoutSetups"].append(newer)
    message = format_decision_message(event)
    assert "舊條件：15 分鐘 K 棒收盤站上 4606.18" in message
    assert "新條件：15 分鐘 K 棒收盤站上 4615.00" in message


def test_missed_requires_prior_ready_and_sent_notification():
    base = {"previous": None, "setup_id": "S1", "direction": "LONG",
            "confirmation_price": 4600, "latest_closed_price": 4601,
            "closed_candle_time": "2026-08-21T14:00:00Z", "current_price": 4612,
            "entry_zone_low": 4600, "entry_zone_high": 4604,
            "risk_controls_passed": True, "calculated_at": "2026-08-21T14:01:00Z"}
    not_ready = evaluate_setup_lifecycle(**base)
    assert not_ready["state"] == "CONFIRMED_WAIT_RETEST"
    prior = {**not_ready, "state": "ENTRY_READY", "wasEntryReady": True,
             "entryNotificationSentAt": "2026-08-21T14:01:01Z"}
    missed = evaluate_setup_lifecycle(**{**base, "previous": prior})
    assert missed["state"] == "MISSED_ENTRY"


def test_no_position_language_is_conditional():
    event = {"currentState": "LONG_MANAGE", "currentPrice": 4620,
             "calculatedAt": "2026-08-21T14:01:00Z", "activeTradePlans": [{
                 "direction": "LONG", "entryZoneLow": 4606, "entryZoneHigh": 4609,
                 "currentR": 1.1, "tp1Price": 4620, "tp2Price": 4630, "tp3Price": 4640,
                 "trailingStopPrice": 4609, "earlyExitCondition": "15M 收盤跌破 4609",
                 "completedEvents": [],
             }]}
    message = format_decision_message(event)
    assert "若你持有多單" in message
    assert "你已持有" not in message


def test_same_fingerprint_dedupes_and_one_cycle_keeps_highest_priority():
    waiting = setup()
    ready = setup("ENTRY_READY_BREAKOUT", price=4607.2, closed=4607)
    waiting["evaluationCycleId"] = ready["evaluationCycleId"] = "cycle-1"
    waiting["eventId"], ready["eventId"] = "wait-event", "ready-event"
    grouped = aggregate_signal_facts("XAUUSD", [waiting, ready])
    assert len(grouped) == 1
    assert grouped[0]["currentState"] == "ENTRY_READY_BREAKOUT"
    assert grouped[0]["factCount"] == 2
    assert semantic_key(grouped[0]) == grouped[0]["semanticDedupKey"]
