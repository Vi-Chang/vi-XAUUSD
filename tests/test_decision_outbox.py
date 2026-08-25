from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.models import DecisionEvent, TelegramNotification
from app.db.session import db_session, init_db
from app.engines.unified_decision_state import evaluate_unified_decision
from app.services.alert_aggregator import (
    is_meaningful_change,
    notification_fingerprint,
    notification_fingerprint_parts,
    semantic_key,
)


def test_micro_quote_and_chase_drift_are_not_meaningful():
    previous = _breakout_dedup_event(
        event_id="meaningful-old", trigger=4591.37, chase=4595.21)
    current = {**_breakout_dedup_event(
        event_id="meaningful-new", trigger=4591.37, chase=4595.46),
               "currentPrice": 4581.84}
    meaningful, reason = is_meaningful_change(previous, current)
    assert meaningful is False
    assert reason == "NO_MEANINGFUL_DECISION_CHANGE"


def test_new_pullback_zone_and_state_transitions_are_meaningful():
    previous = _breakout_dedup_event(event_id="pullback-old")
    with_zone = _breakout_dedup_event(event_id="pullback-created")
    with_zone["breakoutSetupEvent"]["setup"].update({
        "pullbackEntryZoneLow": 4574.0, "pullbackEntryZoneHigh": 4579.0,
    })
    assert is_meaningful_change(previous, with_zone) == (True, "PULLBACK_ZONE_CREATED")
    entered = _breakout_dedup_event(
        event_id="pullback-entered", status="WAIT_PULLBACK_CONFIRMATION")
    assert is_meaningful_change(with_zone, entered)[0] is True
    ready = _breakout_dedup_event(
        event_id="pullback-ready", status="PULLBACK_ENTRY_READY")
    assert is_meaningful_change(entered, ready)[0] is True
