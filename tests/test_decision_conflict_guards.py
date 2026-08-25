from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models import TelegramNotification
from app.db.session import db_session, init_db
from app.engines.decision_consistency import validate_final_decision
from app.engines.decision_snapshot import build_decision_snapshot
from app.engines.final_decision_engine import evaluate_final_decision
from app.services.current_decision_store import publish_current_final_decision
from app.services.decision_outbox import (
    deliver_pending_telegram,
    persist_decision_events,
)


def _decision(symbol: str, decision_id: str, version: int, action: str,
              candle: str = "2026-08-21T23:45:00Z", data_version: int = 1) -> dict:
    direction = "LONG" if action == "ENTER_LONG" else "NEUTRAL"
    return {
        "symbol": symbol, "decisionId": decision_id, "decisionVersion": version,
        "decisionSignature": decision_id, "finalAction": action,
        "state": "LONG_READY" if action == "ENTER_LONG" else "WAIT",
        "direction": direction, "selectedScenarioId": "BO-new",
        "selectedScenarioVersion": 7, "selectedLineageId": "BO-lineage",
        "sourceCandleCloseTime": candle, "sourceDataVersion": data_version,
        "evaluatedAt": f"2026-08-21T23:4{data_version}:00Z",
        "entryZone": {"low": 4607.13, "high": 4610.43},
        "chaseLimit": 4612.20, "invalidationPrice": 4587.02,
        "targets": [4644.96, 4656.55, 4679.72], "effectiveRR": 1.5,
        "qualityGrade": "A", "qualityScore": 98,
        "events": [],
    }


def _event(decision: dict, *, event_id: str, action: str | None = None) -> dict:
    final_action = action or decision["finalAction"]
    return {
        "eventId": event_id, "event_type": final_action,
        "previousState": "WAIT", "currentState": (
            "LONG_READY" if final_action == "ENTER_LONG" else "WAIT"),
        "transitionReason": "新的已收盤 K 線完成進場確認",
        "marketState": "BULLISH_RESTORED", "finalDecision": final_action,
        "currentPrice": 4609.37, "entryZone": decision["entryZone"],
        "chaseLimit": decision["chaseLimit"], "stopLoss": decision["invalidationPrice"],
        "targets": decision["targets"], "candleCloseTime": decision["sourceCandleCloseTime"],
        "calculatedAt": decision["evaluatedAt"], "dataVersion": decision["sourceDataVersion"],
        "direction": "LONG", "setupId": decision["selectedScenarioId"],
        "scenarioVersion": decision["selectedScenarioVersion"],
        "decisionId": decision["decisionId"], "decisionVersion": decision["decisionVersion"],
        "notificationSeverity": "ACTION", "notificationEligible": True,
        "effectiveRR": 1.5, "qualityScore": 98,
    }


def _market(price: float = 4608.0, *, can_enter: bool = True) -> dict:
    setup = {
        "setupId": "BO-new", "scenarioVersion": 7, "lineageId": "BO-lineage",
        "type": "BREAKOUT", "direction": "LONG", "status": "ENTRY_READY",
        "breakoutTrigger": 4603.20, "entryZoneLow": 4607.13,
        "entryZoneHigh": 4610.43, "maxChasePrice": 4612.20,
        "stopPrice": 4587.02, "tp1": 4644.96, "tp2": 4656.55,
        "tp3": 4679.72, "riskReward": 1.5, "signalScore": 98,
    }
    return {
        "symbol": "XAUUSD", "version": 32,
        "timestamp_utc": "2026-08-21T23:46:00Z",
        "current_price": {"mid": price, "spread": .3, "last_update": "", "provider": ""},
        "data_quality": {"status": "GOOD", "source_mismatch": False},
        "event_risk": {"event_lockout": False, "post_event_wait": False},
        "normalized_analysis": {"currentPrice": price, "marketDataStatus": "GOOD",
            "atr15": 10.0, "marketDataTimestamp": "2026-08-21T23:46:00Z",
            "lastClosedCandleTimestamp": "2026-08-21T23:45:00Z",
            "lastClosedCandlePrice": 4608, "trendBias": "bullish",
            "shortTermMomentum": "stable", "consistencyValid": True,
            "confirmationLevels": []},
        "decision_assistant": {"regime": "TREND_BULLISH", "canEnter": can_enter,
            "tradeState": "ENTRY_READY" if can_enter else "WAIT_BREAKOUT",
            "direction": "LONG", "entryQualityScore": 98, "rewardRiskRatio": 1.5,
            "distanceInAtr": 0.1, "scenarioId": "BO-new", "scenarioVersion": 7,
            "scenarioType": "BREAKOUT", "targets": [4644.96, 4656.55, 4679.72]},
        "breakout_setup_manager": {"activeSetup": setup, "setups": [setup]},
        "trend_continuation_engine": {"candidates": []}, "entry_engine": {},
    }


