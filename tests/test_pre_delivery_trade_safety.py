from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models import Candle, LivePrice, TelegramNotification
from app.db.session import db_session, init_db
from app.services.current_decision_store import publish_current_final_decision
from app.services.decision_outbox import (
    deliver_pending_telegram,
    persist_decision_events,
)
from app.services.pre_delivery_trade_safety import validate_pre_delivery

NOW = datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc)
CANDLE = NOW - timedelta(minutes=15)


def _decision(symbol: str, **updates) -> dict:
    base = {
        "symbol": symbol, "decisionSignature": uuid4().hex,
        "finalAction": "ENTER_LONG", "state": "LONG_READY", "direction": "LONG",
        "selectedScenarioId": "BO-4607", "selectedScenarioVersion": 3,
        "sourceCandleCloseTime": CANDLE.isoformat(), "sourceDataVersion": 8,
        "decisionCreatedAt": (NOW - timedelta(seconds=10)).isoformat(),
        "evaluatedAt": (NOW - timedelta(seconds=10)).isoformat(),
        "validUntil": (NOW + timedelta(seconds=110)).isoformat(),
        "entryReadyValidUntil": (NOW + timedelta(seconds=110)).isoformat(),
        "currentPrice": 4608.0, "atr15": 10.0,
        "entryZone": {"low": 4607.13, "high": 4610.43},
        "chaseLimit": 4610.43, "invalidationPrice": 4587.02,
        "targets": [4644.96, 4656.55, 4679.72],
        "riskGate": "ENTRY_READY", "events": [],
    }
    base.update(updates)
    return base


def _market(symbol: str, *, bid=4607.8, ask=4608.0, quote_time=NOW,
            candle_time=CANDLE, spread=.2) -> None:
    with db_session() as db:
        db.add(LivePrice(symbol=symbol, bid=bid, ask=ask, mid=(bid + ask) / 2,
                         spread=spread, provider="TEST", quote_time=quote_time,
                         received_at=quote_time))
        if candle_time is not None:
            db.add(Candle(symbol=symbol, timeframe="15M",
                          open_time=candle_time - timedelta(minutes=15),
                          close_time=candle_time, open=4600, high=4610, low=4599,
                          close=4608, volume=1, is_closed=True,
                          data_provider="TEST", received_at=quote_time))


def _prepared(**decision_updates):
    init_db()
    symbol = f"XAU-PD-{uuid4().hex[:8]}"
    current, _ = publish_current_final_decision(symbol, _decision(symbol, **decision_updates))
    queued = {
        "symbol": symbol, "event_type": "ENTRY_READY",
        "finalDecision": current["finalAction"],
        "decisionId": current["decisionId"], "decisionVersion": current["decisionVersion"],
        "scenarioVersion": current["selectedScenarioVersion"],
        "currentState": "LONG_READY", "currentPrice": current["currentPrice"],
    }
    return symbol, current, queued


def _validate(*, market=None, queued_at=None, now=NOW, **updates):
    symbol, current, queued = _prepared(**updates)
    _market(symbol, **(market or {}))
    with db_session() as db:
        result = validate_pre_delivery(
            db, symbol=symbol, queued_payload=queued,
            queued_at=queued_at or NOW - timedelta(seconds=10), now=now)
    return result, symbol, current, queued


def test_regression_delayed_4607_entry_is_blocked_at_4617():
    result, *_ = _validate(
        market={"bid": 4616.8, "ask": 4617.0},
        queued_at=NOW - timedelta(minutes=14),
    )
    assert result.allowed is False
    assert result.reason == "ENTRY_PRICE_OUT_OF_RANGE"
    assert result.snapshot["decision_price"] == 4608.0
    assert result.snapshot["delivery_price"] == 4617.0
    assert result.snapshot["queue_age_seconds"] == 840


@pytest.mark.parametrize(("name", "decision", "market", "queue_delta", "expected"), [
    ("high", {}, {"ask": 4611.0}, 10, "ENTRY_PRICE_OUT_OF_RANGE"),
    ("low", {}, {"ask": 4606.0}, 10, "ENTRY_PRICE_OUT_OF_RANGE"),
    ("queue", {}, {}, 121, "NOTIFICATION_TOO_OLD"),
    ("decision", {"decisionCreatedAt": (NOW-timedelta(seconds=121)).isoformat()}, {}, 10, "STALE_DECISION"),
    ("expired", {"entryReadyValidUntil": (NOW-timedelta(seconds=1)).isoformat()}, {}, 10, "ENTRY_READY_EXPIRED"),
    ("tick", {}, {"quote_time": NOW-timedelta(seconds=31)}, 10, "LATEST_TICK_STALE"),
    ("candle", {}, {"candle_time": NOW}, 10, "NEW_CLOSED_CANDLE_REQUIRES_REEVALUATION"),
    ("spread", {}, {"spread": 11.0}, 10, "SPREAD_TOO_HIGH"),
    ("drift", {"currentPrice": 4604.0}, {}, 10, "PRICE_DRIFT_REQUIRES_REEVALUATION"),
    ("rr", {"targets": [4620.0]}, {}, 10, "RR_REVALIDATION_FAILED"),
    ("risk", {"riskGate": "EVENT_BLACKOUT"}, {}, 10, "RISK_GATE_NOT_READY"),
    ("plan", {"chaseLimit": 4609.0}, {}, 10, "INVALID_ENTRY_PLAN"),
    ("target", {"targets": [4607.9]}, {}, 10, "TARGET_ALREADY_REACHED"),
    ("stop", {"invalidationPrice": 4608.1}, {}, 10, "INVALIDATION_ALREADY_TRIGGERED"),
])
def test_delivery_invalidation_matrix(name, decision, market, queue_delta, expected):
    result, *_ = _validate(
        market=market, queued_at=NOW-timedelta(seconds=queue_delta), **decision)
    assert result.allowed is False, name
    assert result.reason == expected


