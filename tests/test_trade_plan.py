from app.engines.trade_plan import (
    build_trade_plan,
    evaluate_trade_plans,
    migrate_legacy_virtual_profit,
)

LONG = {
    "status": "ENTRY_TRIGGERED", "setup_id": "long-1", "direction": "LONG",
    "suggested_entry": 4500.0, "zone_low": 4499.0, "zone_high": 4501.0,
    "stop_loss": 4490.0, "take_profit_1": 4510.0,
    "take_profit_2": 4520.0, "take_profit_3": 4530.0,
    "expires_at": "2026-08-21T18:00:00+00:00",
}
SHORT = {
    "status": "ENTRY_TRIGGERED", "setup_id": "short-1", "direction": "SHORT",
    "suggested_entry": 4550.0, "zone_low": 4549.0, "zone_high": 4551.0,
    "stop_loss": 4560.0, "take_profit_1": 4540.0,
    "take_profit_2": 4530.0, "take_profit_3": 4520.0,
}


def evaluate(entry=LONG, previous=None, *, price=4500, closed=4500, protection=None):
    return evaluate_trade_plans(
        entry, previous, symbol="XAUUSD", current_price=price,
        closed_price=closed, latest_structure_protection=protection,
        candle_close_time="2026-08-21T03:00:00+00:00",
        calculated_at="2026-08-21T03:01:00+00:00",
    )


def test_conditional_long_plan_has_fixed_complete_targets_and_percentages():
    state, events = evaluate()
    plan = state["activePlans"][0]
    assert events == []
    assert plan["direction"] == "LONG"
    assert (plan["tp1Price"], plan["tp2Price"], plan["tp3Price"]) == (4510, 4520, 4530)
    assert (plan["tp1Percent"], plan["tp2Percent"], plan["tp3Percent"]) == (30, 30, 40)
    assert plan["calculationVersion"] == "trade-plan-v2-thesis"


def test_missed_entry_does_not_stop_existing_position_management():
    state, _ = evaluate()
    missed = {**LONG, "status": "INVALIDATED"}
    state, events = evaluate(missed, state, price=4510, closed=4508)
    assert state["activePlans"][0]["status"] == "ACTIVE"
    assert events[0]["event_type"] == "TAKE_PROFIT_1"


def test_tp1_notifies_once_with_percent_and_new_protection():
    state, _ = evaluate()
    state, first = evaluate(previous=state, price=4510, closed=4508)
    state, repeated = evaluate(previous=state, price=4510.5, closed=4509)
    assert len(first) == 1
    assert first[0]["event_type"] == "TAKE_PROFIT_1"
    assert first[0]["percent"] == 30
    assert first[0]["newProtectionPrice"] == 4500
    assert repeated == []


def test_closed_15m_below_trailing_support_emits_early_exit():
    state, _ = evaluate()
    state, _ = evaluate(previous=state, price=4510, closed=4508)
    state, events = evaluate(previous=state, price=4499, closed=4499)
    assert events[0]["event_type"] == "EARLY_EXIT"
    assert events[0]["side"] == "LONG"
    assert state["activePlans"] == []


def test_short_intrabar_warning_is_not_mistaken_for_stop_or_take_profit():
    state, _ = evaluate(SHORT, price=4550, closed=4550)
    _state, events = evaluate({**SHORT, "status": "INVALIDATED"}, state,
                              price=4560, closed=4558)
    assert [event["event_type"] for event in events] == ["POSITION_WARNING"]
    assert events[0]["side"] == "SHORT"
    assert events[0]["currentState"] == "WARNING"


def test_restart_keeps_completed_tp_and_does_not_repeat():
    state, _ = evaluate()
    persisted, first = evaluate(previous=state, price=4510, closed=4508)
    rehydrated = {"plans": dict(persisted["plans"]), "errors": []}
    _state, after_restart = evaluate(
        {**LONG, "status": "NO_SETUP"}, rehydrated, price=4511, closed=4509)
    assert first[0]["event_type"] == "TAKE_PROFIT_1"
    assert after_restart == []


def test_missing_targets_fall_back_to_r_and_missing_core_is_reported():
    fallback, error = build_trade_plan(
        {key: value for key, value in LONG.items() if not key.startswith("take_profit")},
        symbol="XAUUSD", created_at="2026-08-21T03:01:00+00:00")
    assert error == ""
    assert fallback["calculationBasis"] == [
        "TP1=參考進場±1R", "TP2=參考進場±2R", "TP3=參考進場±3R"]
    state, _ = evaluate_trade_plans(
        {"status": "ENTRY_TRIGGERED", "setup_id": "broken"}, None,
        symbol="XAUUSD", current_price=4500, closed_price=4500,
        latest_structure_protection=None, candle_close_time="",
        calculated_at="2026-08-21T03:01:00+00:00")
    assert "止盈" not in state["errors"][0]
    assert "缺少" in state["errors"][0]


def test_legacy_completed_tp_progress_is_migrated_without_realerting():
    migrated = migrate_legacy_virtual_profit({
        "active": True, "setup_id": "legacy-1", "direction": "LONG",
        "entry": 4500, "original_stop": 4490, "tp1": 4510,
        "tp2": 4520, "tp3": 4530, "protection": 4500,
        "notified": ["TP1"],
    }, symbol="XAUUSD", calculated_at="2026-08-21T03:01:00+00:00")
    plan = migrated["activePlans"][0]
    assert plan["completedEvents"] == ["TAKE_PROFIT_1"]
    assert plan["migrationSource"] == "virtual_profit_v0"


def test_active_v1_plan_is_forward_migrated_without_losing_protection():
    state, _ = evaluate()
    legacy = dict(state["activePlans"][0])
    legacy.pop("tradeThesis")
    legacy.pop("invalidationState")
    legacy["initialStop"] = 4490.0
    migrated, _ = evaluate(previous={"plans": {legacy["tradePlanId"]: legacy}},
                           price=4500, closed=4500)
    plan = migrated["activePlans"][0]
    assert plan["migrationSource"] == "trade-plan-v1"
    assert plan["tradeThesis"]["hardInvalidation"]["level"] == 4490
    assert plan["initialStop"] < 4490
