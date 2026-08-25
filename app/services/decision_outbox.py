"""Durable DecisionEvent + Telegram transactional outbox."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

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
    notification_state_regression,
)
from app.services.notification_coordinator import coordinate_notification_intents
from app.services.notification_policy import (
    canonical_dedupe_key,
    eligibility,
    has_meaningful_action_delta,
    is_expired,
    user_visible_state_fingerprint,
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


def _canonicalize_payload(payload: dict, current: CurrentFinalDecision | None) -> dict:
    """Bind every notification to the durable canonical decision snapshot."""
    hydrated = dict(payload)
    if current is None:
        return hydrated
    canonical = dict(current.payload or {})
    bias = str(canonical.get("marketBias") or canonical.get("direction") or
               current.direction or "NEUTRAL").upper()
    bias = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(
        bias, bias if bias in {"BULLISH", "BEARISH", "NEUTRAL"} else "NEUTRAL")
    hydrated["marketBias"] = bias
    hydrated["canonicalStateVersion"] = current.decision_version
    hydrated["decisionVersion"] = current.decision_version
    hydrated["decisionId"] = current.decision_id
    snapshot = dict(hydrated.get("canonicalDecision") or {})
    snapshot.update({
        "marketBias": bias,
        "decisionVersion": current.decision_version,
        "decisionId": current.decision_id,
    })
    hydrated["canonicalDecision"] = snapshot
    return hydrated


def _notification_event_key(payload: dict, semantic_key: str) -> str:
    explicit = str(payload.get("dataHealthEventKey") or payload.get("eventKey") or "")
    event_type = str(payload.get("event_type") or "DECISION_UPDATED")
    symbol = str(payload.get("symbol") or "XAUUSD")
    incident = str(payload.get("dataIncidentId") or payload.get("incidentId") or "")
    identity = explicit or semantic_key or str(payload.get("eventId") or "")
    return hashlib.sha256(
        f"{symbol}|{event_type}|{incident}|{identity}".encode()
    ).hexdigest()


def _notification_payload_hash(payload: dict) -> str:
    """Secondary semantic guard for separately-created but identical messages."""
    rendered = format_decision_message(payload)
    normalized = "\n".join(line.strip() for line in rendered.splitlines() if line.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _audit_log(*, payload: dict, event_key: str, payload_hash: str,
               result: str, worker_id: str = "") -> None:
    logger.info("telegram_notification_audit %s", json.dumps({
        "event_key": event_key,
        "event_type": payload.get("event_type"),
        "state_version": payload.get("canonicalStateVersion") or
                         payload.get("decisionVersion"),
        "canonical_version": payload.get("decisionVersion"),
        "bias": payload.get("marketBias"),
        "health": payload.get("dataHealth"),
        "payload_hash": payload_hash,
        "dedupe_result": result,
        "worker_id": worker_id,
    }, ensure_ascii=False, sort_keys=True))


def _last_sent_market_decision(db, symbol: str, payload: dict) -> dict | None:
    """Find the latest delivered decision in the same direction/market stream."""
    direction = str(payload.get("direction") or "NONE")
    scenario_id = str(payload.get("scenarioId") or payload.get("setupId") or "")
    rows = db.execute(
        select(DecisionEvent)
        .join(TelegramNotification,
              TelegramNotification.event_id == DecisionEvent.event_id)
        .where(DecisionEvent.symbol == symbol,
               TelegramNotification.status.in_(DELIVERED_STATUSES))
        .order_by(TelegramNotification.sent_at.desc())
        .limit(50)
    ).scalars().all()
    fallback = None
    for row in rows:
        old = dict(row.payload or {})
        old_scenario = str(old.get("scenarioId") or old.get("setupId") or "")
        if scenario_id and old_scenario == scenario_id:
            return old
        if str(old.get("direction") or "NONE") == direction:
            fallback = fallback or old
    return fallback


def persist_decision_events(symbol: str, events: list[dict]) -> list[dict]:
    """Persist facts but enqueue only one semantic notification per group."""
    created: list[dict] = []
    now = datetime.now(timezone.utc)
    events = coordinate_notification_intents(symbol, events)
    valid_events = []
    for payload in events:
        payload["symbol"] = symbol
        decision = ({"eligible": False, "reasonCode": "LOG_ONLY_INTENT",
                     "priority": "DEBUG"}
                    if payload.get("notificationRoute") == "LOG_ONLY" else
                    eligibility(payload) if payload.get("eventVersion") else {
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
            if not event_id:
                continue
            current = db.execute(select(CurrentFinalDecision).where(
                CurrentFinalDecision.symbol == symbol)).scalar_one_or_none()
            incoming_decision_id = str(payload.get("decisionId") or "")
            incoming_state_version = int(payload.get("canonicalStateVersion") or
                                         payload.get("decisionVersion") or 0)
            is_test = str(payload.get("event_type") or "") == "TEST_NOTIFICATION"
            if (current is not None and not is_test
                    and (incoming_decision_id != current.decision_id
                         or incoming_state_version != current.decision_version)):
                logger.warning("stale state event rejected before enqueue: %s", event_id)
                db.add(NotificationAudit(
                    event_id=event_id,
                    event_type=str(payload.get("event_type") or ""),
                    eligible=False, reason_code="SKIP_STALE_STATE_VERSION",
                    dedupe_key=str(payload.get("semanticDedupKey") or ""),
                    payload={"decisionId": incoming_decision_id,
                             "stateVersion": incoming_state_version,
                             "currentDecisionId": current.decision_id,
                             "currentStateVersion": current.decision_version},
                    created_at=now))
                continue
            payload = _canonicalize_payload(payload, current)
            if payload.get("eventVersion"):
                raw_key = canonical_dedupe_key(payload)
                payload["semanticDedupKey"] = hashlib.sha256(raw_key.encode()).hexdigest()
            semantic_key = str(payload.get("semanticDedupKey") or "")
            event_key = _notification_event_key(payload, semantic_key)
            payload_hash = _notification_payload_hash(payload)
            payload["notificationEventKey"] = event_key
            payload["notificationPayloadHash"] = payload_hash
            payload_decision_id = str(payload.get("decisionId") or "")
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
            existing_notice = db.execute(select(TelegramNotification).where(or_(
                TelegramNotification.event_key == event_key,
                TelegramNotification.semantic_dedup_key == semantic_key,
            ))).scalar_one_or_none() if semantic_key else db.execute(
                select(TelegramNotification).where(
                    TelegramNotification.event_key == event_key)
            ).scalar_one_or_none()
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
                    _audit_log(payload=payload, event_key=event_key,
                               payload_hash=payload_hash, result="DUPLICATE_EVENT_KEY")
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
            cooldown_start = now - timedelta(
                seconds=get_settings().telegram_payload_dedup_cooldown_seconds)
            duplicate_payload = db.execute(select(TelegramNotification).where(
                TelegramNotification.symbol == symbol,
                TelegramNotification.event_type == str(
                    payload.get("event_type") or "DECISION_UPDATED"),
                TelegramNotification.payload_hash == payload_hash,
                TelegramNotification.status.in_((*DELIVERED_STATUSES,
                                                  "DELIVERY_UNKNOWN", "PENDING",
                                                  "FAILED", "RETRYING", "EDIT_PENDING")),
                TelegramNotification.created_at >= cooldown_start,
            ).order_by(TelegramNotification.id.desc()).limit(1)).scalar_one_or_none()
            if duplicate_payload is not None:
                db.add(NotificationAudit(
                    event_id=event_id,
                    event_type=str(payload.get("event_type") or ""),
                    eligible=False, reason_code="SKIP_DUPLICATE_PAYLOAD",
                    dedupe_key=event_key,
                    payload={"originalEventId": duplicate_payload.event_id,
                             "payloadHash": payload_hash}, created_at=now))
                _audit_log(payload=payload, event_key=event_key,
                           payload_hash=payload_hash, result="DUPLICATE_PAYLOAD")
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
            action_delta, action_delta_reason = has_meaningful_action_delta(
                previous_sent, payload)
            if notice_decision["eligible"] and not is_test and not action_delta:
                before_fingerprint = (user_visible_state_fingerprint(previous_sent)
                                      if previous_sent else "")
                after_fingerprint = user_visible_state_fingerprint(payload)
                logger.info(
                    "telegram action suppressed: snapshot=%s intent=%s reason=%s before=%s after=%s",
                    payload.get("snapshotId"), payload.get("event_type"),
                    action_delta_reason, before_fingerprint, after_fingerprint,
                )
                db.add(NotificationAudit(
                    event_id=event_id,
                    event_type=str(payload.get("event_type") or ""),
                    eligible=False, reason_code=action_delta_reason,
                    dedupe_key=semantic_key,
                    payload={"snapshotId": payload.get("snapshotId"),
                             "priority": notice_decision.get("userPriority"),
                             "notificationIntent": payload.get("event_type"),
                             "fingerprintBefore": before_fingerprint,
                             "fingerprintAfter": after_fingerprint},
                    created_at=now,
                ))
                notice_decision = {**notice_decision, "eligible": False,
                                   "reasonCode": action_delta_reason}
                payload["notificationDecision"] = notice_decision
                payload["notificationEligible"] = False
            regressed, regression_reason = notification_state_regression(
                previous_sent, payload)
            if regressed:
                logger.warning(
                    "STATE_REGRESSION_BLOCKED scenario=%s event=%s",
                    payload.get("setupId") or payload.get("scenarioId"), event_id,
                )
                db.add(NotificationAudit(
                    event_id=event_id,
                    event_type=str(payload.get("event_type") or ""),
                    eligible=False,
                    reason_code="STATE_REGRESSION_BLOCKED",
                    dedupe_key=semantic_key,
                    payload={"previousState": (previous_sent or {}).get("currentState"),
                             "currentState": payload.get("currentState"),
                             "reason": regression_reason},
                    created_at=now,
                ))
                continue
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
            try:
                with db.begin_nested():
                    db.add(DecisionEvent(
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
                    ))
                    db.flush()
            except IntegrityError:
                # Another scheduler/worker persisted this exact immutable fact.
                # Its outbox row is authoritative; this worker does no more work.
                db.add(NotificationAudit(
                    event_id=event_id,
                    event_type=str(payload.get("event_type") or ""),
                    eligible=False, reason_code="SKIP_DUPLICATE_DECISION_EVENT",
                    dedupe_key=event_key,
                    payload={"eventKey": event_key}, created_at=now))
                _audit_log(payload=payload, event_key=event_key,
                           payload_hash=payload_hash,
                           result="DUPLICATE_DECISION_EVENT")
                continue
            # Persist the immutable fact first.  The outbox insert below owns a
            # second savepoint so an event-key collision suppresses only the
            # duplicate delivery, not the market audit fact.
            if not notice_decision["eligible"]:
                continue
            try:
                with db.begin_nested():
                    db.add(TelegramNotification(
                    event_id=event_id,
                    semantic_dedup_key=semantic_key or None,
                    event_key=event_key,
                    event_type=str(payload.get("event_type") or "DECISION_UPDATED"),
                    symbol=symbol,
                    state_version=int(payload.get("canonicalStateVersion") or
                                      payload.get("decisionVersion") or 0),
                    incident_id=str(payload.get("dataIncidentId") or
                                    payload.get("incidentId") or ""),
                    payload_hash=payload_hash,
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
                    ))
                    db.flush()
            except IntegrityError:
                db.add(NotificationAudit(
                    event_id=event_id,
                    event_type=str(payload.get("event_type") or ""),
                    eligible=False, reason_code="SKIP_ATOMIC_DUPLICATE",
                    dedupe_key=event_key,
                    payload={"eventKey": event_key, "payloadHash": payload_hash},
                    created_at=now))
                _audit_log(payload=payload, event_key=event_key,
                           payload_hash=payload_hash,
                           result="ATOMIC_INSERT_CONFLICT")
                continue
            _audit_log(payload=payload, event_key=event_key,
                       payload_hash=payload_hash, result="ENQUEUED")
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
    worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
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
            is_test = event_type == "TEST_NOTIFICATION"
            entry_event = event_type in {"ENTRY_READY", "ENTRY_NOW"}
            if entry_event and is_expired(payload, now=now):
                row.status = "CANCELLED"
                row.cancellation_reason = "NOTIFICATION_TOO_OLD"
                row.updated_at = now
                continue
            if (not is_test and current is not None
                    and row.state_version != current.decision_version):
                row.status = "CANCELLED"
                row.cancellation_reason = "STALE_STATE_VERSION"
                row.updated_at = now
                _audit_log(payload=payload, event_key=str(row.event_key or ""),
                           payload_hash=str(row.payload_hash or ""),
                           result="STALE_STATE_VERSION", worker_id=worker_id)
                continue
            prior_status = row.status
            # Atomic compare-and-swap claim.  FOR UPDATE/SKIP LOCKED remains an
            # optimisation on PostgreSQL; this conditional UPDATE is the actual
            # ownership boundary and also works on SQLite/tests.
            result = db.execute(update(TelegramNotification).where(
                TelegramNotification.id == row.id,
                TelegramNotification.status == prior_status,
                TelegramNotification.updated_at == row.updated_at,
            ).values(
                status="RETRYING",
                attempts=TelegramNotification.attempts + 1,
                updated_at=now,
                sender_worker_id=worker_id,
            ).execution_options(synchronize_session=False))
            if getattr(result, "rowcount", 0) != 1:
                _audit_log(payload=payload, event_key=str(row.event_key or ""),
                           payload_hash=str(row.payload_hash or ""),
                           result="ATOMIC_CLAIM_LOST", worker_id=worker_id)
                continue
            payload = _canonicalize_payload(payload, current)
            claimed.append((row.event_id, payload, row.attempts + 1,
                            prior_status, row.message_id, row.created_at,
                            str(row.event_key or ""), str(row.payload_hash or "")))
            _audit_log(payload=payload, event_key=str(row.event_key or ""),
                       payload_hash=str(row.payload_hash or ""),
                       result="ATOMIC_CLAIM_WON", worker_id=worker_id)
    sent = 0
    for (claimed_event_id, payload, attempts, prior_status, prior_message_id,
         queued_at, event_key, payload_hash) in claimed:
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
                row = db.execute(select(TelegramNotification).where(
                    TelegramNotification.event_id == claimed_event_id)).scalar_one()
                if (current is not None and not is_test
                        and row.state_version != current.decision_version):
                    row.status = "CANCELLED"
                    row.cancellation_reason = "STALE_STATE_VERSION"
                    row.updated_at = delivery_now
                    _audit_log(payload=payload, event_key=event_key,
                               payload_hash=payload_hash,
                               result="STALE_BEFORE_SEND", worker_id=worker_id)
                    continue
                payload = _canonicalize_payload(payload, current)
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
            _audit_log(payload=payload, event_key=event_key,
                       payload_hash=payload_hash, result="CONFIRMED",
                       worker_id=worker_id)
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
