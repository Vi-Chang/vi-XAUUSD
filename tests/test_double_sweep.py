from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.engines.decision_presentation import format_decision_message
from app.engines.double_sweep import (
    DoubleSweepConfig,
    detect_double_sweeps,
    edge_lifecycle,
)
from app.services.double_sweep_research import (
    aggregate_outcomes,
    build_point_in_time_outcomes,
    chronological_split,
    wilson_interval,
)


def frame(order="HIGH_THEN_LOW"):
    start = datetime(2026, 8, 24, tzinfo=timezone.utc)
    rows = []
    for i in range(16):
        rows.append({"open": 100, "high": 110, "low": 90, "close": 100,
                     "volume": 100, "is_closed": True})
    if order == "HIGH_THEN_LOW":
        rows.extend([
            {"open": 108, "high": 114, "low": 99, "close": 108, "volume": 180, "is_closed": True},
            {"open": 92, "high": 101, "low": 86, "close": 88, "volume": 220, "is_closed": True},
            {"open": 88, "high": 102, "low": 87, "close": 96, "volume": 200, "is_closed": True},
        ])
    else:
        rows.extend([
            {"open": 92, "high": 101, "low": 86, "close": 92, "volume": 180, "is_closed": True},
            {"open": 108, "high": 114, "low": 99, "close": 112, "volume": 220, "is_closed": True},
            {"open": 112, "high": 113, "low": 98, "close": 104, "volume": 200, "is_closed": True},
        ])
    return pd.DataFrame(rows, index=pd.date_range(start, periods=len(rows), freq="15min"))


CFG = DoubleSweepConfig(reference_bars=16, min_depth_atr=.1,
                        max_interval_bars=4, max_reclaim_bars=2)


@pytest.mark.parametrize("order", ["HIGH_THEN_LOW", "LOW_THEN_HIGH"])
def test_detects_order_with_frozen_prior_range(order):
    events = detect_double_sweeps(frame(order), config=CFG, regime4h="BULLISH")
    assert len(events) == 1
    event = events[0]
    assert event.order == order
    assert event.referenceHigh == 110
    assert event.referenceLow == 90
    assert event.highSweepDepthAtr > 0
    assert event.lowSweepDepthAtr > 0
    assert event.reclaimStatus in {"FULL", "STRONG"}


def test_single_sweep_is_not_double_sweep():
    assert detect_double_sweeps(frame().iloc[:17], config=CFG) == []


def test_forming_candle_cannot_confirm_event():
    data = frame()
    data.iloc[-1, data.columns.get_loc("is_closed")] = False
    assert detect_double_sweeps(data, config=CFG) == []


def test_future_candles_cannot_repaint_confirmed_event():
    base = frame()
    first = detect_double_sweeps(base, config=CFG)[0].to_dict()
    future = pd.DataFrame([
        {"open": 104, "high": 150, "low": 70, "close": 120,
         "volume": 999, "is_closed": True}
    ], index=[base.index[-1] + timedelta(minutes=15)])
    again = detect_double_sweeps(pd.concat([base, future]), config=CFG)[0].to_dict()
    assert again == first


def profile(n=40, expected=2.0):
    return {"sampleSize": n, "directionalBias": "UP", "expectedMoveAtr": expected,
            "triggerTimeP25Min": 15, "triggerTimeP75Min": 60,
            "confidenceWeight": .35}


def test_edge_age_and_consumption_are_monotonic():
    event = detect_double_sweeps(frame(), config=CFG)[0].to_dict()
    confirmed = datetime.fromisoformat(event["confirmedAt"])
    early = edge_lifecycle(event, profile(), now=confirmed + timedelta(minutes=20),
                           current_price=event["referenceMid"] + event["referenceAtr"])
    late = edge_lifecycle(event, profile(), now=confirmed + timedelta(minutes=45),
                          current_price=event["referenceMid"] + 1.8 * event["referenceAtr"])
    assert late["doubleSweepAgeMinutes"] >= early["doubleSweepAgeMinutes"]
    assert late["edgeConsumedPct"] >= early["edgeConsumedPct"]
    assert late["doubleSweepEdgeRemaining"] <= early["doubleSweepEdgeRemaining"]


def test_low_sample_never_contributes_to_strategy():
    event = detect_double_sweeps(frame(), config=CFG)[0].to_dict()
    state = edge_lifecycle(event, profile(n=7), now=datetime.fromisoformat(event["confirmedAt"]),
                           current_price=100)
    assert state["edgeStatus"] == "LOW_CONFIDENCE"
    assert state["statisticalEdgeWeight"] == 0


def research_rows(n=25):
    rows = []
    for i in range(n):
        up = i % 3 != 0
        rows.append({
            "order": "HIGH_THEN_LOW", "firstTouchDirection": "UP" if up else "DOWN",
            "firstTouchMinutes": 15 + i, "mfeUpAtr": 1.5 if up else .4,
            "mfeDownAtr": .4 if up else 1.2,
            "horizons": {str(b): {"returnAtr": .5 if up else -.3,
                                   "mfeAtr": 1.5 if up else .4,
                                   "maeAtr": -.4 if up else -1.2}
                         for b in (1, 2, 4, 8, 16, 32)},
        })
    return rows


def test_research_has_sample_gate_ci_and_reproducible_hash():
    report = aggregate_outcomes(research_rows(), dataset_version="fixture-v1",
                                config={"referenceBars": 16})
    profile_row = report["profiles"]["HIGH_THEN_LOW"]
    assert profile_row["sampleSize"] == 25
    assert profile_row["sampleConfidence"] == "LIMITED"
    assert profile_row["horizons"]["4"]["confidenceInterval95"] == list(
        wilson_interval(16, 25))
    assert report == aggregate_outcomes(research_rows(), dataset_version="fixture-v1",
                                        config={"referenceBars": 16})


def test_statistical_engine_has_no_trigger_or_stop_output():
    event_keys = detect_double_sweeps(frame(), config=CFG)[0].to_dict()
    forbidden = {"primaryTrigger", "hardStop", "entryZone", "canEnter"}
    assert forbidden.isdisjoint(event_keys)


def test_outcomes_start_strictly_after_confirmation():
    candles = frame()
    event = detect_double_sweeps(candles, config=CFG)[0].to_dict()
    rows = build_point_in_time_outcomes(candles, [event])
    assert len(rows) == 1
    assert set(rows[0]["horizons"]) <= {"1"}
    assert rows[0]["confirmedAt"] == event["confirmedAt"]


def test_chronological_split_never_shuffles_future_into_development():
    rows = [{"confirmedAt": f"2026-01-{day:02d}T00:00:00+00:00"}
            for day in range(1, 11)]
    split = chronological_split(list(reversed(rows)))
    assert [len(split[key]) for key in ("development", "validation", "outOfSample")] == [6, 2, 2]
    assert split["development"][-1]["confirmedAt"] < split["validation"][0]["confirmedAt"]
    assert split["validation"][-1]["confirmedAt"] < split["outOfSample"][0]["confirmedAt"]


def test_low_sample_telegram_explicitly_says_no_trade_effect():
    event = detect_double_sweeps(frame(), config=CFG)[0].to_dict()
    message = format_decision_message({"doubleSweepEvent": {
        "event": event, "profile": {"sampleSize": 4, "directionalBias": "UP"},
        "lifecycle": {"doubleSweepEdgeRemaining": 0}}})
    assert "暫不影響買賣判斷" in message
    assert "不會單獨叫你進場" in message
