from copy import deepcopy

from app.engines.breakout_setup_manager import (
    evaluate_breakout_setups,
    migrate_legacy_breakout_setup,
)


def snapshot(*, trigger=4567.88, price=4568.2, closed=4568.0,
             at="2026-08-21T10:00:00+00:00", candle="2026-08-21T09:45:00+00:00"):
    return {"symbol": "XAUUSD", "timestamp_utc": at, "normalized_analysis": {
        "trendBias": "bullish", "currentPrice": price,
        "lastClosedCandlePrice": closed, "lastClosedCandleTimestamp": candle,
        "marketDataStatus": "GOOD", "atr15": 10.0,
        "confirmationLevels": [
            {"kind": "resistance", "timeframe": "15M", "price": trigger, "buffer": 1.0},
            {"kind": "support", "timeframe": "15M", "price": trigger - 8.0, "buffer": 1.0},
        ]}, "entry_engine": {"take_profit_1": trigger + 18,
                              "take_profit_2": trigger + 27,
                              "take_profit_3": trigger + 36}}


def test_closed_breakout_is_confirmed_and_never_returns_to_wait_confirmation():
    state, _ = evaluate_breakout_setups(snapshot(), None)
    setup_id = state["activeSetup"]["setupId"]
    state, events = evaluate_breakout_setups(snapshot(at="2026-08-21T10:01:00+00:00"), state)
    setup = state["setups"][0]
    assert setup["setupId"] == setup_id and setup["breakoutConfirmedAt"]
    assert setup["status"] == "ENTRY_READY_BREAKOUT"
    assert [e["event_type"] for e in events] == ["BREAKOUT_CONFIRMED", "ENTRY_READY_BREAKOUT"]


def test_confirmed_breakout_too_far_waits_for_exact_retest_zone():
    state, _ = evaluate_breakout_setups(snapshot(price=4580), None)
    state, events = evaluate_breakout_setups(snapshot(price=4580, at="2026-08-21T10:01:00+00:00"), state)
    setup = state["setups"][0]
    assert setup["status"] == "WAIT_RETEST"
    assert (setup["retestZoneLow"], setup["retestZoneHigh"]) == (4566.88, 4568.88)
    assert "WAIT_RETEST" in [event["event_type"] for event in events]


def test_retest_hold_becomes_ready_once():
    state, _ = evaluate_breakout_setups(snapshot(price=4580), None)
    state, _ = evaluate_breakout_setups(snapshot(price=4580, at="2026-08-21T10:01:00+00:00"), state)
    data = snapshot(price=4568, at="2026-08-21T10:02:00+00:00")
    state, events = evaluate_breakout_setups(data, state)
    assert state["setups"][0]["status"] == "ENTRY_READY_RETEST"
    assert [e["event_type"] for e in events] == ["ENTRY_READY_RETEST"]
    _, repeated = evaluate_breakout_setups(data, state)
    assert repeated == []


def test_new_structure_creates_new_id_and_preserves_old_trigger():
    state, _ = evaluate_breakout_setups(snapshot(price=4580), None)
    state, _ = evaluate_breakout_setups(snapshot(price=4580, at="2026-08-21T10:01:00+00:00"), state)
    old = deepcopy(state["setups"][0])
    newer = snapshot(trigger=4601.09, price=4595, closed=4595,
                     at="2026-08-21T10:15:00+00:00", candle="2026-08-21T10:00:00+00:00")
    state, events = evaluate_breakout_setups(newer, state)
    assert len(state["setups"]) == 2
    assert state["setups"][0]["setupId"] == old["setupId"]
    assert state["setups"][0]["breakoutTrigger"] == 4567.88
    assert state["setups"][1]["breakoutTrigger"] == 4601.09
    assert state["setups"][1]["previousSetupId"] == old["setupId"]
    assert state["setups"][1]["oldTrigger"] == 4567.88
    assert [e["event_type"] for e in events] == ["NEW_SETUP_CREATED"]


def test_new_breakout_within_chase_becomes_executable():
    data = snapshot(trigger=4601.09, price=4601.5, closed=4602)
    state, _ = evaluate_breakout_setups(data, None)
    state, events = evaluate_breakout_setups(snapshot(
        trigger=4601.09, price=4601.5, closed=4602,
        at="2026-08-21T10:01:00+00:00"), state)
    setup = state["activeSetup"]
    assert setup["status"] == "ENTRY_READY_BREAKOUT"
    assert all(setup[key] is not None for key in ("stopPrice", "tp1", "tp2", "tp3"))
    assert sum(e["event_type"] == "ENTRY_READY_BREAKOUT" for e in events) == 1


def test_same_wait_only_updates_panel_without_new_event():
    state, first = evaluate_breakout_setups(snapshot(trigger=4601.09, price=4590, closed=4590), None)
    assert [e["event_type"] for e in first] == ["NEW_SETUP_CREATED"]
    later = snapshot(trigger=4601.09, price=4592, closed=4590,
                     at="2026-08-21T10:04:00+00:00")
    state, second = evaluate_breakout_setups(later, state)
    assert state["activeSetup"]["status"] == "WAIT_BREAKOUT_CONFIRMATION"
    assert second == []


def test_legacy_migration_keeps_original_trigger_and_confidence_is_external():
    legacy = {"setupLifecycle": {"setupId": "OLD-4567", "direction": "LONG",
        "confirmationPrice": 4567.88, "confirmedAt": "2026-08-21T09:46:00+00:00",
        "confirmedCandleTime": "2026-08-21T09:45:00+00:00"},
        "signalScore": 100, "confidenceGrade": "A"}
    migrated = migrate_legacy_breakout_setup(snapshot(price=4580), legacy)
    setup = migrated["activeSetup"]
    assert (setup["setupId"], setup["breakoutTrigger"], setup["status"]) == (
        "OLD-4567", 4567.88, "BREAKOUT_CONFIRMED")
    assert legacy["signalScore"] == 100 and legacy["confidenceGrade"] == "A"
