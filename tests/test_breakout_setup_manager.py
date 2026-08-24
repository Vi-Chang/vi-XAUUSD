from copy import deepcopy

from app.engines.breakout_setup_manager import (
    evaluate_breakout_setups,
    migrate_legacy_breakout_setup,
)


def snapshot(*, trigger=4567.88, price=4550.0, closed=4550.0,
             at="2026-08-21T10:00:00+00:00", candle="2026-08-21T09:45:00+00:00",
             trend="bullish", ohlc=None, support_shift=0.0):
    result = {"symbol": "XAUUSD", "timestamp_utc": at, "normalized_analysis": {
        "trendBias": trend, "marketRegime": "bullish" if trend == "bullish" else "bearish",
        "currentPrice": price, "lastClosedCandlePrice": closed,
        "lastClosedCandleTimestamp": candle, "marketDataStatus": "GOOD", "atr15": 10.0,
        "confirmationLevels": [
            {"kind": "resistance", "timeframe": "15M", "price": trigger, "buffer": 1.0},
            {"kind": "support", "timeframe": "15M", "price": trigger - 8.0 + support_shift, "buffer": 1.0},
        ]}, "entry_engine": {"take_profit_1": trigger + 18,
                              "take_profit_2": trigger + 27,
                              "take_profit_3": trigger + 36}}
    if ohlc:
        result["latest_closed_15m"] = ohlc
    return result


def advance(data, state=None):
    return evaluate_breakout_setups(data, state)


def test_waiting_setup_exposes_breakout_and_pullback_paths():
    state, events = advance(snapshot())
    setup = state["activeSetup"]
    assert setup["status"] == "WAIT_BREAKOUT_OR_PULLBACK"
    assert setup["breakoutTrigger"] == 4567.88
    assert setup["pullbackEntryZoneLow"] < setup["pullbackEntryZoneHigh"] < setup["breakoutTrigger"]
    assert len(setup["pullbackZoneReason"]) >= 2
    assert [event["event_type"] for event in events] == ["NEW_SETUP_CREATED"]


def test_entering_pullback_zone_waits_for_reversal_confirmation():
    state, _ = advance(snapshot())
    setup = state["activeSetup"]
    middle = (setup["pullbackEntryZoneLow"] + setup["pullbackEntryZoneHigh"]) / 2
    state, events = advance(snapshot(price=middle, closed=middle,
        at="2026-08-21T10:01:00+00:00"), state)
    assert state["activeSetup"]["status"] == "WAIT_PULLBACK_CONFIRMATION"
    assert [event["event_type"] for event in events] == ["WAIT_PULLBACK_CONFIRMATION"]


def test_pullback_reclaim_becomes_ready_once():
    state, _ = advance(snapshot())
    high = state["activeSetup"]["pullbackEntryZoneHigh"]
    data = snapshot(price=high, closed=high, at="2026-08-21T10:01:00+00:00",
                    ohlc={"open": high - 1, "high": high + .4, "low": high - 3, "close": high})
    state, events = advance(data, state)
    assert state["activeSetup"]["status"] == "PULLBACK_ENTRY_READY"
    assert [event["event_type"] for event in events] == ["PULLBACK_ENTRY_READY"]
    assert advance(data, state)[1] == []


def test_pullback_close_below_invalidation_cancels_setup():
    state, _ = advance(snapshot())
    invalidation = state["activeSetup"]["pullbackInvalidationPrice"]
    state, events = advance(snapshot(price=invalidation - 1, closed=invalidation - 1,
        at="2026-08-21T10:01:00+00:00"), state)
    assert state["activeSetup"]["status"] == "PULLBACK_INVALIDATED"
    assert [event["event_type"] for event in events] == ["PULLBACK_INVALIDATED"]


def test_intrabar_pullback_breach_pauses_entry_until_candle_closes():
    state, _ = advance(snapshot())
    invalidation = state["activeSetup"]["pullbackInvalidationPrice"]
    state, events = advance(snapshot(price=invalidation - 1, closed=invalidation + 2,
        at="2026-08-21T10:01:00+00:00"), state)
    setup = state["activeSetup"]
    assert setup["status"] == "PULLBACK_BREACH_PENDING_CLOSE"
    assert "15M尚未收盤" in setup["blockedReason"]
    assert [event["event_type"] for event in events] == ["PULLBACK_BREACH_PENDING_CLOSE"]
    _, repeated = advance(snapshot(price=invalidation - 2, closed=invalidation + 2,
        at="2026-08-21T10:02:00+00:00"), state)
    assert repeated == []