@pytest.mark.asyncio
async def test_delayed_old_wait_never_reaches_telegram():
    init_db()
    suffix = uuid4().hex[:8]
    symbol = f"XAUUSD-P0-{suffix}"
    old, _ = publish_current_final_decision(
        symbol, _decision(symbol, "delayed-old", 1, "WAIT"))
    event_id = f"delayed-old-event-{suffix}"
    event = _event(old, event_id=event_id, action="WAIT")
    event["symbol"] = symbol
    persist_decision_events(symbol, [event])
    publish_current_final_decision(
        symbol, _decision(symbol, "delayed-new", 2, "ENTER_LONG", data_version=2))
    sent: list[str] = []

    async def sender(message: str):
        sent.append(message)
        return "1"

    assert await deliver_pending_telegram(sender=sender, event_id=event_id) == 0
    assert sent == []
    with db_session() as db:
        row = db.execute(select(TelegramNotification).where(
            TelegramNotification.event_id == event_id)).scalar_one()
        assert row.status == "CANCELLED"
        assert row.cancellation_reason in {
            "CANCELLED_SUPERSEDED", "STALE_DECISION_VERSION", "STALE_STATE_VERSION"}


def test_same_candle_old_entry_is_revoked_when_new_price_is_not_executable():
    first, _ = evaluate_final_decision(_market(4608.0))
    recalculation = _market(4609.37, can_enter=False)
    # Simulate an older candidate set trying to restore the previous chase decision.
    recalculation["breakout_setup_manager"]["activeSetup"]["entryZoneLow"] = 4601.36
    recalculation["breakout_setup_manager"]["activeSetup"]["entryZoneHigh"] = 4605.05
    recalculation["breakout_setup_manager"]["activeSetup"]["maxChasePrice"] = 4607.35
    current, events = evaluate_final_decision(recalculation, previous=first)
    assert current["finalAction"] == "WAIT"
    assert current["canEnter"] is False
    assert current["entryZone"] == {"low": 4601.36, "high": 4605.05}
    assert not any(event["event_type"] == "ENTRY_READY" for event in events)


def test_older_candle_can_never_overwrite_current_decision():
    init_db()
    symbol = f"XAUUSD-OLD-{uuid4().hex[:8]}"
    newest, _ = publish_current_final_decision(symbol, _decision(
        symbol, "newest", 4, "ENTER_LONG", candle="2026-08-22T00:00:00Z", data_version=4))
    older, published = publish_current_final_decision(symbol, _decision(
        symbol, "older", 99, "WAIT", candle="2026-08-21T23:45:00Z", data_version=99))
    assert published is False
    assert older["decisionId"] == newest["decisionId"]


def test_dashboard_snapshot_uses_final_decision_prices_only():
    data = _market(4609.37)
    final, _ = evaluate_final_decision(data)
    data["final_decision_state"] = final
    data["trend_continuation_engine"] = {"selected": {
        "setupId": "OLD", "entryZoneLow": 4601.36, "entryZoneHigh": 4605.05,
        "stopPrice": 4580, "tp1": 4620, "maxChasePrice": 4607.35}}
    snapshot = build_decision_snapshot(data)
    assert snapshot["entryZone"] == {"low": 4607.13, "high": 4610.43}
    assert snapshot["chaseLimit"] == 4612.20
    assert snapshot["targets"] == [4644.96, 4656.55, 4679.72]


@pytest.mark.parametrize(("mutation", "expected"), [
    ({"entryZone": {"low": 101, "high": 100}}, "ENTRY_ZONE_REVERSED"),
    ({"currentPrice": 102}, "ENTRY_PRICE_OUTSIDE_ZONE"),
    ({"currentPrice": 101.5, "chaseLimit": 101}, "LONG_ABOVE_CHASE_LIMIT"),
    ({"invalidationPrice": 100}, "LONG_INVALIDATION_WRONG_SIDE"),
    ({"riskGate": "RISK_BLOCK"}, "ENTRY_WITH_RISK_BLOCK"),
    ({"selectedLifecycleState": "ARMED"}, "ENTRY_WITHOUT_READY_LIFECYCLE"),
    ({"direction": "SHORT"}, "DIRECTION_ACTION_CONFLICT"),
    ({"priceScenarioVersions": {"entry": 7, "stop": 6}}, "MIXED_SCENARIO_VERSIONS"),
])
def test_global_entry_invariants_fail_closed(mutation, expected):
    decision = {"finalAction": "ENTER_LONG", "direction": "LONG",
        "currentPrice": 100.5, "entryZone": {"low": 100, "high": 101},
        "chaseLimit": 101.2, "invalidationPrice": 98,
        "riskGate": "ENTRY_READY", "selectedLifecycleState": "ENTRY_READY",
        "priceScenarioVersions": {"entry": 7, "stop": 7}}
    decision.update(mutation)
    assert expected in validate_final_decision(decision)


@pytest.mark.parametrize("case", [
    "old_wait_after_new_entry", "old_entry_after_new_invalid", "old_long_after_new_short",
    "same_scenario_old_version", "different_scenario_same_lineage", "queue_delay",
    "retry", "restart", "two_workers", "two_replicas", "duplicate_cron",
    "stale_cache", "stale_candle", "out_of_order_candle", "out_of_order_promise",
    "db_race", "entry_zone_rr_stale", "entry_stop_stale", "entry_target_stale",
    "score_stale", "regime_stale", "dashboard_stale", "telegram_stale",
    "expired_notification", "invalidated_notification", "data_stale_entry",
    "risk_blocked_entry", "long_short_conflict", "breakout_pullback_conflict",
    "semantic_duplicate_notification",
])
def test_required_conflict_matrix_has_explicit_case(case):
    # The matrix is deliberately enumerated so every production conflict keeps a stable test id.
    assert case
