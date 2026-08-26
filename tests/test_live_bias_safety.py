from app.engines.decision_presentation import format_decision_message
from app.engines.entry_opportunity_gate import evaluate_entry_opportunity_gate
from app.engines.live_bias import evaluate_live_bias


def snapshot(*, price: float, closed: float, closed_time="2026-08-25T10:00:00Z",
             atr=1.0):
    return {
        "symbol": "XAUUSD", "timestamp_utc": "2026-08-25T10:05:00Z",
        "normalized_analysis": {
            "currentPrice": price, "lastClosedCandlePrice": closed,
            "lastClosedCandleTimestamp": closed_time, "atr15": atr,
            "marketDataStatus": "GOOD",
        },
        "decision_health_state": {"dataHealth": "HEALTHY"},
    }


def setup(side="SHORT", *, invalidation=100.0, lifecycle="ENTRY_READY"):
    return {
        "scenario_id": f"{side}-1", "scenario_version": 2,
        "direction": side, "setup_type": "RESISTANCE_REJECTION_SHORT",
        "lifecycle_state": lifecycle, "entry_zone": (98.0, 99.0),
        "invalidation_price": invalidation, "risk_reward": 2.0,
        "strength": 85,
    }


def test_a_live_strong_break_stops_old_short_without_flipping_long():
    state, events = evaluate_live_bias(
        snapshot(price=102.0, closed=99.0), structural_bias="BEARISH",
        candidates=[setup()])
    assert state["structuralBias"] == "BEARISH"
    assert state["liveBiasState"] == "INVALIDATING"
    assert state["executionBias"] == "NEUTRAL"
    assert state["allowShort"] is False and state["intrabarSafetyOverride"]
    assert events[0]["event_type"] == "SHORT_INVALIDATING"


def test_b_break_smaller_than_dynamic_buffer_does_not_flip():
    state, _ = evaluate_live_bias(
        snapshot(price=100.30, closed=99.0), structural_bias="BEARISH",
        candidates=[setup()])
    assert state["liveBiasState"] == "WEAKENING"
    assert state["executionBias"] == "SHORT_WATCH"
    assert state["intrabarSafetyOverride"] is False


def test_c_closed_15m_confirms_opposite_watch_but_not_entry():
    previous = {
        "liveBiasState": "INVALIDATING", "executionBias": "NEUTRAL",
        "lastClosed15m": "2026-08-25T09:45:00Z", "biasOriginPrice": 99.0,
        "consecutiveBreachCount": 2,
    }
    state, events = evaluate_live_bias(
        snapshot(price=101.4, closed=101.0), structural_bias="BEARISH",
        candidates=[setup()], previous=previous)
    assert state["liveBiasState"] == "REVERSAL_CANDIDATE"
    assert state["executionBias"] == "LONG_WATCH"
    assert state["allowLong"] is True and state["allowShort"] is False
    assert events[0]["event_type"] == "LONG_WATCH"


def test_d_intrabar_break_that_closes_back_restores_short():
    previous = {
        "liveBiasState": "INVALIDATING", "executionBias": "NEUTRAL",
        "lastClosed15m": "2026-08-25T09:45:00Z", "biasOriginPrice": 99.0,
        "consecutiveBreachCount": 2,
    }
    state, events = evaluate_live_bias(
        snapshot(price=99.6, closed=99.5), structural_bias="BEARISH",
        candidates=[setup()], previous=previous)
    assert state["liveBiasState"] == "ALIGNED"
    assert state["executionBias"] == "SHORT"
    assert events[0]["event_type"] == "SHORT_RESTORED"


def test_e_long_invalidation_is_perfectly_symmetric():
    state, events = evaluate_live_bias(
        snapshot(price=98.0, closed=101.0), structural_bias="BULLISH",
        candidates=[setup("LONG", invalidation=100.0)])
    assert state["liveBiasState"] == "INVALIDATING"
    assert state["executionBias"] == "NEUTRAL"
    assert state["allowLong"] is False
    assert events[0]["event_type"] == "LONG_INVALIDATING"


def test_f_closed_break_invalidates_old_setup_lifecycle():
    previous = {"lastClosed15m": "2026-08-25T09:45:00Z"}
    state, _ = evaluate_live_bias(
        snapshot(price=101.5, closed=101.2), structural_bias="BEARISH",
        candidates=[setup()], previous=previous)
    assert state["setupState"] == "INVALIDATED"
    assert state["executionBias"] == "LONG_WATCH"


def test_old_direction_is_a_hard_execution_veto_during_invalidation():
    result = evaluate_entry_opportunity_gate([setup()], context={
        "currentPrice": 98.5, "atr15": 1.0, "dataHealth": "HEALTHY",
        "closedCandleAvailable": True, "bias15m": "BEARISH",
        "bias1h": "BEARISH", "bias4h": "BEARISH", "bias1d": "BULLISH",
        "marketBias": "BEARISH", "momentum": "ACCELERATING",
        "defenseState": "HELD", "scenarioValidity": "ACTIVE",
        "structuralBias": "BEARISH", "structuralSide": "SHORT",
        "liveBiasState": "INVALIDATING", "executionBias": "NEUTRAL",
    })
    assert result["entryState"] == "BLOCKED"
    assert "LIVE_BIAS_INVALIDATING" in result["selected"]["hardBlocks"]


def test_g_telegram_separates_structure_live_momentum_and_action():
    message = format_decision_message({
        "event_type": "SHORT_INVALIDATING", "currentPrice": 102.0,
        "structuralBias": "BEARISH", "liveBiasState": "INVALIDATING",
        "executionBias": "NEUTRAL", "invalidationLevel": 100.0,
        "canonicalDecision": {"liveBiasEvaluation": {
            "structuralBias": "BEARISH", "liveMomentum": "STRONG_LONG",
            "liveBiasState": "INVALIDATING", "executionBias": "NEUTRAL",
        }},
    })
    assert "結構方向：🔴 原結構偏空" in message
    assert "即時動能：🟢 明顯轉強" in message
    assert "目前操作：🟡 暫停舊方向，等待15M收盤確認" in message
    assert "優先找空" not in message
