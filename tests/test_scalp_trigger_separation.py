from copy import deepcopy

import pytest

from app.engines.decision_presentation import format_decision_message
from app.engines.fake_breakout_recovery import evaluate_fake_breakout_recovery
from app.engines.final_decision_engine import collect_signal_candidates
from app.engines.scalp_entry_trigger import (
    build_scalp_trigger_layers,
    validate_trigger_distance,
)


def market(*, direction="LONG", current=100.2, close=100.2,
           candle="2026-08-25T05:00:00+00:00", status="GOOD"):
    if direction == "LONG":
        levels = [
            {"kind": "support", "price": 99.5, "timeframe": "15M"},
            {"kind": "resistance", "price": 101.0, "timeframe": "15M"},
            {"kind": "resistance", "price": 104.0, "timeframe": "1H"},
        ]
    else:
        levels = [
            {"kind": "resistance", "price": 100.5, "timeframe": "15M"},
            {"kind": "support", "price": 99.0, "timeframe": "15M"},
            {"kind": "support", "price": 96.0, "timeframe": "1H"},
        ]
    return {
        "symbol": "XAUUSD",
        "timestamp_utc": candle,
        "normalized_analysis": {
            "currentPrice": current,
            "lastClosedCandlePrice": close,
            "lastClosedCandleTimestamp": candle,
            "marketDataStatus": status,
            "atr15": 2.0,
            "confirmationLevels": levels,
        },
    }


def failed_break(direction="LONG", candle="2026-08-25T05:00:00+00:00"):
    return {
        "state": "FAILED_BREAKDOWN" if direction == "LONG" else "FAILED_BREAKOUT",
        "level": 100.0,
        "break_confidence": 10,
        "reclaim_confidence": 85,
        "follow_through": "INSUFFICIENT",
        "last_evaluated_candle": candle,
    }


def test_4627_reclaim_uses_nearest_scalp_trigger_not_distant_trend_level():
    data = {
        "symbol": "XAUUSD",
        "timestamp_utc": "2026-08-25T05:00:00+00:00",
        "normalized_analysis": {
            "currentPrice": 4627.0,
            "lastClosedCandlePrice": 4627.0,
            "lastClosedCandleTimestamp": "2026-08-25T05:00:00+00:00",
            "marketDataStatus": "GOOD",
            "atr15": 10.0,
            "confirmationLevels": [
                {"kind": "support", "price": 4620.0, "timeframe": "15M"},
                {"kind": "resistance", "price": 4653.33, "timeframe": "1H"},
                {"kind": "resistance", "price": 4658.04, "timeframe": "4H"},
            ],
        },
    }
    state, _ = evaluate_fake_breakout_recovery(
        data=data,
        break_state={**failed_break("LONG"), "level": 4624.0},
    )
    scalp = state["primaryScalpTrigger"]
    trend = state["structuralConfirmationTrigger"]
    assert scalp["level"] < 4653.33
    assert abs(scalp["level"] - 4627.0) <= scalp["distanceGuard"]["maximumDistance"]
    assert trend["level"] == pytest.approx(4653.33)
    assert trend["timeframe"] == "1H"
    assert trend["entryGate"] is False
    assert state["nextAction"]["triggerLevel"] == scalp["level"]
    assert state["earlyReversalLong"] is True
    assert state["rescanBothSides"] is True


def test_distant_trend_confirmation_is_rejected_as_scalp_gate():
    guard = validate_trigger_distance(
        direction="LONG", current_price=4627.0, trigger=4653.33,
        atr15=10.0, invalidation=4620.0, nearest_target=4658.04,
        minimum_rr=1.5,
    )
    assert guard["valid"] is False
    assert guard["status"] == "TRIGGER_TOO_LATE_FOR_SCALP"


