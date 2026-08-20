from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import DecisionEvent, TelegramNotification
from app.db.session import db_session, init_db
from app.engines.unified_decision_state import evaluate_unified_decision
from app.services.decision_outbox import (
    deliver_pending_telegram,
    format_telegram_event,
    persist_decision_events,
    telegram_delivery_status,
)


def market(price, *, entry_status="INVALIDATED", direction="SHORT", event_type=""):
    return {
        "symbol": "XAUUSD-OUTBOX-TEST",
        "version": 88,
        "timestamp_utc": "2026-08-20T14:30:00+00:00",
        "market_decision": {"action": "WATCH", "reason": "等待確認"},
        "entry_engine": {
            "status": entry_status,
            "direction": direction,
            "zone_low": 4483,
            "zone_high": 4485,
            "stop_loss": 4478,
            "take_profit_1": 4499,
            "take_profit_2": 4505,
            "take_profit_3": 4511,
        },
        "directional_alert": {"event_type": event_type},
        "normalized_analysis": {
            "currentPrice": price,
            "marketDataTimestamp": "2026-08-20T14:30:00+00:00",
            "lastClosedCandleTimestamp": "2026-08-20T14:30:00+00:00",
            "lastClosedCandlePrice": 4484,
            "marketDataStatus": "GOOD",
            "consistencyValid": True,
            "atr15": 8,
            "marketStateCode": "RECOVERY",
            "confirmationLevels": [
                {"kind": "support", "timeframe": "15M", "price": 4484},
                {"kind": "resistance", "timeframe": "15M", "price": 4490},
            ],
        },
    }


def test_false_breakout_continues_to_bullish_recovery_and_long_watch():
    previous = {"state": "SHORT_WATCH", "source_price": 4479}
    recovered, events = evaluate_unified_decision(
        market(4484, event_type="FALSE_BREAKOUT"), previous
    )
    assert [event["currentState"] for event in events[:3]] == [
        "SHORT_INVALIDATED",
        "FALSE_BREAKOUT",
        "BULLISH_RECOVERY",
    ]
    intrabar, _ = evaluate_unified_decision(market(4480), recovered)
    assert intrabar["state"] == "BULLISH_RECOVERY"
    stronger, events = evaluate_unified_decision(market(4494), intrabar)
    assert stronger["state"] == "BULLISH_RECOVERY"
    assert any(event["event_type"] == "AWAIT_CLOSE_CONFIRMATION" for event in events)
    watch, events = evaluate_unified_decision(
        market(4494, entry_status="SETUP_WATCH", direction="LONG"), stronger
    )
    assert watch["state"] == "LONG_WATCH"
    assert any(event["currentState"] == "LONG_WATCH" for event in events)


@pytest.mark.asyncio
async def test_outbox_retries_persists_message_id_and_deduplicates_restart():
    init_db()
    _state, events = evaluate_unified_decision(
        market(4484, event_type="FALSE_BREAKOUT"),
        {"state": "SHORT_WATCH", "source_price": 4479},
    )
    event = events[-1]
    created = persist_decision_events("XAUUSD-OUTBOX-TEST", [event])
    assert len(created) == 1
    assert persist_decision_events("XAUUSD-OUTBOX-TEST", [event]) == []

    calls = 0

    async def flaky_sender(message):
        nonlocal calls
        calls += 1
        assert f"現價：{event['currentPrice']:.2f}" in message
        if calls == 1:
            raise RuntimeError("temporary")
        return "tg-message-991"

    assert (
        await deliver_pending_telegram(sender=flaky_sender, event_id=event["eventId"])
        == 0
    )
    with db_session() as db:
        row = db.execute(
            select(TelegramNotification).where(
                TelegramNotification.event_id == event["eventId"]
            )
        ).scalar_one()
        assert row.status == "FAILED"
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert (
        await deliver_pending_telegram(sender=flaky_sender, event_id=event["eventId"])
        == 1
    )
    with db_session() as db:
        notification = db.execute(
            select(TelegramNotification).where(
                TelegramNotification.event_id == event["eventId"]
            )
        ).scalar_one()
        stored = db.execute(
            select(DecisionEvent).where(DecisionEvent.event_id == event["eventId"])
        ).scalar_one()
        assert notification.status == "SENT"
        assert notification.message_id == "tg-message-991"
        assert notification.sent_at is not None
        assert stored.payload["currentPrice"] == event["currentPrice"]
        assert stored.payload["currentState"] == event["currentState"]
        count = (
            db.execute(
                select(TelegramNotification).where(
                    TelegramNotification.event_id == event["eventId"]
                )
            )
            .scalars()
            .all()
        )
        assert len(count) == 1
    status = telegram_delivery_status()
    assert status["status"] == "SENT"
    assert status["messageId"] == "tg-message-991"


def test_telegram_message_uses_plain_chinese_not_internal_only():
    _state, events = evaluate_unified_decision(
        market(4484, event_type="FALSE_BREAKOUT"),
        {"state": "SHORT_WATCH", "source_price": 4479},
    )
    message = format_telegram_event(events[-1])
    assert "行情轉強" in message
    assert "未持倉" in message
    assert "資料時間" in message


def test_outbox_worker_is_scheduled_within_five_seconds():
    from app.services.scheduler import build_scheduler

    scheduler = build_scheduler()
    job = scheduler.get_job("telegram_outbox")
    assert job is not None
    assert job.trigger.interval.total_seconds() <= 5
