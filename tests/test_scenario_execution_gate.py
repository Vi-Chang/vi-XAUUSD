from copy import deepcopy

from app.engines.canonical_decision import build_canonical_decision
from app.engines.decision_presentation import format_decision_message
from app.engines.final_decision_engine import evaluate_final_decision
from app.engines.scenario_execution import (
    can_execute_scenario,
    candidate_crossed_invalidation,
)
from app.services.alert_aggregator import notification_fingerprint
from tests.test_final_decision_engine import market


def test_directional_hard_invalidation_is_mirrored():
    assert candidate_crossed_invalidation(
        direction="LONG", current_price=97.9, invalidation_price=98.0)
    assert not candidate_crossed_invalidation(
        direction="LONG", current_price=99.0, invalidation_price=98.0)
    assert candidate_crossed_invalidation(
        direction="SHORT", current_price=102.1, invalidation_price=102.0)
    assert not candidate_crossed_invalidation(
        direction="SHORT", current_price=101.0, invalidation_price=102.0)


def test_central_gate_requires_every_execution_condition():
    gate = can_execute_scenario(
        direction="LONG", current_price=100.5, invalidation_price=98.0,
        lifecycle_state="ENTRY_READY", data_health="HEALTHY",
        entry_confirmation="READY", closed_candle_confirmed=True,
        in_executable_zone=True, risk_valid=True, rr_valid=True,
        stop_valid=True, expires_at="2026-08-21T16:00:00Z",
        evaluated_at="2026-08-21T15:01:00Z")
    assert gate["executionAllowed"]
    assert gate["scenarioValidity"] == "ACTIVE"
    blocked = {**gate, **can_execute_scenario(
        direction="LONG", current_price=100.5, invalidation_price=98.0,
        lifecycle_state="ENTRY_READY", data_health="STALE",
        entry_confirmation="BLOCKED_BY_DATA", closed_candle_confirmed=False,
        in_executable_zone=True, risk_valid=True, rr_valid=True,
        stop_valid=True)}
    assert not blocked["executionAllowed"]
    assert blocked["scenarioValidity"] == "BLOCKED_BY_DATA"


def test_price_crossing_candidate_stop_invalidates_only_scenario_not_htf_bias():
    data = market(price=97.9)
    decision, events = evaluate_final_decision(data)
    assert decision["finalAction"] == "NO_TRADE"
    assert decision["scenarioValidity"] == "INVALIDATED"
    assert decision["candidateInvalidated"] is True
    assert decision["marketBias"] == "BULLISH"
    assert decision["marketBiasChanged"] is False
    assert decision["entrySignal"] != "READY"
    invalidated = next(event for event in events
                       if event["event_type"] == "SCENARIO_INVALIDATED")
    assert invalidated["scenarioValidity"] == "INVALIDATED"
    assert invalidated["executionAllowed"] is False


def test_canonical_and_telegram_hide_invalidated_execution_prices():
    data = market(price=97.9)
    final, _events = evaluate_final_decision(data)
    canonical = build_canonical_decision(data, final)
    assert canonical["scenarioValidity"] == "INVALIDATED"
    assert canonical["executionAllowed"] is False
    assert canonical["newEntryDecision"]["action"] == "WAIT"
    message = format_decision_message({
        "event_type": "SCENARIO_INVALIDATED", "currentPrice": 97.9,
        "canonicalDecision": canonical,
    })
    assert "原進場、停損與止盈：暫不具執行效力" in message
    assert "高週期方向" not in message or "未被這次失效改寫" in message


def test_scenario_validity_is_a_meaningful_telegram_identity_dimension():
    base = {
        "symbol": "XAUUSD", "event_type": "WAIT", "setupId": "S-1",
        "currentState": "WAIT", "marketBias": "BULLISH",
        "entryConfirmation": "READY", "defenseState": "HELD",
        "dataHealth": "HEALTHY", "scenarioValidity": "ACTIVE",
    }
    changed = deepcopy(base)
    changed["scenarioValidity"] = "INVALIDATED"
    assert notification_fingerprint(base) != notification_fingerprint(changed)
