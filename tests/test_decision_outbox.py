from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.models import DecisionEvent, TelegramNotification
from app.db.session import db_session, init_db
from app.engines.unified_decision_state import evaluate_unified_decision
from app.services.alert_aggregator import semantic_key
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
        "LONG_BIAS",
    ]
    intrabar, _ = evaluate_unified_decision(market(4480), recovered)
    assert intrabar["state"] == "LONG_BIAS"
    stronger, events = evaluate_unified_decision(market(4494), intrabar)
    assert stronger["state"] == "LONG_BIAS"
    assert any(event["event_type"] == "AWAIT_CLOSE_CONFIRMATION" for event in events)
    watch, events = evaluate_unified_decision(
        market(4494, entry_status="SETUP_WATCH", direction="LONG"), stronger
    )
    assert watch["state"] == "LONG_BIAS"


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
    assert "行情偏多" in message
    assert "目前動作" in message
    assert "資料時間" in message


def test_watch_telegram_is_yellow_and_explicitly_forbids_entry():
    event = {
        "currentState": "LONG_WATCH", "direction": "LONG",
        "currentPrice": 4529.96, "latestClosedCandlePrice": 4530,
        "candleCloseTime": "2026-08-20T15:15:00+00:00",
        "calculatedAt": "2026-08-20T15:16:00+00:00",
        "missingCondition": "15M 收盤尚未站上 4532.51",
        "confirmation": "等 15 分鐘收盤站上 4532.51",
        "cancelCondition": "15M 收盤跌破 4520.00",
    }
    message = format_telegram_event(event)
    assert message.startswith("🟡【偏多等待確認｜尚不可進場】")
    assert "目前動作：等待，尚不可進場，請勿追價。" in message
    assert "最新已收盤 15M：4530.00" in message
    assert "下一個觸發：等 15 分鐘收盤站上 4532.51" in message


def test_ready_telegram_has_complete_entry_and_risk_plan():
    event = {
        "currentState": "LONG_READY", "direction": "LONG",
        "currentPrice": 4534, "latestClosedCandlePrice": 4533,
        "candleCloseTime": "2026-08-20T15:30:00+00:00",
        "calculatedAt": "2026-08-20T15:31:00+00:00",
        "entryZone": {"low": 4527, "high": 4529}, "stopLoss": 4520,
        "targets": [4540, 4550], "cancelCondition": "15M 收盤跌破 4520",
    }
    message = format_telegram_event(event)
    assert message.startswith("🟢【多單進場條件成立】")
    assert "建議進場區間：4527–4529" in message
    assert "防守價：4520" in message
    assert "分批止盈價：4540／4550" in message


def test_tp_and_stop_telegram_are_actionable_and_semantically_distinct():
    base = {
        "currentState": "LONG_MANAGE", "currentPrice": 4510,
        "calculatedAt": "2026-08-21T03:01:00+00:00",
    }
    tp = {**base, "positionEvent": {
        "tradePlanId": "tp-long-1", "event_type": "TAKE_PROFIT_1",
        "side": "LONG", "price": 4510, "targetPrice": 4510,
        "percent": 30, "newProtectionPrice": 4500, "nextLevel": 4520,
    }}
    tp_message = format_telegram_event(tp)
    assert tp_message.startswith("🟢【多單第一止盈觸發】")
    assert "若你持有多單：建議平倉 30%" in tp_message
    assert "剩餘部位防守調整至：4500.00" in tp_message
    stop = {**base, "currentState": "SHORT_MANAGE", "positionEvent": {
        "tradePlanId": "tp-short-1", "event_type": "STOP_TRIGGERED",
        "side": "SHORT", "price": 4560, "percent": 100,
        "newProtectionPrice": 4560,
    }}
    stop_message = format_telegram_event(stop)
    assert stop_message.startswith("🔴【空單防守條件已觸發】")
    assert "依風控規則退出" in stop_message
    assert "不是止盈訊號" in stop_message


def test_outbox_worker_is_scheduled_within_five_seconds():
    from app.services.scheduler import build_scheduler

    scheduler = build_scheduler()
    job = scheduler.get_job("telegram_outbox")
    assert job is not None
    assert job.trigger.interval.total_seconds() <= 5


