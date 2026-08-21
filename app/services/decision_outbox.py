"""Durable DecisionEvent + Telegram transactional outbox."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from app.config import get_settings
from app.db.models import DecisionEvent, TelegramNotification
from app.db.session import db_session
from app.engines.decision_presentation import format_decision_message
from app.engines.trigger_lifecycle import validate_notification
from app.services.alert_aggregator import aggregate_signal_facts

logger = logging.getLogger(__name__)


def persist_decision_events(symbol: str, events: list[dict]) -> list[dict]:
    """Persist facts but enqueue only one semantic notification per group."""
    created: list[dict] = []
    now = datetime.now(timezone.utc)
    events = aggregate_signal_facts(symbol, events)
    valid_events = []
    for payload in events:
        errors = validate_notification(payload)
        if errors:
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
            existing_notice = db.execute(select(TelegramNotification).where(
                TelegramNotification.semantic_dedup_key == semantic_key
            )).scalar_one_or_none() if semantic_key else None
            if existing_notice is not None:
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
                if existing_notice.status == "SENT" and existing_notice.message_id:
                    existing_notice.status = "EDIT_PENDING"
                elif existing_notice.status != "RETRYING":
                    existing_notice.status = "PENDING"
                existing_notice.next_attempt_at = now + timedelta(
                    seconds=get_settings().alert_aggregation_window_seconds)
                existing_notice.updated_at = now
                created.append(merged)
                continue
            exists = db.execute(
                select(DecisionEvent.id).where(DecisionEvent.event_id == event_id)
            ).scalar_one_or_none()
            if exists is not None:
                continue
            db.add(
                DecisionEvent(
                    event_id=event_id,
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
                    payload=payload,
                    created_at=now,
                )
            )
            db.add(
                TelegramNotification(
                    event_id=event_id,
                    semantic_dedup_key=semantic_key or None,
                    status="PENDING",
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
    }.get(value, "市場決策更新")


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
            prior_status = row.status
            row.status = "RETRYING"
            row.attempts += 1
            row.updated_at = now
            event = db.execute(
                select(DecisionEvent).where(DecisionEvent.event_id == row.event_id)
            ).scalar_one()
            claimed.append((row.event_id, dict(event.payload), row.attempts,
                            prior_status, row.message_id))
    sent = 0
    for claimed_event_id, payload, attempts, prior_status, prior_message_id in claimed:
        try:
            if prior_status == "EDIT_PENDING" and prior_message_id and editor:
                message_id = await editor(prior_message_id, format_telegram_event(payload))
            else:
                message_id = await sender(format_telegram_event(payload))
            if not message_id:
                raise RuntimeError("Telegram 未回傳 message_id")
            with db_session() as db:
                row = db.execute(
                    select(TelegramNotification).where(
                        TelegramNotification.event_id == claimed_event_id
                    )
                ).scalar_one()
                row.status, row.message_id, row.sent_at = "SENT", str(message_id), now
                row.last_error, row.updated_at = "", now
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
                row.status = "FAILED"
                row.last_error = type(exc).__name__
                row.next_attempt_at = now + timedelta(seconds=min(300, 2**attempts))
                row.updated_at = now
    return sent


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
            .where(TelegramNotification.status == "SENT")
            .order_by(TelegramNotification.sent_at.desc())
            .limit(1)
        ).scalar_one_or_none()
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
        }
    notification, event = row
    return {
        "connected": configured,
        "status": notification.status,
        "lastSentAt": last_sent.sent_at.isoformat()
        if last_sent and last_sent.sent_at
        else "",
        "lastEvent": event.current_state,
        "delivered": notification.status == "SENT",
        "messageId": notification.message_id or "",
        "eventId": event.event_id,
    }
