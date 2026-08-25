from copy import deepcopy

from app.engines.candle_confirmation_registry import confirmation_record
from app.engines.canonical_decision import build_canonical_decision
from app.engines.setup_lifecycle import evaluate_setup_lifecycle
from app.services.alert_aggregator import aggregate_signal_facts
from tests.test_canonical_decision import payload


def test_live_cross_without_closed_cross_is_in_progress_everywhere():
    data = payload(price=4652.27, trigger=4650.08)
    data["normalized_analysis"]["lastClosedCandlePrice"] = 4643.09
    data["final_decision_state"].update(finalAction="WAIT", canEnter=False, state="WAIT")
    canonical = build_canonical_decision(data, data["final_decision_state"])
    record = next(iter(canonical["confirmationRegistry"].values()))
    assert canonical["confirmationStatus"] == record["status"] == "IN_PROGRESS"
    assert canonical["newEntryDecision"]["canEnter"] is False


def test_two_components_share_the_same_confirmation_registry_record():
    data = payload(price=4652.27, trigger=4650.08)
    data["normalized_analysis"]["lastClosedCandlePrice"] = 4643.09
    data["final_decision_state"].update(finalAction="WAIT", canEnter=False, state="WAIT")
    canonical = build_canonical_decision(data, data["final_decision_state"])
    key = "XAUUSD:15M:4650.08:ABOVE"
    dashboard_status = canonical["confirmationRegistry"][key]["status"]
    telegram_status = canonical["confirmationStatus"]
    assert dashboard_status == telegram_status == "IN_PROGRESS"


def test_future_confirmation_cannot_pollute_older_decision():
    record = confirmation_record(
        symbol="XAUUSD", timeframe="15M", level=4650.08, direction="LONG",
        live_price=4652.0, last_closed_price=4652.0,
        candle_close_time="2026-08-24T13:30:00Z",
        decision_timestamp="2026-08-24T13:15:00Z")
    assert record["status"] == "IN_PROGRESS"
    assert record["confirmedAt"] is None and not record["temporalValid"]


def test_invalidated_setup_archives_and_cannot_reenter():
    base = {"previous": None, "setup_id": "S1", "direction": "SHORT",
            "confirmation_price": 4650.0, "latest_closed_price": 4640.0,
            "closed_candle_time": "2026-08-24T13:15:00Z", "current_price": 4645.0,
            "entry_zone_low": 4644.0, "entry_zone_high": 4646.0,
            "risk_controls_passed": True, "calculated_at": "2026-08-24T13:16:00Z"}
    invalid = evaluate_setup_lifecycle(**base, invalidated=True)
    archived = evaluate_setup_lifecycle(**{**base, "previous": invalid,
                                           "calculated_at": "2026-08-24T13:17:00Z"},
                                        invalidated=True)
    assert invalid["state"] == "INVALIDATED" and invalid["invalidatedAt"]
    assert archived["state"] == "ARCHIVED" and archived["archivedAt"]


def test_rr_point_eight_three_never_uses_best_entry_label():
    data = payload(rr=.83)
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["rrValid"] is False
    assert canonical["primarySetup"]["entryZoneLabel"] != "最佳進場區"


def test_four_setups_are_ranked_to_one_primary():
    data = payload()
    base = data["final_decision_state"]["signalCandidates"][0]
    variants = []
    for index, rr in enumerate((.8, 1.2, 1.8, 2.2), 1):
        item = deepcopy(base)
        item.update(scenario_id=f"S{index}", risk_reward=rr,
                    targets=(4655.0 + rr * 15.0, 4700.0, 4720.0),
                    setup_type="PULLBACK_LONG" if index == 4 else "BREAKOUT")
        variants.append(item)
    data["final_decision_state"].update(
        finalAction="WAIT", canEnter=False, state="WAIT", signalCandidates=variants)
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["primarySetup"]["setupId"] == "S4"
    assert len(canonical["alternativeSetups"]) == 3
    assert sum(bool(item["canEnter"]) for item in [
        canonical["primarySetup"], *canonical["alternativeSetups"]]) <= 1


def test_long_take_profit_without_bearish_confirmation_never_sells():
    alerts = aggregate_signal_facts("XAUUSD", [{
        "evaluationCycleId": "tp-cycle", "tradePlanId": "LONG-1",
        "direction": "LONG", "currentState": "LONG_MANAGE",
        "event_type": "TAKE_PROFIT_1", "canEnter": False, "finalAction": "WAIT",
    }])
    assert len(alerts) == 1
    assert alerts[0]["alertCategory"] == "EXIT_WARNING"
    assert alerts[0].get("finalAction") != "ENTER_SHORT"


def test_invalidated_short_targets_are_not_in_primary_decision():
    data = payload()
    invalid = deepcopy(data["final_decision_state"]["signalCandidates"][0])
    invalid.update(direction="SHORT", scenario_id="OLD-SHORT",
                   lifecycle_state="INVALIDATED", targets=(4510, 4506, 4502))
    data["final_decision_state"].update(
        finalAction="WAIT", canEnter=False, state="WAIT", signalCandidates=[invalid])
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["activeSetupId"] is None
    assert canonical["primarySetup"] is None
    assert canonical["archivedSetups"][0]["targets"] == [4510.0, 4506.0, 4502.0]


def test_only_one_primary_trigger_is_exposed():
    data = payload()
    base = data["final_decision_state"]["signalCandidates"][0]
    other = deepcopy(base)
    other.update(scenario_id="ALT", trigger_price=4670.0, risk_reward=1.7)
    data["final_decision_state"].update(
        finalAction="WAIT", canEnter=False, state="WAIT",
        signalCandidates=[base, other])
    data["normalized_analysis"]["lastClosedCandlePrice"] = 4640.0
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["primaryNextTrigger"] == canonical["canonicalNextTrigger"]
    assert canonical["primaryNextTrigger"]["setupId"] == canonical["primarySetup"]["setupId"]


def test_unknown_position_is_collapsed_and_separate_from_entry():
    data = payload()
    canonical = build_canonical_decision(data, data["final_decision_state"])
    assert canonical["positionManagement"]["positionKnown"] is False
    assert canonical["positionManagement"]["collapsedByDefault"] is True
    assert canonical["newEntryDecision"]["action"] in {"BUY", "WAIT"}
