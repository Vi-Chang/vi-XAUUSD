"""Transactional, monotonic storage for the one current FinalDecision."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import (
    CurrentFinalDecision,
    DecisionConflictAudit,
    DecisionEvent,
    MarketMonitorState,
    TelegramNotification,
)
from app.db.session import db_session

logger = logging.getLogger(__name__)
PENDING_NOTIFICATION_STATES = ("PENDING", "FAILED", "RETRYING", "EDIT_PENDING")


def _time_key(value: str | None) -> str:
    return str(value or "")


def _record_conflict(db, symbol: str, kind: str, decision: dict,
                     severity: str = "P1") -> None:
    db.add(DecisionConflictAudit(
        symbol=symbol, conflict_type=kind, severity=severity,
        decision_id=str(decision.get("decisionId") or ""), payload=decision,
        created_at=datetime.now(timezone.utc)))
    logger.error("decision conflict %s for %s", kind, symbol)


def get_current_final_decision(symbol: str = "XAUUSD") -> dict:
    with db_session() as db:
        row = db.execute(select(CurrentFinalDecision).where(
            CurrentFinalDecision.symbol == symbol)).scalar_one_or_none()
        return dict(row.payload or {}) if row else {}


def publish_current_final_decision(symbol: str, decision: dict) -> tuple[dict, bool]:
    """CAS-like publish; older candle/data/worker results can never roll state back."""
    now = datetime.now(timezone.utc)
    incoming = dict(decision)
    with db_session() as db:
        row = db.execute(select(CurrentFinalDecision).where(
            CurrentFinalDecision.symbol == symbol).with_for_update()).scalar_one_or_none()
        previous = dict(row.payload or {}) if row else {}
        incoming_candle = _time_key(incoming.get("sourceCandleCloseTime"))
        current_candle = _time_key(previous.get("sourceCandleCloseTime"))
        incoming_data = int(incoming.get("sourceDataVersion") or incoming.get("version") or 0)
        current_data = int(previous.get("sourceDataVersion") or previous.get("version") or 0)
        incoming_eval = _time_key(incoming.get("evaluatedAt"))
        current_eval = _time_key(previous.get("evaluatedAt"))
        if row and ((incoming_candle and current_candle and incoming_candle < current_candle)
                    or (incoming_candle == current_candle and incoming_data < current_data)
                    or (incoming_candle == current_candle and incoming_data == current_data
                        and incoming_eval and current_eval and incoming_eval < current_eval)):
            _record_conflict(db, symbol, "OUT_OF_ORDER_DECISION", incoming)
            return previous, False

        signature = str(incoming.get("decisionSignature") or "")
        changed = not row or signature != row.decision_signature
        version = (row.decision_version + 1 if row and changed
                   else row.decision_version if row else max(1, int(
                       incoming.get("decisionVersion") or 1)))
        supersedes = row.decision_id if row and changed else ""
        incoming["decisionVersion"] = version
        incoming["supersedesDecisionId"] = supersedes
        incoming["sourceDataVersion"] = incoming_data
        decision_id = hashlib.sha256(
            f"{symbol}|{version}|{signature}".encode()).hexdigest()[:24]
        incoming["decisionId"] = decision_id
        for event in incoming.get("events") or []:
            event["decisionId"], event["decisionVersion"] = decision_id, version
            event["supersedesDecisionId"] = supersedes
            event["eventId"] = hashlib.sha256(
                f"{decision_id}|{event.get('finalDecision')}|"
                f"{event.get('candleCloseTime')}".encode()).hexdigest()[:32]
        if row is None:
            row = CurrentFinalDecision(
                symbol=symbol, decision_id=decision_id, decision_version=version,
                decision_signature=signature, action="WAIT", updated_at=now)
            db.add(row)
        row.decision_id, row.decision_version = decision_id, version
        row.decision_signature = signature
        row.scenario_id = str(incoming.get("selectedScenarioId") or "")
        row.scenario_version = int(incoming.get("selectedScenarioVersion") or 1)
        row.lineage_id = str(incoming.get("selectedLineageId") or "")
        row.action = str(incoming.get("finalAction") or "WAIT")
        row.direction = str(incoming.get("direction") or "NEUTRAL")
        row.source_candle_close_time = incoming_candle
        row.source_data_version = incoming_data
        row.evaluated_at = incoming_eval
        row.supersedes_decision_id = supersedes
        row.payload, row.updated_at = incoming, now
        monitor = db.execute(select(MarketMonitorState).where(
            MarketMonitorState.symbol == symbol,
            MarketMonitorState.monitor_key == "final_decision").with_for_update()
        ).scalar_one_or_none()
        if monitor is None:
            monitor = MarketMonitorState(symbol=symbol, monitor_key="final_decision",
                                         updated_at=now)
            db.add(monitor)
        monitor.payload, monitor.updated_at = incoming, now
        if changed:
            symbol_event_ids = select(DecisionEvent.event_id).where(
                DecisionEvent.symbol == symbol)
            stale = db.execute(select(TelegramNotification).where(
                TelegramNotification.event_id.in_(symbol_event_ids),
                TelegramNotification.status.in_(PENDING_NOTIFICATION_STATES),
                TelegramNotification.decision_id != "",
                TelegramNotification.decision_id != decision_id,
            ).with_for_update()).scalars().all()
            for notification in stale:
                event = db.execute(select(DecisionEvent).where(
                    DecisionEvent.event_id == notification.event_id)).scalar_one_or_none()
                event_type = str((event.payload or {}).get("event_type") if event else "")
                # A newer dashboard decision invalidates old entry advice only.
                # DATA/position/setup lifecycle events are historical facts and
                # must still be delivered exactly once.
                legacy_event = bool(event and not (event.payload or {}).get("eventVersion"))
                if legacy_event or event_type in {"ENTRY_READY", "ENTRY_NOW"}:
                    notification.status = "CANCELLED"
                    notification.cancellation_reason = "CANCELLED_SUPERSEDED"
                    notification.updated_at = now
        return incoming, True


def conflict_metrics() -> dict:
    with db_session() as db:
        rows = db.execute(select(DecisionConflictAudit)).scalars().all()
        cancelled = db.execute(select(TelegramNotification).where(
            TelegramNotification.status == "CANCELLED")).scalars().all()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.conflict_type] = counts.get(row.conflict_type, 0) + 1
    return {
        "decision_conflict_count": len(rows),
        "stale_notification_blocked_count": sum(
            r.cancellation_reason == "STALE_DECISION_VERSION" for r in cancelled),
        "superseded_notification_count": sum(
            r.cancellation_reason == "CANCELLED_SUPERSEDED" for r in cancelled),
        "out_of_order_decision_count": counts.get("OUT_OF_ORDER_DECISION", 0),
        "duplicate_decision_count": counts.get("DUPLICATE_DECISION", 0),
        "multi_current_decision_count": counts.get("MULTI_CURRENT_DECISION", 0),
        "stale_entry_blocked_count": sum(counts.get(key, 0) for key in (
            "STALE_DECISION", "ENTRY_READY_EXPIRED", "NOTIFICATION_TOO_OLD")),
        "entry_out_of_range_blocked_count": counts.get(
            "ENTRY_PRICE_OUT_OF_RANGE", 0),
        "expired_entry_blocked_count": counts.get("ENTRY_READY_EXPIRED", 0),
        "stale_candle_blocked_count": sum(counts.get(key, 0) for key in (
            "NEW_CLOSED_CANDLE_REQUIRES_REEVALUATION", "CANDLE_DATA_MISSING",
            "LATEST_CLOSED_CANDLE_STALE")),
        "stale_decision_blocked_count": counts.get("STALE_DECISION", 0),
        "rr_revalidation_failed_count": counts.get("RR_REVALIDATION_FAILED", 0),
        "notification_queue_expired_count": counts.get("NOTIFICATION_TOO_OLD", 0),
        "price_drift_revalidation_count": counts.get(
            "PRICE_DRIFT_REQUIRES_REEVALUATION", 0),
    }