from app.services.decision_outbox import (
    deliver_pending_telegram,
    format_telegram_event,
    persist_decision_events,
    reconcile_unknown_deliveries,
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
        assert notification.status == "CONFIRMED"
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
    assert status["status"] == "CONFIRMED"
    assert status["messageId"] == "tg-message-991"


def test_telegram_message_uses_plain_chinese_not_internal_only():
    _state, events = evaluate_unified_decision(
        market(4484, event_type="FALSE_BREAKOUT"),
        {"state": "SHORT_WATCH", "source_price": 4479},
    )
    message = format_telegram_event(events[-1])
    assert "行情偏多" in message
    assert "現在先不要進場" in message
    assert "資料時間" in message


@pytest.mark.asyncio
async def test_health_test_notification_is_not_cancelled_by_current_market_decision():
    """System health events are not stale trading advice and must survive supersession."""
    from app.services.current_decision_store import publish_current_final_decision

    init_db()
    publish_current_final_decision("XAUUSD-TEST-HEALTH", {
        "decisionSignature": "current-market-decision",
        "sourceCandleCloseTime": "2026-08-24T01:45:00+00:00",
        "sourceDataVersion": 10, "evaluatedAt": "2026-08-24T01:46:00+00:00",
        "finalAction": "WAIT", "direction": "NEUTRAL", "events": [],
    })
    event = {
        "eventId": "telegram-health-test-not-superseded",
        "event_type": "TEST_NOTIFICATION", "previousState": "WAIT",
        "currentState": "WAIT", "transitionReason": "Telegram 測試通知",
        "marketState": "", "finalDecision": "WAIT", "currentPrice": 4600,
        "entryZone": None, "stopLoss": None, "targets": [],
        "candleCloseTime": "2026-08-24T01:45:00+00:00",
        "calculatedAt": "2026-08-24T01:46:00+00:00", "dataVersion": 10,
        "flatAction": "這是一則測試，不代表交易訊號",
        "symbol": "XAUUSD-TEST-HEALTH",
    }
    assert len(persist_decision_events("XAUUSD-TEST-HEALTH", [event])) == 1
    sent_messages = []

    async def sender(message):
        sent_messages.append(message)
        return "telegram-health-message-1"

    assert await deliver_pending_telegram(
        sender=sender, event_id=event["eventId"]) == 1
    assert len(sent_messages) == 1
    with db_session() as db:
        row = db.execute(select(TelegramNotification).where(
            TelegramNotification.event_id == event["eventId"])).scalar_one()
        assert row.status == "CONFIRMED"
        assert row.message_id == "telegram-health-message-1"


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
    assert message.startswith("【XAUUSD 現在怎麼做】")
    assert "🟡 現在先不要進場" in message
    assert "最新已收盤 15M：4530.00" in message
    assert "下一個觸發：等 15 分鐘收盤站上 4532.51" in message


def test_ready_telegram_has_complete_entry_and_risk_plan():
    event = {
        "currentState": "LONG_READY", "direction": "LONG",
        "canEnter": True, "finalAction": "ENTER_LONG",
        "currentPrice": 4534, "latestClosedCandlePrice": 4533,
        "candleCloseTime": "2026-08-20T15:30:00+00:00",
        "calculatedAt": "2026-08-20T15:31:00+00:00",
        "entryZone": {"low": 4527, "high": 4529}, "stopLoss": 4520,
        "targets": [4540, 4550], "cancelCondition": "15M 收盤跌破 4520",
    }
    message = format_telegram_event(event)
    assert message.startswith("【XAUUSD 現在怎麼做】")
    assert "🟢 現在可以進場" in message
    assert "建議進場區間：4527–4529" in message
    assert "防守價：4520" in message
    assert "分批止盈價：4540／4550" in message


@pytest.mark.parametrize(("direction", "kind", "title", "name", "invalidation"), [
    ("LONG", "SHALLOW_PULLBACK_LONG", "多單進場條件成立", "淺回踩續漲", "收盤跌破 4520.00"),
    ("SHORT", "SHALLOW_PULLBACK_SHORT", "空單進場條件成立", "淺反彈續跌", "收盤站上 4550.00"),
])
def test_continuation_telegram_is_directionally_symmetric(
        direction, kind, title, name, invalidation):
    setup = {
        "setupId": f"tc-{direction.lower()}", "direction": direction, "type": kind,
        "entryZoneLow": 4530, "entryZoneHigh": 4532, "suggestedEntry": 4531,
        "stopPrice": 4520 if direction == "LONG" else 4550,
        "tp1": 4545 if direction == "LONG" else 4515,
        "tp2": 4560 if direction == "LONG" else 4500,
        "tp3": 4580 if direction == "LONG" else 4480,
        "riskReward": 1.5, "signalScore": 82,
    }
    event = {
        "currentState": f"{direction}_READY", "currentPrice": 4531,
        "calculatedAt": "2026-08-21T12:30:00+00:00",
        "trendContinuationEvent": {
            "setupId": setup["setupId"], "direction": direction, "setup": setup,
        },
    }
    message = format_telegram_event(event)
    assert title in message
    assert f"劇本：{name}" in message
    assert invalidation in message
    assert "TP1：" in message and "TP2：" in message and "TP3：" in message


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
        ("EXIT_ZONE_REACHED", "價格已到達本次計畫的明確分批處理價區"),
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
async def test_ten_workers_claim_same_semantic_notification_once():
    import asyncio

    init_db()
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

    results = await asyncio.gather(*[
        deliver_pending_telegram(sender=slow_sender, event_id=created[0]["eventId"])
        for _ in range(10)
    ])
    assert sum(results) == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_ten_concurrent_emitters_create_one_atomic_outbox_claim():
    import asyncio

    init_db()
    symbol = "XAUUSD-ATOMIC-EMIT"

    def emit(_index: int):
        return persist_decision_events(symbol, [{
            "eventId": "atomic-emitter-shared", "eventVersion": 1,
            "event_type": "DATA_DELAYED",
            "dataHealthEventKey": "DATA_DELAYED:INC-ATOMIC",
            "dataIncidentId": "INC-ATOMIC", "previousState": "WAIT",
            "currentState": "DATA_STALE", "transitionReason": "行情延遲",
            "finalDecision": "WAIT", "currentPrice": 4660,
            "candleCloseTime": "2026-08-25T04:30:00+00:00",
            "calculatedAt": "2026-08-25T04:30:01+00:00", "dataVersion": 102,
        }])

    results = await asyncio.gather(*[
        asyncio.to_thread(emit, index) for index in range(10)
    ])
    # Some callers may observe the same pending aggregate being enriched, but
    # database ownership and delivery remain exactly one.
    assert any(results)
    with db_session() as db:
        rows = db.execute(select(TelegramNotification).where(
            TelegramNotification.symbol == symbol)).scalars().all()
        assert len(rows) == 1
        assert rows[0].event_key
        assert rows[0].status == "PENDING"
        event_id = rows[0].event_id
    sends = 0

    async def sender(_message):
        nonlocal sends
        sends += 1
        return "atomic-emitter-message"

    delivered = await asyncio.gather(*[
        deliver_pending_telegram(sender=sender, event_id=event_id)
        for _ in range(10)
    ])
    assert sum(delivered) == 1
    assert sends == 1


@pytest.mark.asyncio
async def test_data_health_message_uses_latest_canonical_bias_only():
    from app.services.current_decision_store import (
        get_canonical_market_bias,
        publish_current_final_decision,
    )

    init_db()
    symbol = "XAUUSD-CANONICAL-BIAS"
    publish_current_final_decision(symbol, {
        "decisionSignature": "bias-long", "marketBias": "BULLISH",
        "sourceCandleCloseTime": "2026-08-25T04:00:00+00:00",
        "sourceDataVersion": 100, "evaluatedAt": "2026-08-25T04:00:01+00:00",
        "finalAction": "WAIT", "events": [],
    })
    current, _ = publish_current_final_decision(symbol, {
        "decisionSignature": "bias-short", "marketBias": "BEARISH",
        "entryConfirmation": "BLOCKED_BY_DATA", "dataHealth": "DEGRADED",
        "sourceCandleCloseTime": "2026-08-25T04:15:00+00:00",
        "sourceDataVersion": 101, "evaluatedAt": "2026-08-25T04:15:01+00:00",
        "finalAction": "WAIT", "events": [],
    })
    assert get_canonical_market_bias(symbol) == "BEARISH"
    event = {
        "eventId": "canonical-bias-data-delayed", "eventVersion": 1,
        "event_type": "DATA_DELAYED", "dataHealthEventKey": "DATA_DELAYED:INC-1",
        "dataIncidentId": "INC-1", "marketBias": "BULLISH",
        "previousState": "WAIT", "currentState": "DATA_STALE",
        "transitionReason": "行情延遲", "finalDecision": "WAIT",
        "currentPrice": 4660, "candleCloseTime": "2026-08-25T04:15:00+00:00",
        "calculatedAt": "2026-08-25T04:15:01+00:00", "dataVersion": 101,
        "decisionId": current["decisionId"], "decisionVersion": current["decisionVersion"],
    }
    created = persist_decision_events(symbol, [event])
    assert len(created) == 1
    assert created[0]["marketBias"] == "BEARISH"
    assert created[0]["canonicalDecision"]["marketBias"] == "BEARISH"


@pytest.mark.asyncio
async def test_stale_state_version_is_dropped_before_delivery():
    from app.services.current_decision_store import publish_current_final_decision

    init_db()
    symbol = "XAUUSD-STALE-VERSION"
    current, _ = publish_current_final_decision(symbol, {
        "decisionSignature": "v100", "marketBias": "BULLISH",
        "sourceCandleCloseTime": "2026-08-25T05:00:00+00:00",
        "sourceDataVersion": 100, "evaluatedAt": "2026-08-25T05:00:01+00:00",
        "finalAction": "WAIT", "events": [],
    })
    event = {
        "eventId": "stale-v100-notification", "eventVersion": 1,
        "event_type": "DATA_DELAYED", "dataHealthEventKey": "DATA_DELAYED:INC-STALE",
        "dataIncidentId": "INC-STALE", "previousState": "WAIT",
        "currentState": "DATA_STALE", "transitionReason": "行情延遲",
        "finalDecision": "WAIT", "currentPrice": 4650,
        "candleCloseTime": "2026-08-25T05:00:00+00:00",
        "calculatedAt": "2026-08-25T05:00:01+00:00", "dataVersion": 100,
        "decisionId": current["decisionId"], "decisionVersion": current["decisionVersion"],
    }
    assert persist_decision_events(symbol, [event])
    publish_current_final_decision(symbol, {
        "decisionSignature": "v101", "marketBias": "BEARISH",
        "sourceCandleCloseTime": "2026-08-25T05:15:00+00:00",
        "sourceDataVersion": 101, "evaluatedAt": "2026-08-25T05:15:01+00:00",
        "finalAction": "WAIT", "events": [],
    })
    calls = 0

    async def sender(_message):
        nonlocal calls
        calls += 1
        return "must-not-send"

    assert await deliver_pending_telegram(
        sender=sender, event_id=event["eventId"]) == 0
    assert calls == 0
    with db_session() as db:
        row = db.execute(select(TelegramNotification).where(
            TelegramNotification.event_id == event["eventId"])).scalar_one()
        assert row.status == "CANCELLED"
        assert row.cancellation_reason == "STALE_STATE_VERSION"


@pytest.mark.asyncio
async def test_identical_rendered_payload_is_secondary_deduplicated():
    symbol = "XAUUSD-PAYLOAD-HASH"
    base = {
        "event_type": "EXIT_WARNING", "previousState": "LONG_MANAGE",
        "currentState": "LONG_MANAGE", "transitionReason": "防守條件接近",
        "currentPrice": 4668, "candleCloseTime": "2026-08-25T06:00:00+00:00",
        "calculatedAt": "2026-08-25T06:00:01+00:00", "dataVersion": 200,
        "direction": "LONG", "scenarioId": "same-rendered-scenario",
    }
    first = persist_decision_events(symbol, [{**base, "eventId": "payload-hash-a"}])
    assert first

    async def sender(_message):
        return "payload-hash-message"

    assert await deliver_pending_telegram(
        sender=sender, event_id=first[0]["eventId"]) == 1
    # A separately-created event identity with the same user-facing decision is
    # suppressed by the rendered payload hash during the cooldown window.
    repeated = {**base, "eventId": "payload-hash-b", "scenarioId": "different-id"}
    assert persist_decision_events(symbol, [repeated]) == []
    with db_session() as db:
        assert db.scalar(select(func.count()).select_from(TelegramNotification).where(
            TelegramNotification.symbol == symbol)) == 1


@pytest.mark.asyncio
async def test_distinct_exit_event_type_is_not_mistaken_for_duplicate():
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
    assert len(updated) == 1
    assert await deliver_pending_telegram(
        sender=sender, editor=editor, event_id=updated[0]["eventId"]) == 1
    assert sends == 2 and edits == 0


def _breakout_dedup_event(*, event_id: str, status="WAIT_BREAKOUT_CONFIRMATION",
                          trigger=4606.18, chase=4609.88,
                          calculated="2026-08-21T13:56:00+00:00"):
    setup = {"setupId": "BO-dedup-4606", "direction": "LONG", "status": status,
             "breakoutTrigger": trigger, "maxChasePrice": chase,
             "stopPrice": 4590.0, "createdFromCandleTime": "2026-08-21T13:45:00+00:00",
             "entryZoneLow": trigger, "entryZoneHigh": chase}
    return {"eventId": event_id, "symbol": "XAUUSD-DEDUP-T1-T6",
            "setupId": setup["setupId"], "direction": "LONG",
            "currentState": status, "event_type": status,
            "currentPrice": 4584.31, "triggerLevel": trigger,
            "stopLoss": 4590.0, "entryZone": {"low": trigger, "high": chase},
            "candleCloseTime": "2026-08-21T13:45:00+00:00",
            "decisionBasisCandleCloseTime": "2026-08-21T13:45:00+00:00",
            "calculatedAt": calculated, "dataVersion": 1,
            "breakoutSetupEvent": {"setupId": setup["setupId"],
                                   "currentState": status, "setup": setup}}


@pytest.mark.asyncio
async def test_persisted_wait_suppresses_micro_drift_even_across_fingerprint_bucket():
    first = _breakout_dedup_event(
        event_id="micro-persisted-first", trigger=4591.37, chase=4595.91)
    first["symbol"] = "XAUUSD-MICRO-PERSISTED"
    first["breakoutSetupEvent"]["setup"]["setupId"] = "BO-micro-persisted"
    first["setupId"] = "BO-micro-persisted"
    created = persist_decision_events(first["symbol"], [first])
    assert len(created) == 1

    async def sender(_message):
        return "micro-persisted-message"

    assert await deliver_pending_telegram(
        sender=sender, event_id=created[0]["eventId"]) == 1
    repeated = _breakout_dedup_event(
        event_id="micro-persisted-second", trigger=4591.37, chase=4596.16,
        calculated="2026-08-21T14:01:00+00:00")
    repeated["symbol"] = first["symbol"]
    repeated["currentPrice"] = 4581.84
    repeated["breakoutSetupEvent"]["setup"]["setupId"] = "BO-micro-persisted"
    repeated["setupId"] = "BO-micro-persisted"
    assert notification_fingerprint(repeated) != notification_fingerprint(first)
    assert persist_decision_events(first["symbol"], [repeated]) == []


@pytest.mark.asyncio
async def test_t1_to_t6_stable_breakout_fingerprint_and_retry():
    t1 = _breakout_dedup_event(event_id="dedup-t1")
    parts = notification_fingerprint_parts(t1)
    assert parts == {"symbol": "XAUUSD-DEDUP-T1-T6", "scenarioId": "BO-dedup-4606",
                     "direction": "LONG", "status": "WAIT_BREAKOUT_CONFIRMATION",
                     "triggerPrice": "4606.18", "chaseLimit": "4609.88",
                     "invalidationPrice": "4590.00",
                     "sourceCandleTime": "",
                     "canonicalStateVersion": "1",
                     "canonicalState": "WAIT_BREAKOUT_CONFIRMATION",
                     "marketBias": "NEUTRAL", "entryConfirmation": "",
                     "defenseState": "", "dataHealth": "",
                     "primaryTriggerId": "BO-dedup-4606"}
    first = persist_decision_events("XAUUSD-DEDUP-T1-T6", [t1])
    assert len(first) == 1
    deliveries = []

    async def sender(message):
        deliveries.append(message)
        return "dedup-first-message"

    assert await deliver_pending_telegram(sender=sender, event_id=first[0]["eventId"]) == 1

    # T2/T3: scheduler time, quote and event identity change; decision does not.
    for index, minute in enumerate((58, 59), start=2):
        repeated = {**_breakout_dedup_event(
            event_id=f"dedup-t{index}", calculated=f"2026-08-21T13:{minute}:00+00:00"),
                    "currentPrice": 4585 + index, "dataVersion": index}
        assert notification_fingerprint(repeated) == notification_fingerprint(t1)
        assert persist_decision_events("XAUUSD-DEDUP-T1-T6", [repeated]) == []
    assert len(deliveries) == 1

    # T4: a material trigger/chase update is a new decision.
    changed = _breakout_dedup_event(event_id="dedup-t4", trigger=4608.20, chase=4611.90)
    assert notification_fingerprint(changed) != notification_fingerprint(t1)
    assert len(persist_decision_events("XAUUSD-DEDUP-T1-T6", [changed])) == 1

    # T5: ENTRY_READY transition must notify immediately.
    ready = _breakout_dedup_event(event_id="dedup-t5", status="ENTRY_READY_BREAKOUT")
    ready_created = persist_decision_events("XAUUSD-DEDUP-T1-T6", [ready])
    assert len(ready_created) == 1

    # T6: worker failures before acceptance may retry, but successful delivery is once.
    accepted = []
    attempts = 0

    async def flaky(message):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary failure before Telegram accepted")
        accepted.append(message)
        return "dedup-ready-message"

    for _ in range(3):
        await deliver_pending_telegram(sender=flaky, event_id=ready_created[0]["eventId"])
    assert attempts == 3 and len(accepted) == 1
    assert await deliver_pending_telegram(sender=flaky,
                                          event_id=ready_created[0]["eventId"]) == 0


@pytest.mark.asyncio
async def test_telegram_timeout_is_not_automatically_resent():
    event = _breakout_dedup_event(event_id="dedup-timeout", trigger=4612.0, chase=4615.0)
    created = persist_decision_events("XAUUSD-DEDUP-T1-T6", [event])
    calls = 0

    async def ambiguous_timeout(_message):
        nonlocal calls
        calls += 1
        raise TimeoutError("response lost after possible acceptance")

    assert await deliver_pending_telegram(
        sender=ambiguous_timeout, event_id=created[0]["eventId"]) == 0
    assert await deliver_pending_telegram(
        sender=ambiguous_timeout, event_id=created[0]["eventId"]) == 0
    assert calls == 1
    with db_session() as db:
        notice = db.execute(select(TelegramNotification).where(
            TelegramNotification.event_id == created[0]["eventId"])).scalar_one()
        assert notice.status == "DELIVERY_UNKNOWN"


@pytest.mark.asyncio
async def test_delivery_unknown_receipt_and_same_fingerprint_never_resend():
    event = _breakout_dedup_event(
        event_id="unknown-fingerprint-1", trigger=4696.75, chase=4700.0)
    event.update({"decisionVersion": 7,
                  "candleCloseTime": "2026-08-24T06:00:00+00:00"})
    created = persist_decision_events("XAUUSD-UNKNOWN-FP", [event])
    calls = 0

    async def ambiguous_receipt(_message):
        nonlocal calls
        calls += 1
        return "DELIVERY_UNKNOWN"

    assert await deliver_pending_telegram(
        sender=ambiguous_receipt, event_id=created[0]["eventId"]) == 0
    for index in range(2, 4):
        repeated = {**event, "eventId": f"unknown-fingerprint-{index}"}
        assert persist_decision_events("XAUUSD-UNKNOWN-FP", [repeated]) == []
    assert await deliver_pending_telegram(
        sender=ambiguous_receipt, event_id=created[0]["eventId"]) == 0
    assert calls == 1
    assert reconcile_unknown_deliveries()["unresolved"] >= 1
    with db_session() as db:
        notice = db.execute(select(TelegramNotification).where(
            TelegramNotification.event_id == created[0]["eventId"])).scalar_one()
        assert notice.status == "DELIVERY_UNKNOWN"


@pytest.mark.asyncio
async def test_expired_and_new_wait_share_new_setup_fingerprint():
    old = _breakout_dedup_event(event_id="combined-old", status="EXPIRED",
                                trigger=4601.09, chase=4604.30)
    new = _breakout_dedup_event(event_id="combined-new", trigger=4606.18, chase=4609.88)
    new_setup = new["breakoutSetupEvent"]["setup"]
    new_setup["setupId"] = "BO-new-4606"
    new["setupId"] = "BO-new-4606"
    for item in (old, new):
        item["evaluationCycleId"] = "expired-plus-new-cycle"
    old["breakoutSetups"] = [old["breakoutSetupEvent"]["setup"], new_setup]
    created = persist_decision_events("XAUUSD-COMBINED-UPDATE", [old, new])
    assert len(created) == 1 and created[0]["factCount"] == 2
    assert created[0]["semanticDedupKey"] == notification_fingerprint({
        **new, "symbol": "XAUUSD-COMBINED-UPDATE"})

    async def sender(_message):
        return "combined-update-message"

    assert await deliver_pending_telegram(sender=sender,
                                          event_id=created[0]["eventId"]) == 1
    repeated_wait = {**new, "eventId": "combined-new-repeat",
                     "calculatedAt": "2026-08-21T14:00:00+00:00", "dataVersion": 2}
    assert persist_decision_events("XAUUSD-COMBINED-UPDATE", [repeated_wait]) == []


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