@pytest.mark.asyncio
async def test_same_cycle_three_facts_make_one_telegram_call():
    init_db()
    base = market(4523.22)
    base["normalized_analysis"]["lastClosedCandleTimestamp"] = "2026-08-20T15:34:00+00:00"
    facts = []
    reasons = [
        ("EXIT_ZONE_REACHED", "價格進入條件式出場區"),
        ("EXIT_NOW", "反向收盤突破防守價"),
        ("BULLISH_CONTINUATION", "連續收盤站穩突破位，多方延續"),
    ]
    for index, (kind, reason) in enumerate(reasons):
        facts.append({
            "eventId": f"raw-fact-{index}", "event_type": kind,
            "previousState": "WAIT", "currentState": "MISSED_ENTRY",
            "transitionReason": reason, "triggerReason": reason,
            "currentPrice": 4523.22, "candleCloseTime": "2026-08-20T15:34:00+00:00",
            "calculatedAt": "2026-08-20T15:34:05+00:00", "dataVersion": 2039,
            "flatAction": "等待價格回踩新的確認區",
            "longManage": "防守 4449.56，依序分批止盈",
            "shortManage": "防守 4496.93，立即降低風險",
            "confirmation": "等待 15 分鐘收盤確認新結構",
        })
    created = persist_decision_events("XAUUSD-AGGREGATION-TEST", facts)
    assert len(created) == 1
    assert len(created[0]["signalFacts"]) == 3
    with db_session() as db:
        assert db.scalar(select(func.count()).select_from(TelegramNotification).where(
            TelegramNotification.semantic_dedup_key == created[0]["semanticDedupKey"])) == 1
        assert db.scalar(select(func.count()).select_from(DecisionEvent).where(
            DecisionEvent.event_id == created[0]["eventId"])) == 1
    calls = []

    async def sender(message):
        calls.append(message)
        return "single-message-1"

    assert await deliver_pending_telegram(sender=sender, event_id=created[0]["eventId"]) == 1
    assert len(calls) == 1
    assert all(reason in calls[0] for _, reason in reasons)
    assert calls[0].count("• ") == 3
    assert await deliver_pending_telegram(sender=sender, event_id=created[0]["eventId"]) == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_two_workers_claim_same_semantic_notification_once():
    import asyncio

    facts = [{
        "eventId": "multi-worker-raw", "event_type": "EXIT_NOW",
        "previousState": "WAIT", "currentState": "MISSED_ENTRY",
        "transitionReason": "反向收盤突破防守價", "currentPrice": 4524,
        "candleCloseTime": "2026-08-20T15:49:00+00:00",
        "calculatedAt": "2026-08-20T15:49:01+00:00", "dataVersion": 2040,
    }]
    created = persist_decision_events("XAUUSD-MULTI-WORKER", facts)
    calls = 0

    async def slow_sender(_message):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return "one-worker-won"

    results = await asyncio.gather(
        deliver_pending_telegram(sender=slow_sender, event_id=created[0]["eventId"]),
        deliver_pending_telegram(sender=slow_sender, event_id=created[0]["eventId"]),
    )
    assert sum(results) == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_late_fact_edits_sent_message_instead_of_sending_again():
    base = {
        "eventId": "late-fact-a", "event_type": "EXIT_ZONE_REACHED",
        "previousState": "WAIT", "currentState": "MISSED_ENTRY",
        "transitionReason": "價格進入條件式出場區", "currentPrice": 4523.22,
        "candleCloseTime": "2026-08-20T16:04:00+00:00",
        "calculatedAt": "2026-08-20T16:04:01+00:00", "dataVersion": 2041,
    }
    first = persist_decision_events("XAUUSD-LATE-FACT", [base])[0]
    sends, edits = 0, 0

    async def sender(_message):
        nonlocal sends
        sends += 1
        return "editable-message"

    async def editor(message_id, message):
        nonlocal edits
        edits += 1
        assert message_id == "editable-message"
        assert "反向收盤突破防守價" in message
        return message_id

    assert await deliver_pending_telegram(sender=sender, event_id=first["eventId"]) == 1
    late = {**base, "eventId": "late-fact-b", "event_type": "EXIT_NOW",
            "transitionReason": "反向收盤突破防守價"}
    updated = persist_decision_events("XAUUSD-LATE-FACT", [late])
    assert len(updated) == 1 and len(updated[0]["signalFacts"]) == 2
    assert await deliver_pending_telegram(sender=sender, editor=editor,
                                          event_id=first["eventId"]) == 1
    assert sends == 1 and edits == 1