def test_closed_breakout_inside_chase_is_ready():
    state, _ = advance(snapshot())
    state, events = advance(snapshot(price=4568.2, closed=4568.0,
        at="2026-08-21T10:01:00+00:00"), state)
    assert state["activeSetup"]["status"] == "BREAKOUT_ENTRY_READY"
    assert [event["event_type"] for event in events] == ["BREAKOUT_CONFIRMED", "BREAKOUT_ENTRY_READY"]


def test_breakout_above_chase_switches_to_pullback_instead_of_entry():
    state, _ = advance(snapshot())
    state, events = advance(snapshot(price=4580, closed=4569,
        at="2026-08-21T10:01:00+00:00"), state)
    setup = state["activeSetup"]
    assert setup["status"] == "WAIT_RETEST"
    assert setup["tradeState"] == "MISSED"
    assert "超過追價上限" in setup["blockedReason"]
    assert not any(event["event_type"].endswith("ENTRY_READY") for event in events)


def test_new_structure_does_not_move_locked_active_trigger():
    state, _ = advance(snapshot())
    old = deepcopy(state["activeSetup"])
    newer = snapshot(trigger=4601.09, price=4590, closed=4560,
                     at="2026-08-21T10:15:00+00:00", candle="2026-08-21T10:00:00+00:00")
    state, events = advance(newer, state)
    assert len(state["setups"]) == 1
    assert state["setups"][0]["breakoutTrigger"] == old["breakoutTrigger"]
    assert state["setups"][0]["primaryTrigger"] == old["breakoutTrigger"]
    assert events == []


def test_higher_timeframe_invalidation_disables_pullback_long():
    state, _ = advance(snapshot())
    state, events = advance(snapshot(trend="bearish", at="2026-08-21T10:01:00+00:00"), state)
    assert state["setups"][0]["status"] == "PULLBACK_INVALIDATED"
    assert any(event["event_type"] == "PULLBACK_INVALIDATED" for event in events)


def test_new_closed_structure_moves_pullback_zone_without_moving_breakout_trigger():
    state, _ = advance(snapshot())
    old = deepcopy(state["activeSetup"])
    state, events = advance(snapshot(price=4555, closed=4555, support_shift=4,
        candle="2026-08-21T10:00:00+00:00", at="2026-08-21T10:01:00+00:00"), state)
    setup = state["activeSetup"]
    assert setup["breakoutTrigger"] == old["breakoutTrigger"]
    assert setup["pullbackEntryZoneLow"] > old["pullbackEntryZoneLow"]
    assert any(event["event_type"] == "PULLBACK_ZONE_UPDATED" for event in events)


def test_same_wait_state_is_panel_only_and_does_not_notify_again():
    state, first = advance(snapshot())
    assert len(first) == 1
    state, second = advance(snapshot(price=4557, closed=4557,
                                     at="2026-08-21T10:04:00+00:00"), state)
    assert state["activeSetup"]["status"] == "WAIT_BREAKOUT_OR_PULLBACK"
    assert second == []


def test_pullback_ready_has_priority_when_conditions_overlap():
    data = snapshot(price=4567.88, closed=4568,
                    ohlc={"open": 4566, "high": 4568.5, "low": 4564, "close": 4568})
    state, _ = advance(data)
    setup = state["activeSetup"]
    setup.update(pullbackEntryZoneLow=4564, pullbackEntryZoneHigh=4568,
                 pullbackInvalidationPrice=4558)
    state, events = advance({**data, "timestamp_utc": "2026-08-21T10:01:00+00:00"}, state)
    assert state["activeSetup"]["status"] == "PULLBACK_ENTRY_READY"
    assert [event["event_type"] for event in events] == ["PULLBACK_ENTRY_READY"]


def test_legacy_migration_keeps_original_trigger_and_confidence_is_external():
    legacy = {"setupLifecycle": {"setupId": "OLD-4567", "direction": "LONG",
        "confirmationPrice": 4567.88, "confirmedAt": "2026-08-21T09:46:00+00:00",
        "confirmedCandleTime": "2026-08-21T09:45:00+00:00"},
        "signalScore": 100, "confidenceGrade": "A"}
    migrated = migrate_legacy_breakout_setup(snapshot(), legacy)
    setup = migrated["activeSetup"]
    assert (setup["setupId"], setup["breakoutTrigger"], setup["status"]) == (
        "OLD-4567", 4567.88, "BREAKOUT_CONFIRMED")
    assert legacy["signalScore"] == 100 and legacy["confidenceGrade"] == "A"