def test_scenario_and_decision_supersession_are_blocked():
    symbol, _current, queued = _prepared()
    _market(symbol)
    queued["scenarioVersion"] = 2
    with db_session() as db:
        result = validate_pre_delivery(
            db, symbol=symbol, queued_payload=queued,
            queued_at=NOW-timedelta(seconds=5), now=NOW)
    assert result.reason == "SCENARIO_SUPERSEDED"
    queued["scenarioVersion"] = 3
    queued["decisionId"] = "old"
    with db_session() as db:
        result = validate_pre_delivery(
            db, symbol=symbol, queued_payload=queued,
            queued_at=NOW-timedelta(seconds=5), now=NOW)
    assert result.reason == "SUPERSEDED_DECISION"


def test_missing_tick_and_candle_fail_closed():
    symbol, _, queued = _prepared()
    with db_session() as db:
        result = validate_pre_delivery(
            db, symbol=symbol, queued_payload=queued,
            queued_at=NOW-timedelta(seconds=5), now=NOW)
    assert result.reason == "LATEST_TICK_STALE"
    _market(symbol, candle_time=None)
    with db_session() as db:
        result = validate_pre_delivery(
            db, symbol=symbol, queued_payload=queued,
            queued_at=NOW-timedelta(seconds=5), now=NOW)
    assert result.reason == "CANDLE_DATA_MISSING"


def test_old_closed_candle_fails_closed_even_when_decision_is_recent():
    old_candle = NOW - timedelta(minutes=30)
    result, *_ = _validate(
        sourceCandleCloseTime=old_candle.isoformat(),
        market={"candle_time": old_candle},
    )
    assert result.reason == "LATEST_CLOSED_CANDLE_STALE"


@pytest.mark.asyncio
async def test_outbox_revalidates_and_never_calls_telegram_for_stale_entry(monkeypatch):
    symbol, current, queued = _prepared()
    _market(symbol, bid=4616.8, ask=4617.0)
    event_id = uuid4().hex
    event = {
        **queued, "eventId": event_id, "previousState": "WAIT",
        "transitionReason": "15M 收盤確認", "candleCloseTime": CANDLE.isoformat(),
        "calculatedAt": NOW.isoformat(), "dataVersion": 8,
        "entryZone": current["entryZone"], "stopLoss": current["invalidationPrice"],
        "targets": current["targets"], "direction": "LONG",
        "setupId": current["selectedScenarioId"], "notificationEligible": True,
    }
    assert persist_decision_events(symbol, [event])
    monkeypatch.setattr("app.services.decision_outbox.datetime", type(
        "FixedDateTime", (), {"now": staticmethod(lambda tz=None: NOW)}))
    calls = []

    async def sender(message):
        calls.append(message)
        return "should-not-send"

    assert await deliver_pending_telegram(sender=sender, event_id=event_id) == 0
    assert calls == []
    with db_session() as db:
        row = db.execute(select(TelegramNotification).where(
            TelegramNotification.event_id == event_id)).scalar_one()
        assert row.status == "CANCELLED"
        assert row.cancellation_reason == "ENTRY_PRICE_OUT_OF_RANGE"
        assert row.decision_snapshot["deliveryValidation"]["delivery_price"] == 4617.0


def test_valid_entry_passes_and_uses_delivery_ask():
    result, *_ = _validate()
    assert result.allowed is True
    assert result.reason == "PASS"
    assert result.render_payload["currentPrice"] == 4608.0
    assert result.render_payload["effectiveRR"] >= 1.5


def test_short_entry_uses_bid_and_is_symmetric():
    result, *_ = _validate(
        finalAction="ENTER_SHORT", state="SHORT_READY", direction="SHORT",
        currentPrice=4592.0, entryZone={"low": 4590.0, "high": 4593.0},
        chaseLimit=4590.0, invalidationPrice=4610.0, targets=[4560.0],
        market={"bid": 4592.0, "ask": 4592.2},
    )
    assert result.allowed is True
    assert result.snapshot["delivery_price"] == 4592.0