def test_live_prices_share_dedup_key_for_same_basis_candle_state_and_trigger():
    base = {
        "symbol": "XAUUSD", "timeframe": "15M",
        "decisionBasisCandleCloseTime": "2026-08-20T15:45:00+00:00",
        "currentState": "MISSED_ENTRY", "alertCategory": "MISSED_ENTRY",
        "triggerLevel": 4495.12,
    }
    keys = {semantic_key({**base, "currentPrice": price,
                          "calculatedAt": f"2026-08-20T15:{minute}:00+00:00"})
            for price, minute in ((4517, 50), (4519, 51), (4520, 56))}
    assert len(keys) == 1


def test_direction_is_part_of_semantic_dedup_identity():
    base = {
        "symbol": "XAUUSD", "timeframe": "15M",
        "decisionBasisCandleCloseTime": "2026-08-20T15:45:00+00:00",
        "currentState": "WAIT", "alertCategory": "WAIT", "triggerLevel": 4532.51,
    }
    assert semantic_key({**base, "direction": "LONG"}) != semantic_key({
        **base, "direction": "SHORT"
    })


def test_setup_id_is_part_of_state_transition_dedup_identity():
    base = {
        "symbol": "XAUUSD", "timeframe": "15M",
        "decisionBasisCandleCloseTime": "2026-08-21T03:00:00+00:00",
        "direction": "LONG", "currentState": "ENTRY_READY",
        "alertCategory": "ENTRY_READY", "triggerLevel": 4539.17,
    }
    assert semantic_key({**base, "setupId": "setup-a"}) != semantic_key({
        **base, "setupId": "setup-b"
    })


def test_breakout_setup_dedup_ignores_quote_and_confidence_only_changes():
    base = {
        "symbol": "XAUUSD", "setupId": "BO-4567", "direction": "LONG",
        "currentState": "WAIT_RETEST", "triggerLevel": 4567.88,
        "entryZone": {"low": 4566.88, "high": 4568.88},
        "blockedReason": "等待回踩固定區間", "event_type": "WAIT_RETEST",
        "breakoutSetupEvent": {"setupId": "BO-4567"},
    }
    assert semantic_key({**base, "currentPrice": 4580, "signalScore": 100}) == semantic_key({
        **base, "currentPrice": 4590, "signalScore": 90,
        "calculatedAt": "2026-08-21T11:00:00+00:00",
    })
    assert semantic_key(base) != semantic_key({**base, "triggerLevel": 4601.09})


def test_notification_validator_rejects_completed_next_trigger():
    bad = {
        "eventId": "bad-completed-next", "currentState": "LONG_WATCH",
        "currentPrice": 4520.91, "candleCloseTime": "2026-08-20T15:45:00+00:00",
        "latestClosedCandlePrice": 4520.50,
        "nextTriggerCondition": {"condition": "closeAbove", "level": 4495.12,
                                 "timeframe": "15M", "status": "PENDING"},
        "triggerLevel": 4495.12,
    }
    assert persist_decision_events("XAUUSD-VALIDATION", [bad]) == []


def test_message_lists_completed_trigger_separately_from_next_trigger():
    event = {
        "currentState": "MISSED_ENTRY", "currentPrice": 4520.91,
        "transitionReason": "連續收盤站穩突破位，多方延續",
        "completedTriggers": [{"condition": "closeAbove", "level": 4495.12,
                               "status": "SATISFIED"}],
        "confirmation": "原突破條件已完成，正在等待新結構形成",
    }
    message = format_telegram_event(event)
    assert "已完成：15M 收盤站上 4495.12" in message
    assert "下一個觸發：原突破條件已完成，正在等待新結構形成" in message
