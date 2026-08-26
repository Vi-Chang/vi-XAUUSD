from datetime import datetime, timedelta, timezone

from app.engines.decision_presentation import format_decision_message
from app.engines.entry_opportunity_gate import evaluate_entry_opportunity_gate
from app.engines.entry_starvation_monitor import evaluate_entry_starvation


def candidate(*, side="SHORT", rr=2.0, lifecycle="ENTRY_READY",
              setup_type="RESISTANCE_REJECTION_SHORT", strength=86):
    is_long = side == "LONG"
    return {
        "scenario_id": f"{setup_type}-1", "direction": side,
        "setup_type": setup_type, "lifecycle_state": lifecycle,
        "entry_zone": (100.0, 101.0),
        "invalidation_price": 98.0 if is_long else 103.0,
        "risk_reward": rr, "strength": strength,
    }


def context(*, bias15="BEARISH", bias1h="BEARISH", bias4h="BEARISH",
            bias1d="BEARISH", health="HEALTHY", price=100.5):
    return {
        "currentPrice": price, "atr15": 2.0, "dataHealth": health,
        "closedCandleAvailable": True, "bias15m": bias15,
        "bias1h": bias1h, "bias4h": bias4h, "bias1d": bias1d,
        "marketBias": bias1h, "momentum": "ACCELERATING",
        "defenseState": "HELD", "scenarioValidity": "ACTIVE",
    }


def test_a_complete_bearish_rejection_is_entry_ready():
    result = evaluate_entry_opportunity_gate([candidate()], context=context())
    assert result["entryState"] == "ENTRY_READY"
    assert result["selected"]["direction"] == "SHORT"
    assert result["selected"]["hardBlocks"] == []


def test_b_counter_4h_is_soft_and_cannot_veto_short():
    result = evaluate_entry_opportunity_gate(
        [candidate()], context=context(bias4h="BULLISH"))
    assert result["entryState"] in {"ENTRY_READY", "PROBE_READY"}
    assert "COUNTER_4H_STRUCTURE" in {
        row["code"] for row in result["selected"]["softFilters"]}
    assert result["selected"]["hardBlocks"] == []


def test_c_daily_bullish_only_adjusts_short_confidence():
    aligned = evaluate_entry_opportunity_gate([candidate()], context=context())
    counter = evaluate_entry_opportunity_gate(
        [candidate()], context=context(bias1d="BULLISH"))
    assert counter["entryState"] in {"ENTRY_READY", "PROBE_READY"}
    assert counter["shortScore"] < aligned["shortScore"]
    assert "COUNTER_1D_TREND" in {
        row["code"] for row in counter["selected"]["softFilters"]}


def test_d_momentum_continuation_does_not_require_perfect_retest():
    result = evaluate_entry_opportunity_gate([
        candidate(side="LONG", setup_type="CONTINUATION_ENTRY")
    ], context=context(
        bias15="BULLISH", bias1h="BULLISH", bias4h="BULLISH",
        bias1d="BULLISH"))
    assert result["entryState"] == "ENTRY_READY"
    assert result["selected"]["setupType"] == "CONTINUATION_ENTRY"


def test_e_false_break_long_is_a_formal_candidate():
    result = evaluate_entry_opportunity_gate([
        candidate(side="LONG", setup_type="FALSE_BREAK_LONG", strength=82)
    ], context=context(
        bias15="BULLISH", bias1h="BULLISH", bias4h="BULLISH",
        bias1d="BULLISH"))
    assert result["entryState"] == "ENTRY_READY"
    assert result["selected"]["scoreComponents"]["recognizedSetup"] == 6


def test_f_rr_between_absolute_and_preferred_is_probe_ready():
    result = evaluate_entry_opportunity_gate([
        candidate(rr=1.30, lifecycle="CONFIRMED", strength=90)
    ], context=context())
    assert result["entryState"] == "PROBE_READY"
    assert result["selected"]["positionSizeMultiplier"] == .4


def test_g_rr_below_absolute_minimum_is_blocked():
    result = evaluate_entry_opportunity_gate(
        [candidate(rr=1.10)], context=context())
    assert result["entryState"] == "BLOCKED"
    assert "RR_BELOW_ABSOLUTE_MINIMUM" in result["selected"]["hardBlocks"]


def test_h_stale_core_data_is_blocked():
    result = evaluate_entry_opportunity_gate(
        [candidate()], context=context(health="STALE"))
    assert result["entryState"] == "BLOCKED"
    assert "DATA_STALE" in result["selected"]["hardBlocks"]


def test_i_forming_setup_is_watch_and_explains_missing_confirmation():
    result = evaluate_entry_opportunity_gate([
        candidate(lifecycle="SETUP_FORMING")
    ], context=context())
    assert result["entryState"] == "WATCH"
    assert "等待15M收盤完成主要確認" in result["selected"]["missingConditions"]


def test_long_and_short_are_scored_independently():
    result = evaluate_entry_opportunity_gate([
        candidate(side="LONG", setup_type="SUPPORT_REJECTION_LONG", strength=40),
        candidate(side="SHORT", strength=86),
    ], context=context())
    assert result["long"] is not None and result["short"] is not None
    assert result["longScore"] < result["shortScore"]
    assert result["selected"]["direction"] == "SHORT"


def test_j_starvation_monitor_warns_without_relaxing_thresholds():
    state = None
    events = []
    start = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
    for index in range(4):
        decision = {"entryOpportunityGate": {
            "entryState": "WATCH", "longScore": 52, "shortScore": 57,
            "candidateEvaluations": [{
                "scenarioId": f"S{index}", "hardBlocks": [],
                "softFilters": [{"code": "RETEST_PARTIAL", "penalty": 8}],
            }],
            "selected": {
                "scenarioId": f"S{index}", "hardBlocks": [],
                "softFilters": [{"code": "RETEST_PARTIAL", "penalty": 8}],
            },
        }}
        state, emitted = evaluate_entry_starvation(
            decision, previous=state,
            evaluated_at=(start + timedelta(minutes=index * 15)).isoformat())
        events.extend(emitted)
    assert state["starvationWarning"] is True
    assert state["thresholdPolicy"] == "DIAGNOSTIC_ONLY_NEVER_RELAX_SAFETY"
    assert state["windows"]["1h"]["candidateCount"] == 4
    assert state["windows"]["1h"]["topSoftPenalties"][0][0] == "RETEST_PARTIAL"
    assert [event["event_type"] for event in events] == ["ENTRY_STARVATION_WARNING"]


def test_probe_notification_is_actionable_plain_chinese():
    message = format_decision_message({
        "event_type": "PROBE_READY", "direction": "SHORT",
        "currentPrice": 4638.4, "entryZone": {"low": 4638, "high": 4640},
        "stopLoss": 4644, "effectiveRR": 1.3,
        "positionSizeMultiplier": .4,
    })
    assert message.splitlines()[0] == "🟠【XAUUSD 可以小倉試單】"
    assert "方向：做空" in message
    assert "建議倉位：一般設定的 0.4 倍" in message
    for internal in ("PROBE_READY", "SOFT_FILTER", "None", "null"):
        assert internal not in message
