from app.engines.unified_decision_state import (
    enforce_scenario_consistency,
    evaluate_unified_decision,
)
from app.schemas.analysis import Scenario


def payload(price, *, status="NO_SETUP", market_status="GOOD", tracker=None):
    return {
        "version": 7,
        "timestamp_utc": "2026-08-20T14:00:00+00:00",
        "market_decision": {"action": "WATCH", "reason": "等待結構確認"},
        "entry_engine": {"status": status, "direction": "NONE"},
        "virtual_profit_tracker": tracker or {},
        "normalized_analysis": {
            "currentPrice": price,
            "marketDataTimestamp": "2026-08-20T14:00:00+00:00",
            "lastClosedCandleTimestamp": "2026-08-20T13:45:00+00:00",
            "marketDataStatus": market_status,
            "consistencyValid": True,
            "atr15": 10,
            "confirmationLevels": [
                {"kind": "support", "timeframe": "15M", "price": 4480},
                {"kind": "resistance", "timeframe": "15M", "price": 4490},
            ],
        },
    }


def test_4450_to_4481_to_4495_emits_rebound_reclaim_and_wait_close():
    state, _ = evaluate_unified_decision(payload(4450))
    state, events = evaluate_unified_decision(payload(4481), state)
    kinds = [event["event_type"] for event in events]
    assert "PRICE_REBOUND" in kinds
    assert "KEY_LEVEL_RECLAIMED" in kinds
    state, events = evaluate_unified_decision(payload(4495), state)
    kinds = [event["event_type"] for event in events]
    assert "AWAIT_CLOSE_CONFIRMATION" in kinds
    assert "15 分鐘收盤站上 4490.00" in state["confirmation"]


def test_wait_forces_both_scenarios_out_of_executable_status():
    long, short = enforce_scenario_consistency(
        "WAIT", Scenario(status="PREPARE"), Scenario(status="TRIGGERED")
    )
    assert long.status == short.status == "WATCH"


def test_first_target_has_position_management_and_flat_no_chase():
    tracker = {"active": True, "direction": "LONG", "events": [{"event_type": "TP1"}]}
    state, events = evaluate_unified_decision(payload(4500, tracker=tracker))
    assert state["state"] == "LONG_MANAGE"
    assert "禁止追價" in state["flat_action"]
    assert any(event["event_type"] == "FIRST_TARGET_REACHED" for event in events)


def test_new_quote_updates_source_price_and_quote_metadata():
    state, _ = evaluate_unified_decision(payload(4450))
    newer = payload(4481)
    newer["normalized_analysis"]["marketDataTimestamp"] = "2026-08-20T14:01:00+00:00"
    state, _ = evaluate_unified_decision(newer, state)
    assert state["source_price"] == 4481
    assert state["quote_time"].endswith("14:01:00+00:00")


def test_stale_data_is_never_ready():
    state, events = evaluate_unified_decision(
        payload(4495, status="ENTRY_TRIGGERED", market_status="STALE")
    )
    assert state["state"] == "DATA_STALE"
    assert state["action"] == "暫停交易"
    assert any(event["event_type"] == "DATA_STALE" for event in events)


def test_rehydrated_same_state_and_price_does_not_repeat_notification():
    state, _ = evaluate_unified_decision(payload(4481))
    _same, events = evaluate_unified_decision(payload(4481), state)
    assert events == []


def test_ready_is_downgraded_when_cost_adjusted_rr_is_too_low():
    data = payload(100, status="ENTRY_TRIGGERED")
    data["entry_engine"].update({"direction": "LONG", "suggested_entry": 100,
                                  "stop_loss": 99, "take_profit_1": 101.6})
    data["current_price"] = {"spread": 0.5}
    data["normalized_analysis"]["eventDataStatus"] = "GOOD"
    state, events = evaluate_unified_decision(data)
    assert state["state"] == "LONG_WATCH"
    assert any(event["executionCosts"]["netRiskReward"] < 1.5 for event in events)


def test_failed_event_data_caps_confidence_without_erasing_direction():
    data = payload(100, status="SETUP_WATCH")
    data["entry_engine"].update({"direction": "LONG", "confidence_score": 90})
    data["normalized_analysis"]["eventDataStatus"] = "FAILED"
    state, _ = evaluate_unified_decision(data)
    assert state["state"] == "LONG_BIAS"
    assert state["confidence"] == 55


def test_completed_breakout_is_not_reused_and_short_defense_is_triggered():
    data = payload(4520.91, status="SETUP_WATCH")
    data["entry_engine"]["direction"] = "LONG"
    data["normalized_analysis"].update({
        "lastClosedCandlePrice": 4520.50,
        "confirmationLevels": [
            {"kind": "support", "timeframe": "15M", "price": 4497.02},
            {"kind": "resistance", "timeframe": "15M", "price": 4495.12},
        ],
    })
    data["hypothetical_exit_advisor"] = {"plans": {
        "LONG": {"defense_price": 4449.56},
        "SHORT": {"defense_price": 4497.02},
    }}
    data["breakout_alert"] = {"event": {"event_type": "BULLISH_CONTINUATION"}}
    state, events = evaluate_unified_decision(data)
    completed = next(item for item in state["triggers"] if item["level"] == 4495.12)
    assert completed["status"] == "SATISFIED"
    assert state["next_trigger"] is None
    assert "等待新結構" in state["confirmation"]
    assert "4497.02 防守條件已觸發" in state["short_manage"]
    assert any(event["event_type"] == "SHORT_DEFENSE_TRIGGERED" for event in events)
    assert all((event.get("nextTriggerCondition") or {}).get("level") != 4495.12
               for event in events)


