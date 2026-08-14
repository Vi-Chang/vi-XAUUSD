from types import SimpleNamespace

from app.config import get_settings
from app.schemas.analysis import NormalizedAnalysisState
from app.services.tactical_shadow import (
    build_tactical_shadow,
    outcome_action,
    shadow_setup_state,
    signal_mode,
)


def _row(action="WATCH", shadow=None):
    return SimpleNamespace(decision_action=action, result_json={"tactical_shadow": shadow or {}})


def test_observe_is_recorded_but_not_scored():
    record = build_tactical_shadow(
        NormalizedAnalysisState(setupState="OBSERVE"), current_price=4400,
        created_at="2026-08-14T00:00:00+00:00", settings=get_settings())
    assert record.direction == "NONE"
    assert record.eligibleForOutcome is False
    assert record.liveAdviceEnabled is False


def test_short_watch_is_eligible_shadow_not_live_advice():
    state = NormalizedAnalysisState(
        setupState="SHORT_WATCH", tacticalBias="bearish", triggerLevel=4390,
        invalidationLevel=4410, expiresAt="2026-08-14T01:00:00+00:00")
    record = build_tactical_shadow(
        state, current_price=4400, created_at="2026-08-14T00:00:00+00:00",
        settings=get_settings())
    assert record.direction == "SHORT"
    assert record.eligibleForOutcome is True
    assert record.liveAdviceEnabled is False
    assert record.parameters["minimumRiskReward"] == get_settings().tactical_min_rr


def test_no_chase_keeps_tactical_direction_for_measurement():
    record = build_tactical_shadow(
        NormalizedAnalysisState(setupState="NO_CHASE", tacticalBias="bearish"),
        current_price=4380, created_at="2026-08-14T00:00:00+00:00",
        settings=get_settings())
    assert record.direction == "SHORT"
    assert record.eligibleForOutcome is True


def test_live_action_always_wins_over_shadow():
    row = _row("PREPARE_LONG", {
        "enabled": True, "eligibleForOutcome": True, "direction": "SHORT",
        "setupState": "SHORT_WATCH"})
    assert outcome_action(row) == "PREPARE_LONG"
    assert signal_mode(row) == "LIVE"
    assert shadow_setup_state(row) == "SHORT_WATCH"


def test_watch_uses_only_explicitly_eligible_shadow():
    eligible = _row(shadow={"enabled": True, "eligibleForOutcome": True,
                            "direction": "SHORT", "setupState": "NO_CHASE"})
    disabled = _row(shadow={"enabled": False, "eligibleForOutcome": True,
                            "direction": "SHORT"})
    assert outcome_action(eligible) == "SHORT"
    assert signal_mode(eligible) == "SHADOW"
    assert shadow_setup_state(eligible) == "NO_CHASE"
    assert outcome_action(disabled) is None
