from app.engines.decision_presentation import format_decision_message
from app.engines.multi_timeframe_bias import derive_multi_timeframe_bias
from app.engines.user_facing_trade_message import (
    assert_no_nullish_user_facing_text,
    sanitize_user_facing_payload,
)
from app.services.notification_coordinator import coordinate_notification_intents
from app.services.notification_policy import (
    eligibility,
    has_meaningful_action_delta,
    user_visible_state_fingerprint,
)


def _multi():
    return derive_multi_timeframe_bias({"timeframeAssessments": [
        {"timeframe": "15M", "trend": "bearish"},
        {"timeframe": "1H", "trend": "bearish"},
        {"timeframe": "4H", "trend": "bullish", "momentum": "pullback"},
        {"timeframe": "1D", "trend": "bullish"},
    ]}, canonical_bias="BULLISH")


def _prepare(**updates):
    value = {
        "event_type": "EARLY_ENTRY_PREPARE", "currentState": "PREPARE",
        "candidateSide": "SHORT", "currentPrice": 4660.0,
        "candidateZone": {"low": 4658.0, "high": 4662.0},
        "candidateDefenseLevel": 4668.0, "candidateReasons": ["RESISTANCE_REJECTION"],
        "multiTimeframeBias": _multi(), "setupId": "SHORT-1",
    }
    value.update(updates)
    return value


def test_1_short_term_bearish_macro_bullish():
    snapshot = _multi()
    assert snapshot["shortTermBias"] == "SHORT_TERM_BEARISH"
    assert snapshot["macroBias"] == "MACRO_BULLISH"
    assert snapshot["alignment"] == "COUNTERTREND"


def test_2_canonical_bullish_does_not_hide_bearish_short_timeframes():
    message = format_decision_message(_prepare(marketBias="BULLISH"))
    assert "15M：🔴 偏空" in message and "1H：🔴 偏空" in message
    assert "短線：🔴 偏空" in message and "大方向：🟢 偏多" in message
    assert "市場方向：🟢 偏多" not in message


def test_3_null_reason_is_omitted():
    message = format_decision_message(_prepare(candidateReasons=None, transitionReason=None))
    assert "None" not in message and "原因：None" not in message


def test_4_undefined_trigger_line_is_omitted():
    message = format_decision_message(_prepare(nextTrigger="undefined"))
    assert "undefined" not in message.lower()


def test_5_irrelevant_missing_position_is_omitted():
    message = format_decision_message(_prepare(positionKnown=False))
    assert "未取得實際持倉" not in message


def test_6_temporary_15m_missing_without_action_delta_is_log_only():
    decision = eligibility({"event_type": "DATA_DELAYED", "currentState": "WAIT",
                            "entryConfirmation": "BLOCKED_BY_DATA"})
    assert decision["eligible"] is False and decision["reasonCode"] == "LOW_PRIORITY"


def test_7_same_health_episode_has_stable_user_visible_identity():
    first = {"event_type": "DATA_STALE", "currentState": "DATA_STALE",
             "dataIncidentId": "INC-1", "criticalDataBlock": True}
    later = {**first, "currentPrice": 4661, "calculatedAt": "later"}
    assert user_visible_state_fingerprint(first) == user_visible_state_fingerprint(later)
    assert has_meaningful_action_delta(first, later) == (False, "NO_ACTION_DELTA")


def test_8_recovery_relevant_to_active_setup_is_actionable_once():
    recovered = {"event_type": "DATA_RECOVERED", "currentState": "PREPARE",
                 "activeSetupId": "LONG-1", "recoveryRelevant": True}
    assert eligibility(recovered)["eligible"] is True
    assert has_meaningful_action_delta(recovered, recovered) == (False, "NO_ACTION_DELTA")


def test_9_wait_to_wait_is_suppressed():
    old = {"event_type": "WAIT", "currentState": "WAIT"}
    new = {**old, "currentPrice": 4661}
    assert has_meaningful_action_delta(old, new)[0] is False


def test_10_prepare_to_entry_ready_is_actionable():
    ready = {**_prepare(), "event_type": "ENTRY_READY",
             "currentState": "ENTRY_READY", "canEnter": True}
    assert has_meaningful_action_delta(_prepare(), ready)[0] is True
    assert eligibility(ready)["userPriority"] == "P1"


def test_11_prepare_to_invalidated_is_actionable():
    invalid = {**_prepare(), "event_type": "EARLY_ENTRY_INVALIDATED",
               "currentState": "INVALIDATED"}
    assert has_meaningful_action_delta(_prepare(), invalid)[0] is True
    assert eligibility(invalid)["userPriority"] == "P2"


def test_12_every_rendered_payload_is_nullish_safe():
    raw = _prepare(reason=None, nextTrigger="null", diagnostic=float("nan"), empty="—")
    clean = sanitize_user_facing_payload(raw)
    message = format_decision_message(clean)
    assert_no_nullish_user_facing_text(message)
    for token in ("None", "null", "undefined", "NaN"):
        assert token.lower() not in message.lower()


def test_13_same_snapshot_without_action_delta_is_not_user_facing():
    events = coordinate_notification_intents("XAUUSD", [{
        "eventId": "wait-1", "eventVersion": 1, "event_type": "WAIT",
        "currentState": "WAIT", "evaluationCycleId": "cycle-1",
    }, {
        "eventId": "data-1", "eventVersion": 1, "event_type": "DATA_DELAYED",
        "currentState": "WAIT", "evaluationCycleId": "cycle-1",
    }])
    assert len(events) == 1
    assert eligibility(events[0])["eligible"] is False


def test_14_four_timeframes_and_composites_are_shown():
    message = format_decision_message(_prepare())
    assert all(label in message for label in ("15M：", "1H：", "4H：", "1D："))
    assert "4H：🟠 多頭修正／短線偏空" in message
    assert "短線：🔴 偏空" in message and "大方向：🟢 偏多" in message