def test_active_virtual_position_never_tells_flat_user_to_enter_again():
    data = payload(4515, status="ENTRY_TRIGGERED", tracker={
        "active": True, "direction": "SHORT", "events": []
    })
    data["entry_engine"].update({
        "direction": "SHORT", "suggested_entry": 4515.26, "stop_loss": 4520,
        "take_profit_1": 4506.75, "take_profit_2": 4502.26,
        "take_profit_3": 4480.16, "risk_reward": 1.8,
        "missing_condition": "",
    })
    state, _ = evaluate_unified_decision(data)
    assert state["state"] == "SHORT_MANAGE"
    assert "禁止追價" in state["flat_action"]
    assert "進場" not in state["action"]


def test_triggered_with_missing_confirmation_is_downgraded_to_watch():
    data = payload(4515, status="ENTRY_TRIGGERED")
    data["entry_engine"].update({
        "direction": "SHORT", "suggested_entry": 4515, "stop_loss": 4520,
        "take_profit_1": 4505, "risk_reward": 2,
        "missing_condition": "尚缺反轉 K 線",
    })
    state, _ = evaluate_unified_decision(data)
    assert state["state"] == "SHORT_WATCH"
    assert state["flat_action"] == "等待，尚不可進場，請勿追價。"


def closed_breakout_case(*, current_price=4529.96, closed_price=4530.0):
    data = payload(current_price, status="ENTRY_TRIGGERED")
    data["entry_engine"].update({
        "direction": "LONG", "suggested_entry": 4528, "stop_loss": 4520,
        "take_profit_1": 4548, "take_profit_2": 4560,
        "risk_reward": 2.5, "missing_condition": "",
        "zone_low": 4527, "zone_high": 4529,
        "cancel_condition": "15M 收盤跌破 4520.00",
    })
    data["normalized_analysis"].update({
        "lastClosedCandlePrice": closed_price,
        "confirmationLevels": [
            {"kind": "support", "timeframe": "15M", "price": 4520},
            {"kind": "resistance", "timeframe": "15M", "price": 4532.51},
        ],
    })
    return data


def test_intrabar_above_trigger_remains_yellow_watch_until_15m_close():
    state, events = evaluate_unified_decision(
        closed_breakout_case(current_price=4534, closed_price=4530)
    )
    assert state["state"] == "LONG_WATCH"
    assert state["presentation"]["tone"] == "warning"
    assert state["presentation"]["title"] == "🟡【偏多等待確認｜尚不可進場】"
    assert state["flat_action"] == "等待，尚不可進場，請勿追價。"
    assert state["next_trigger"] == 4532.51
    assert events[-1]["latestClosedCandlePrice"] == 4530


def test_closed_15m_above_trigger_with_risk_controls_becomes_long_ready():
    state, events = evaluate_unified_decision(
        closed_breakout_case(current_price=4534, closed_price=4533)
    )
    assert state["state"] == "LONG_READY"
    assert state["presentation"]["tone"] == "long_ready"
    assert state["presentation"]["title"] == "🟢【多單進場條件成立】"
    assert state["next_trigger"] is None
    assert "等待新結構" in state["confirmation"]
    assert events[-1]["entryZone"] == {"low": 4527, "high": 4529}


def test_bias_means_direction_exists_but_price_has_not_reached_entry_zone():
    data = payload(4529.96, status="SETUP_WATCH")
    data["entry_engine"].update({
        "direction": "LONG", "missing_condition": "價格尚未進入觀察區",
    })
    state, _ = evaluate_unified_decision(data)
    assert state["state"] == "LONG_BIAS"
    assert state["presentation"]["title"] == "🟡【行情偏多｜尚未到進場區】"


def test_entry_zone_transition_is_bias_to_watch_not_directly_ready():
    outside = payload(4529, status="SETUP_WATCH")
    outside["entry_engine"].update({"direction": "LONG"})
    bias, _ = evaluate_unified_decision(outside)
    inside = payload(4530, status="ENTRY_READY")
    inside["entry_engine"].update({
        "direction": "LONG", "missing_condition": "尚缺已收盤反轉 K 線",
    })
    watch, events = evaluate_unified_decision(inside, bias)
    assert bias["state"] == "LONG_BIAS"
    assert watch["state"] == "LONG_WATCH"
    assert any(event["previousState"] == "LONG_BIAS"
               and event["currentState"] == "LONG_WATCH" for event in events)


def test_previously_completed_close_trigger_is_removed_immediately():
    previous = {
        "state": "LONG_WATCH", "direction": "LONG", "source_price": 4531,
        "next_trigger": 4532.51,
        "last_closed_candle_time": "2026-08-20T13:30:00+00:00",
    }
    state, events = evaluate_unified_decision(
        closed_breakout_case(current_price=4534, closed_price=4533), previous
    )
    assert state["state"] == "LONG_READY"
    assert state["next_trigger"] is None
    assert all((event.get("nextTriggerCondition") or {}).get("level") != 4532.51
               for event in events)