def test_same_structure_revalidates_without_moving_trigger_and_can_enter():
    first, _ = evaluate_fake_breakout_recovery(
        data=market(), break_state=failed_break("LONG"))
    original_trigger = first["nextAction"]["triggerLevel"]
    later_candle = "2026-08-25T05:15:00+00:00"
    later = market(current=101.1, close=101.1, candle=later_candle)
    confirmed, _ = evaluate_fake_breakout_recovery(
        data=later,
        break_state=failed_break("LONG", later_candle),
        previous=deepcopy(first),
    )
    assert confirmed["nextAction"]["triggerLevel"] == original_trigger
    assert confirmed["nextAction"]["triggerRevalidatedAt"] == later_candle
    assert confirmed["state"] == "LONG_SETUP_CONFIRMED"
    assert confirmed["scalpEntryReady"] is True
    candidates = collect_signal_candidates({**later, "fake_breakout_recovery": confirmed})
    recovery = next(item for item in candidates if item.setup_type == "FAKE_BREAKOUT_RECOVERY")
    assert recovery.lifecycle_state == "ENTRY_READY"
    assert recovery.trigger_price == original_trigger


def test_missing_or_degraded_15m_allows_watch_but_blocks_entry_ready():
    first, _ = evaluate_fake_breakout_recovery(
        data=market(), break_state=failed_break("LONG"))
    trigger = first["nextAction"]["triggerLevel"]
    later_candle = "2026-08-25T05:15:00+00:00"
    degraded = market(current=trigger + 0.1, close=trigger + 0.1,
                      candle=later_candle, status="DEGRADED_15M")
    state, _ = evaluate_fake_breakout_recovery(
        data=degraded,
        break_state=failed_break("LONG", later_candle),
        previous=deepcopy(first),
    )
    assert state["state"] == "LONG_SETUP_CONFIRMED"
    assert state["scalpEntryReady"] is False
    candidates = collect_signal_candidates({**degraded, "fake_breakout_recovery": state})
    recovery = next(item for item in candidates if item.setup_type == "FAKE_BREAKOUT_RECOVERY")
    assert recovery.lifecycle_state == "ARMED"


def test_short_recovery_is_directionally_symmetric():
    first, _ = evaluate_fake_breakout_recovery(
        data=market(direction="SHORT", current=99.8, close=99.8),
        break_state=failed_break("SHORT"),
    )
    trigger = first["nextAction"]["triggerLevel"]
    later_candle = "2026-08-25T05:15:00+00:00"
    later = market(direction="SHORT", current=trigger - 0.1,
                   close=trigger - 0.1, candle=later_candle)
    state, _ = evaluate_fake_breakout_recovery(
        data=later,
        break_state=failed_break("SHORT", later_candle),
        previous=deepcopy(first),
    )
    assert state["state"] == "SHORT_SETUP_CONFIRMED"
    assert state["scalpEntryReady"] is True
    assert state["primaryScalpTrigger"]["direction"] == "SHORT"
    assert state["structuralConfirmationTrigger"]["entryGate"] is False


def test_telegram_labels_scalp_and_trend_confirmation_separately():
    state, events = evaluate_fake_breakout_recovery(
        data=market(), break_state=failed_break("LONG"))
    text = format_decision_message({
        **events[0], "fakeBreakoutRecovery": state,
        "nextAction": state["nextAction"],
    })
    assert "短線進場確認" in text
    assert "趨勢翻多確認" in text
    assert "趨勢確認只影響信心與持有時間" in text
    assert "下一觸發" not in text
    assert "TREND_REVERSAL_CONFIRMED" not in text
    assert all(raw not in text for raw in ("None", "null", "undefined", "NaN"))


def test_new_structure_can_replace_trigger_with_explicit_new_identity():
    first = build_scalp_trigger_layers(
        direction="LONG", source_level=100.0,
        normalized=market()["normalized_analysis"],
        created_at="2026-08-25T05:00:00+00:00",
    )
    changed = deepcopy(market()["normalized_analysis"])
    changed["confirmationLevels"][1]["price"] = 101.5
    second = build_scalp_trigger_layers(
        direction="LONG", source_level=100.0, normalized=changed,
        created_at="2026-08-25T05:15:00+00:00",
    )
    assert first["triggerSourceStructureId"] != second["triggerSourceStructureId"]
    assert first["primaryScalpTrigger"]["level"] != second["primaryScalpTrigger"]["level"]
