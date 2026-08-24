import pytest

from app.engines.thesis_invalidation import (
    build_trade_thesis,
    evaluate_invalidation,
    initial_invalidation_state,
    validate_immutable_thesis,
)

SWEEP_LONG = {
    "setup_id": "sweep-long-20260824", "direction": "LONG",
    "suggested_entry": 4623.74, "stop_loss": 4615.0,
    "strategy_type": "SWEEP_RECLAIM_LONG", "sweep_low": 4594.73,
    "warning_level": 4615.0, "atr15": 12.0, "risk_per_trade": 100.0,
    "contract_value_per_point": 1.0, "reclaim_max_bars": 1,
    "thesis_description": "4594.73 下方掃低後快速收回，多方 reclaim 成立",
}


def thesis(entry=SWEEP_LONG):
    return build_trade_thesis(entry, created_at="2026-08-24T01:00:00+00:00")


def step(t, state, price, closed, candle):
    return evaluate_invalidation(
        t, state, current_price=price, closed_price=closed,
        candle_close_time=candle, atr15=12.0, regime="WHIPSAW")


def test_levels_are_distinct_and_frozen_before_entry():
    value = thesis()
    assert value["warningLevel"] == 4615
    assert value["hardInvalidation"]["level"] == 4594.73
    assert value["emergencyStop"] < 4594.73
    assert value["positionSize"] == pytest.approx(
        value["riskBudget"] / value["stopDistance"], rel=1e-5)


def test_today_intrabar_breach_recovers_without_false_stop():
    value, state = thesis(), initial_invalidation_state(thesis())
    state, _ = step(value, state, 4624, 4624, "2026-08-24T01:00:00+00:00")
    state, warning = step(value, state, 4613, 4624, "2026-08-24T01:00:00+00:00")
    state, recovered = step(value, state, 4617, 4624, "2026-08-24T01:00:00+00:00")
    state, repeated = step(value, state, 4625, 4624, "2026-08-24T01:00:00+00:00")
    assert [x["event_type"] for x in warning] == ["POSITION_WARNING"]
    assert [x["event_type"] for x in recovered] == ["POSITION_RECOVERED"]
    assert repeated == [] and state["state"] in {"HEALTHY", "RECOVERED"}


def test_closed_failure_has_fixed_reclaim_deadline_then_soft_invalidates():
    value, state = thesis(), initial_invalidation_state(thesis())
    state, _ = step(value, state, 4623, 4623, "2026-08-24T01:00:00+00:00")
    state, pending = step(value, state, 4610, 4609, "2026-08-24T01:15:00+00:00")
    deadline = state["reclaimDeadline"]
    state, invalidated = step(value, state, 4613, 4613, "2026-08-24T01:30:00+00:00")
    assert [x["event_type"] for x in pending] == ["SOFT_INVALIDATION_PENDING"]
    assert state["reclaimDeadline"] == deadline
    assert [x["event_type"] for x in invalidated] == ["SOFT_INVALIDATED"]
    assert state["reasonCode"] == "EXIT_SOFT_INVALIDATION"


def test_hard_acceptance_and_flash_crash_are_deterministic_exits():
    value = thesis()
    state, hard = step(value, initial_invalidation_state(value), 4593, 4593,
                       "2026-08-24T01:15:00+00:00")
    assert state["state"] == "HARD_INVALIDATED"
    assert hard[0]["reasonCode"] == "EXIT_HARD_INVALIDATION"
    state, flash = step(value, initial_invalidation_state(value), 4570, 4623,
                        "2026-08-24T01:00:00+00:00")
    assert state["state"] == "HARD_INVALIDATED"
    assert flash[0]["reasonCode"] == "EXIT_EMERGENCY_STOP"


def test_wider_structural_stop_reduces_position_size():
    narrow = thesis({**SWEEP_LONG, "sweep_low": 4610.0})
    wide = thesis({**SWEEP_LONG, "sweep_low": 4590.0})
    assert wide["stopDistance"] > narrow["stopDistance"]
    assert wide["positionSize"] < narrow["positionSize"]


def test_moving_goalpost_and_reclaim_deadline_are_immutable():
    original = thesis()
    changed = {**original, "hardInvalidation": {"level": 4570}}
    with pytest.raises(ValueError, match="不得修改固定風控"):
        validate_immutable_thesis(original, changed)


@pytest.mark.parametrize(("direction", "warning_tick"), [
    ("LONG", 4613.0), ("SHORT", 4634.0),
])
def test_intrabar_warning_is_symmetric_and_never_an_immediate_exit(
        direction, warning_tick):
    source = dict(SWEEP_LONG)
    if direction == "SHORT":
        source.update({
            "setup_id": "sweep-short", "direction": "SHORT",
            "suggested_entry": 4623.74, "stop_loss": 4632.0,
            "strategy_type": "SWEEP_RECLAIM_SHORT", "sweep_high": 4652.0,
            "warning_level": 4632.0,
        })
    value = thesis(source)
    state, events = step(value, initial_invalidation_state(value), warning_tick,
                         4623.74, "2026-08-24T01:00:00+00:00")
    assert state["state"] == "WARNING"
    assert events[0]["event_type"] == "POSITION_WARNING"
