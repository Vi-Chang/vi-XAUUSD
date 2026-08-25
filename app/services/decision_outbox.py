"""Durable DecisionEvent + Telegram transactional outbox."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select

from app.config import get_settings
from app.db.models import (
    CurrentFinalDecision,
    DecisionEvent,
    MarketMonitorState,
    NotificationAudit,
    TelegramNotification,
)
from app.db.session import db_session
from app.engines.decision_presentation import format_decision_message
from app.engines.trigger_lifecycle import validate_notification
from app.services.alert_aggregator import (
    aggregate_signal_facts,
    is_meaningful_change,
)
from app.services.notification_policy import (
    canonical_dedupe_key,
    eligibility,
    is_expired,
)
from app.services.pre_delivery_trade_safety import (
    audit_delivery_block,
    transition_blocked_entry,
    validate_pre_delivery,
)

logger = logging.getLogger(__name__)
DELIVERED_STATUSES = ("SENT", "CONFIRMED")


class DeliveryUnknownError(RuntimeError):
    """Telegram may have accepted the message but its receipt was lost."""


def _last_sent_market_decision(db, symbol: str, payload: dict) -> dict | None:
    """Find the latest delivered decision in the same direction/market stream."""
    direction = str(payload.get("direction") or "NONE")
    rows = db.execute(
        select(DecisionEvent)
        .join(TelegramNotification,
              TelegramNotification.event_id == DecisionEvent.event_id)
        .where(DecisionEvent.symbol == symbol,
               TelegramNotification.status.in_(DELIVERED_STATUSES))
        .order_by(TelegramNotification.sent_at.desc())
        .limit(50)
    ).scalars().all()
    for row in rows:
        old = dict(row.payload or {})
        if str(old.get("direction") or "NONE") == direction:
            return old
    return None


def persist_decision_events(symbol: str, events: list[dict]) -> list[dict]:
    """Persist facts but enqueue only one semantic notification per group."""
    created: list[dict] = []
    now = datetime.now(timezone.utc)
    canonical = [dict(event) for event in events if event.get("eventVersion")]
    legacy = [event for event in events if not event.get("eventVersion")]
    events = canonical + aggregate_signal_facts(symbol, legacy)
    valid_events = []
    for payload in events:
        payload["symbol"] = symbol
        decision = (eligibility(payload) if payload.get("eventVersion") else {
            "eligible": True, "reasonCode": "SEND_LEGACY_MEANINGFUL_EVENT",
            "priority": "IMPORTANT"})
        payload["notificationDecision"] = decision
        payload["notificationEligible"] = decision["eligible"]
        if payload.get("eventVersion"):
            raw_key = canonical_dedupe_key(payload)
            payload["semanticDedupKey"] = hashlib.sha256(raw_key.encode()).hexdigest()
        errors = validate_notification(payload)
        if errors and decision["eligible"]:
            logger.error("notification validation failed: %s", ",".join(errors))
            continue
        valid_events.append(payload)
    events = valid_events
    with db_session() as db:
        for payload in events:
            event_id = str(payload.get("eventId") or "")
            semantic_key = str(payload.get("semanticDedupKey") or "")
            if not event_id:
                continue
            current = db.execute(select(CurrentFinalDecision).where(
                CurrentFinalDecision.symbol == symbol)).scalar_one_or_none()
            payload_decision_id = str(payload.get("decisionId") or "")
            is_test = str(payload.get("event_type") or "") == "TEST_NOTIFICATION"
            if (current is not None and not is_test
                    and payload_decision_id != current.decision_id):
                logger.warning("non-current decision event rejected before enqueue: %s", event_id)
                db.add(NotificationAudit(
                    event_id=event_id,
                    event_type=str(payload.get("event_type") or ""),
                    eligible=False,
                    reason_code="SKIP_STALE_EVENT",
                    dedupe_key=semantic_key,
                    payload={"decisionId": payload_decision_id,
                             "currentDecisionId": current.decision_id},
                    created_at=now,
                ))
                continue
            notice_decision = payload.get("notificationDecision") or eligibility(payload)
            existing_notice = db.execute(select(TelegramNotification).where(
                TelegramNotification.semantic_dedup_key == semantic_key
            )).scalar_one_or_none() if semantic_key else None
            if existing_notice is not None:
                db.add(NotificationAudit(
                    event_id=event_id,
                    event_type=str(payload.get("event_type") or ""),
                    eligible=False,
                    reason_code="SKIP_DUPLICATE_EVENT",
                    dedupe_key=semantic_key,
                    payload={"originalEventId": existing_notice.event_id},
                    created_at=now,
                ))
                # The durable unique fingerprint is the authority. A scheduler
                # rerun creates fresh eventIds/dataVersions, but no new market
                # decision. Never turn a successfully sent fingerprint back
                # into PENDING/EDIT_PENDING.
                if existing_notice.status in (*DELIVERED_STATUSES, "DELIVERY_UNKNOWN"):
                    continue
                canonical = db.execute(select(DecisionEvent).where(
                    DecisionEvent.event_id == existing_notice.event_id)).scalar_one()
                old = dict(canonical.payload or {})
                old_fact_ids = {str(item.get("eventId") or "")
                                for item in old.get("signalFacts") or [old]}
                incoming_facts = list(payload.get("signalFacts") or [])
                incoming_ids = {str(item.get("eventId") or "") for item in incoming_facts}
                if incoming_ids and incoming_ids.issubset(old_fact_ids):
                    continue
                merged = aggregate_signal_facts(symbol,
                    list(old.get("signalFacts") or [old]) + incoming_facts)[0]
                merged["eventId"] = canonical.event_id
                canonical.payload = merged
                canonical.transition_reason = str(merged.get("transitionReason") or "")
                canonical.current_price = float(merged.get("currentPrice") or canonical.current_price)
                # Merge only while the original row has not been delivered.
                # Keep the original retry state and due time, so polling cannot
                # postpone delivery forever or create a second send operation.
                existing_notice.updated_at = now
                created.append(merged)
                continue
            db.add(NotificationAudit(
                event_id=event_id, event_type=str(payload.get("event_type") or ""),
                eligible=bool(notice_decision["eligible"]),
                reason_code=str(notice_decision["reasonCode"]),
                dedupe_key=semantic_key, payload={
                    "setupId": payload.get("setupId"),
                    "positionId": payload.get("positionId"),
                    "snapshotId": payload.get("snapshotId")}, created_at=now))
            previous_sent = _last_sent_market_decision(db, symbol, payload)
            meaningful, reason = is_meaningful_change(previous_sent, payload)
            if not meaningful:
                logger.info(
                    "telegram notification suppressed: %s (%s)",
                    payload.get("setupId") or event_id,
                    reason,
                )
                db.add(NotificationAudit(
                    event_id=event_id,
                    event_type=str(payload.get("event_type") or ""),
                    eligible=False,
                    reason_code="SKIP_NO_MEANINGFUL_DECISION_CHANGE",
                    dedupe_key=semantic_key,
                    payload={"meaningfulChangeReason": reason},
                    created_at=now,
                ))
                continue
            payload["meaningfulChangeReason"] = reason
            exists = db.execute(
                select(DecisionEvent.id).where(DecisionEvent.event_id == event_id)
            ).scalar_one_or_none()
            if exists is not None:
                continue
            db.add(
                DecisionEvent(
                    event_id=event_id,
                    event_type=str(payload.get("event_type") or "DECISION_UPDATED"),
                    event_version=int(payload.get("eventVersion") or 1),
                    setup_id=str(payload.get("setupId") or ""),
                    position_id=str(payload.get("positionId") or ""),
                    snapshot_id=str(payload.get("snapshotId") or ""),
                    event_time_utc=str(payload.get("eventTimeUtc") or payload.get(
                        "candleCloseTime") or ""),
                    notification_eligible=bool(notice_decision["eligible"]),
                    notification_reason=str(notice_decision["reasonCode"]),
                    notification_priority=str(notice_decision["priority"]),
                    symbol=symbol,
                    previous_state=str(payload.get("previousState") or "WAIT"),
                    current_state=str(payload.get("currentState") or "WAIT"),
                    transition_reason=str(payload.get("transitionReason") or ""),
                    market_state=str(payload.get("marketState") or ""),
                    final_decision=str(payload.get("finalDecision") or "WAIT"),
                    current_price=float(payload.get("currentPrice") or 0),
                    entry_zone=payload.get("entryZone"),
                    stop_loss=payload.get("stopLoss"),
                    targets=list(payload.get("targets") or []),
                    candle_close_time=str(payload.get("candleCloseTime") or ""),
                    calculated_at=str(payload.get("calculatedAt") or ""),
                    data_version=int(payload.get("dataVersion") or 0),
                    scenario_type=str((payload.get("decisionAssistant") or {}).get(
                        "scenarioType") or payload.get("setupType") or ""),
                    scenario_version=int((payload.get("decisionAssistant") or {}).get(
                        "scenarioVersion") or 1),
                    entry_quality_score=(payload.get("decisionAssistant") or {}).get(
                        "entryQualityScore"),
                    expected_rr=(payload.get("decisionAssistant") or {}).get(
                        "rewardRiskRatio"),
                    payload=payload,
                    created_at=now,
                )
            )
            if not notice_decision["eligible"]:
                continue
            db.add(
                TelegramNotification(
                    event_id=event_id,
                    semantic_dedup_key=semantic_key or None,
                    status="PENDING",
                    decision_id=str(payload.get("decisionId") or ""),
                    decision_version=int(payload.get("decisionVersion") or 0),
                    decision_snapshot={
                        key: payload.get(key) for key in (
                            "decisionId", "decisionVersion", "scenarioVersion",
                            "finalDecision", "currentPrice", "entryZone", "chaseLimit",
                            "stopLoss", "targets", "effectiveRR", "qualityScore")
                    },
                    attempts=0,
                    next_attempt_at=now + timedelta(
                        seconds=get_settings().alert_aggregation_window_seconds),
                    created_at=now,
                    updated_at=now,
                )
            )
            created.append(payload)
    return created


def _zh_state(value: str) -> str:
    return {
        "SHORT_INVALIDATED": "空方劇本失效",
        "FALSE_BREAKOUT": "假跌破確認",
        "BULLISH_RECOVERY": "行情轉強｜等待多方確認",
        "LONG_WATCH": "多方觀察中",
        "LONG_READY": "多方條件成立",
        "SHORT_WATCH": "空方觀察中",
        "SHORT_READY": "空方條件成立",
        "LONG_MANAGE": "多單管理更新",
        "SHORT_MANAGE": "空單管理更新",
        "MISSED_ENTRY": "原進場區已錯過",
        "INVALIDATED": "原劇本失效",
        "DATA_STALE": "行情資料異常",
        "SHORT_TERM_WEAK_HTF_BULLISH": "短線轉弱，高週期仍偏多",
        "SHORT_TERM_RECOVERING": "短線正在恢復，還差最後確認",
        "SHORT_TERM_BULLISH_RESTORED": "短線重新轉強",
        "BULLISH_RESTORED": "短線重新轉強",
        "BEARISH_CONFIRMED": "短線已正式轉空",
    }.get(value, "市場方向暫無法確認")


def format_telegram_event(event: dict) -> str:
    return format_decision_message(event)


async def deliver_pending_telegram(
    *, sender=None, editor=None, limit: int = 20, event_id: str | None = None
) -> int:
    """Claim due rows, send, and persist Telegram receipt or retry state."""
    if sender is None:
        settings = get_settings()
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            return 0
        from app.notifications.telegram import TelegramChannel

        channel = TelegramChannel(
            settings.telegram_bot_token, settings.telegram_chat_id
        )
        sender = channel.send_with_receipt
        editor = channel.edit_with_receipt
    now = datetime.now(timezone.utc)
    with db_session() as db:
        claimable = or_(
            TelegramNotification.status.in_(("PENDING", "FAILED", "EDIT_PENDING")),
            and_(TelegramNotification.status == "RETRYING",
                 TelegramNotification.updated_at <= now - timedelta(seconds=60)),
        )
        filters = [claimable]
        if not event_id:
            filters.append(TelegramNotification.next_attempt_at <= now)
        if event_id:
            filters.append(TelegramNotification.event_id == event_id)
        rows = (
            db.execute(
                select(TelegramNotification)
                .where(*filters)
                .order_by(TelegramNotification.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        claimed = []
        for row in rows:
            event = db.execute(
                select(DecisionEvent).where(DecisionEvent.event_id == row.event_id)
            ).scalar_one()
            current = db.execute(select(CurrentFinalDecision).where(
                CurrentFinalDecision.symbol == event.symbol).with_for_update()
            ).scalar_one_or_none()
            payload = dict(event.payload or {})
            event_type = str(payload.get("event_type") or event.event_type or "")
            entry_event = event_type in {"ENTRY_READY", "ENTRY_NOW"}
            if entry_event and is_expired(payload, now=now):
                row.status = "CANCELLED"
                row.cancellation_reason = "NOTIFICATION_TOO_OLD"
                row.updated_at = now
                continue
            if (entry_event and current is not None and row.decision_id
                    and (row.decision_id != current.decision_id
                         or row.decision_version < current.decision_version)):
                row.status = "CANCELLED"
                row.cancellation_reason = (
                    "STALE_DECISION_VERSION" if row.decision_version < current.decision_version
                    else "CANCELLED_SUPERSEDED")
                row.updated_at = now
                logger.warning("superseded Telegram notification blocked: %s", row.event_id)
                continue
            prior_status = row.status
            row.status = "RETRYING"
            row.attempts += 1
            row.updated_at = now
            claimed.append((row.event_id, payload, row.attempts,
                            prior_status, row.message_id, row.created_at))
    sent = 0
    for (claimed_event_id, payload, attempts, prior_status, prior_message_id,
         queued_at) in claimed:
        try:
            delivery_now = datetime.now(timezone.utc)
            blocked: tuple[str, str, str, dict, dict] | None = None
            with db_session() as db:
                current = db.execute(select(CurrentFinalDecision).where(
                    CurrentFinalDecision.symbol == str(payload.get("symbol") or "XAUUSD")
                )).scalar_one_or_none()
                is_test = str(payload.get("event_type") or "") == "TEST_NOTIFICATION"
                entry_event = str(payload.get("event_type") or "") in {
                    "ENTRY_READY", "ENTRY_NOW"}
                if (entry_event and current is not None and not is_test
                        and str(payload.get("decisionId") or "") != current.decision_id):
                    row = db.execute(select(TelegramNotification).where(
                        TelegramNotification.event_id == claimed_event_id)).scalar_one()
                    row.status = "CANCELLED"
                    row.cancellation_reason = "CANCELLED_SUPERSEDED"
                    row.updated_at = delivery_now
                    continue
                if entry_event and not is_test:
                    symbol = str(payload.get("symbol") or "XAUUSD")
                    safety = validate_pre_delivery(
                        db, symbol=symbol, queued_payload=payload,
                        queued_at=queued_at, now=delivery_now,
                    )
                    row = db.execute(select(TelegramNotification).where(
                        TelegramNotification.event_id == claimed_event_id
                    )).scalar_one()
                    row.decision_snapshot = {
                        **dict(row.decision_snapshot or {}),
                        "deliveryValidation": safety.snapshot,
                    }
                    row.updated_at = delivery_now
                    if not safety.allowed:
                        row.status = "CANCELLED"
                        row.cancellation_reason = safety.reason
                        blocked = (
                            symbol, row.decision_id, safety.reason, safety.snapshot,
                            safety.render_payload,
                        )
                    else:
                        payload = safety.render_payload
            if blocked is not None:
                symbol, decision_id, reason, snapshot, current_payload = blocked
                audit_delivery_block(symbol, decision_id, reason, snapshot)
                transition_blocked_entry(symbol, current_payload, reason, snapshot)
                logger.warning(
                    "unsafe Telegram notification blocked at delivery: %s (%s)",
                    claimed_event_id, reason,
                )
                continue
            if prior_status == "EDIT_PENDING" and prior_message_id and editor:
                message_id = await editor(prior_message_id, format_telegram_event(payload))
            else:
                message_id = await sender(format_telegram_event(payload))
            if str(message_id).upper() == "DELIVERY_UNKNOWN":
                raise DeliveryUnknownError("Telegram delivery receipt unavailable")
            if not message_id:
                raise RuntimeError("Telegram 未回傳 message_id")
            with db_session() as db:
                row = db.execute(
                    select(TelegramNotification).where(
                        TelegramNotification.event_id == claimed_event_id
                    )
                ).scalar_one()
                row.status, row.message_id, row.sent_at = (
                    "CONFIRMED", str(message_id), delivery_now)
                row.last_error, row.updated_at = "", delivery_now
                lifecycle = payload.get("setupLifecycle") or {}
                if lifecycle.get("state") == "ENTRY_READY" and payload.get("setupId"):
                    monitor = db.execute(select(MarketMonitorState).where(
                        MarketMonitorState.symbol == str(payload.get("symbol") or "XAUUSD"),
                        MarketMonitorState.monitor_key == "final_decision",
                    )).scalar_one_or_none()
                    if monitor is not None:
                        stored = dict(monitor.payload or {})
                        current_lifecycle = dict(stored.get("setup_lifecycle") or {})
                        if current_lifecycle.get("setupId") == payload.get("setupId"):
                            current_lifecycle["entryNotificationSentAt"] = now.isoformat()
                            current_lifecycle["wasEntryReady"] = True
                            stored["setup_lifecycle"] = current_lifecycle
                            monitor.payload, monitor.updated_at = stored, now
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "telegram outbox delivery failed for event %s", claimed_event_id
            )
            with db_session() as db:
                row = db.execute(
                    select(TelegramNotification).where(
                        TelegramNotification.event_id == claimed_event_id
                    )
                ).scalar_one()
                # A timeout is ambiguous: Telegram may have accepted the
                # message while its response was lost. Retrying sendMessage
                # could duplicate it, so park it for operator reconciliation.
                timeout_unknown = (isinstance(exc, DeliveryUnknownError)
                                   or "timeout" in type(exc).__name__.lower())
                row.status = "DELIVERY_UNKNOWN" if timeout_unknown else "FAILED"
                row.last_error = type(exc).__name__
                row.next_attempt_at = (None if timeout_unknown else
                                       now + timedelta(seconds=min(300, 2**attempts)))
                row.updated_at = now
    return sent


def reconcile_unknown_deliveries(*, limit: int = 100) -> dict:
    """Inspect ambiguous rows without resending them.

    Telegram Bot API has no reliable lookup-by-client-fingerprint endpoint.
    Rows with a persisted receipt can be confirmed; receipt-less rows remain
    parked until an operator or a future provider reconciliation explicitly
    resolves them. Merely running this queue never calls sendMessage.
    """
    with db_session() as db:
        rows = db.execute(select(TelegramNotification).where(
            TelegramNotification.status == "DELIVERY_UNKNOWN"
        ).order_by(TelegramNotification.updated_at).limit(limit)).scalars().all()
        confirmed = 0
        for row in rows:
            if row.message_id:
                row.status = "CONFIRMED"
                row.sent_at = row.sent_at or row.updated_at
                row.last_error = ""
                confirmed += 1
    unresolved = len(rows) - confirmed
    if unresolved:
        logger.warning("telegram reconciliation pending for %d ambiguous deliveries",
                       unresolved)
    return {"checked": len(rows), "confirmed": confirmed,
            "unresolved": unresolved}


def resolve_delivery_unknown(event_id: str, *, delivered: bool,
                             message_id: str = "") -> str:
    """Explicit reconciliation result; retry is enabled only when not delivered."""
    now = datetime.now(timezone.utc)
    with db_session() as db:
        row = db.execute(select(TelegramNotification).where(
            TelegramNotification.event_id == event_id)).scalar_one()
        if row.status != "DELIVERY_UNKNOWN":
            return row.status
        if delivered:
            row.status = "CONFIRMED"
            row.message_id = message_id or row.message_id
            row.sent_at = row.sent_at or now
            row.next_attempt_at = None
            row.last_error = ""
        else:
            row.status = "FAILED"
            row.next_attempt_at = now
            row.last_error = "RECONCILIATION_CONFIRMED_NOT_DELIVERED"
        row.updated_at = now
        return row.status


def telegram_delivery_status() -> dict:
    with db_session() as db:
        row = db.execute(
            select(TelegramNotification, DecisionEvent)
            .join(
                DecisionEvent, DecisionEvent.event_id == TelegramNotification.event_id
            )
            .order_by(TelegramNotification.id.desc())
            .limit(1)
        ).first()
        last_sent = db.execute(
            select(TelegramNotification)
            .where(TelegramNotification.status.in_(DELIVERED_STATUSES))
            .order_by(TelegramNotification.sent_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        pending_count = db.scalar(select(func.count()).select_from(
            TelegramNotification).where(TelegramNotification.status.in_((
                "PENDING", "FAILED", "RETRYING", "EDIT_PENDING")))) or 0
        failed_count = db.scalar(select(func.count()).select_from(
            TelegramNotification).where(TelegramNotification.status == "FAILED")) or 0
        unknown_count = db.scalar(select(func.count()).select_from(
            TelegramNotification).where(
                TelegramNotification.status == "DELIVERY_UNKNOWN")) or 0
        gap_count = db.scalar(select(func.count()).select_from(TelegramNotification).where(
            TelegramNotification.status.in_(("PENDING", "FAILED", "RETRYING")),
            TelegramNotification.created_at < datetime.now(timezone.utc) - timedelta(seconds=30)
        )) or 0
    configured = bool(
        get_settings().telegram_bot_token and get_settings().telegram_chat_id
    )
    if not row:
        return {
            "connected": configured,
            "status": "IDLE",
            "lastSentAt": "",
            "lastEvent": "",
            "delivered": False,
            "messageId": "",
            "pipelineStatus": "HEALTHY",
            "queueDepth": int(pending_count),
            "failedCount": int(failed_count),
            "reconciliationPendingCount": int(unknown_count),
            "deliveryGapCount": int(gap_count),
            "lastDeliveryLatencyMs": None,
            "lastError": "",
        }
    notification, event = row
    latency_ms = None
    if last_sent and last_sent.sent_at:
        latency_ms = round((last_sent.sent_at - last_sent.created_at).total_seconds() * 1000)
    return {
        "connected": configured,
        "status": notification.status,
        "lastSentAt": last_sent.sent_at.isoformat()
        if last_sent and last_sent.sent_at
        else "",
        "lastEvent": event.current_state,
        "delivered": notification.status in DELIVERED_STATUSES,
        "messageId": notification.message_id or "",
        "eventId": event.event_id,
        "pipelineStatus": ("NOTIFICATION_PIPELINE_DEGRADED" if gap_count else
                           "RECONCILIATION_PENDING" if unknown_count else "HEALTHY"),
        "queueDepth": int(pending_count), "failedCount": int(failed_count),
        "reconciliationPendingCount": int(unknown_count),
        "deliveryGapCount": int(gap_count), "lastDeliveryLatencyMs": latency_ms,
        "lastError": ("" if notification.status == "DELIVERY_UNKNOWN"
                      else notification.last_error),
    }
