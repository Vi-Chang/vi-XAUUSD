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
    log = logger.error if severity in {"P0", "P1"} else logger.info
    log("canonical classification %s for %s", kind, symbol)


def get_current_final_decision(symbol: str = "XAUUSD") -> dict:
    with db_session() as db:
        row = db.execute(select(CurrentFinalDecision).where(
            CurrentFinalDecision.symbol == symbol)).scalar_one_or_none()
        return dict(row.payload or {}) if row else {}


def get_canonical_market_bias(symbol: str = "XAUUSD") -> str:
    """Return the one durable market bias used by UI and notifications.

    Data-health code must gate execution, never derive or overwrite direction.
    """
    decision = get_current_final_decision(symbol)
    value = str(decision.get("marketBias") or decision.get("direction") or
                "NEUTRAL").upper()
    return {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(
        value, value if value in {"BULLISH", "BEARISH", "NEUTRAL"} else "NEUTRAL")


def get_canonical_state_version(symbol: str = "XAUUSD") -> int:
    return int(get_current_final_decision(symbol).get("decisionVersion") or 0)


def atomic_publish_canonical_snapshot(symbol: str,
                                      decision: dict) -> tuple[dict, bool]:
    """Atomically publish one complete canonical snapshot under a row lock."""
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
        incoming["canonicalStateVersion"] = version
        incoming["supersedesDecisionId"] = supersedes
        incoming["sourceDataVersion"] = incoming_data
        decision_id = hashlib.sha256(
            f"{symbol}|{version}|{signature}".encode()).hexdigest()[:24]
        incoming["decisionId"] = decision_id
        canonical = dict(incoming.get("canonicalDecision") or {})
        canonical.update({"decisionId": decision_id, "decisionVersion": version,
                          "canonicalStateVersion": version})
        incoming["canonicalDecision"] = canonical
        for event in incoming.get("events") or []:
            event["decisionId"], event["decisionVersion"] = decision_id, version
            event["supersedesDecisionId"] = supersedes
            event["eventId"] = hashlib.sha256(
                f"{decision_id}|{event.get('event_type')}|{event.get('setupId')}|"
                f"{event.get('currentState')}|{event.get('candleCloseTime')}"
                .encode()).hexdigest()[:32]
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
        row.direction = str(incoming.get("marketBias") or incoming.get("direction") or
                            "NEUTRAL")
        row.source_candle_close_time = incoming_candle
        row.source_data_version = incoming_data
        row.evaluated_at = incoming_eval
        row.supersedes_decision_id = supersedes
        row.payload, row.updated_at = incoming, now
        previous_type = str(previous.get("conflictType") or "NO_CONFLICT")
        incoming_type = str(incoming.get("conflictType") or "NO_CONFLICT")
        if incoming_type != previous_type and incoming_type != "NO_CONFLICT":
            _record_conflict(
                db, symbol, incoming_type, incoming,
                "P0" if incoming_type == "TRUE_ENGINE_CONFLICT" else "P3")
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
                # Every queued user-facing message is a snapshot of one canonical
                # version.  Historical facts remain in DecisionEvent, but a stale
                # snapshot must never be delivered as current advice.
                legacy_event = bool(event and not (event.payload or {}).get("eventVersion"))
                if legacy_event or event_type != "TEST_NOTIFICATION":
                    notification.status = "CANCELLED"
                    notification.cancellation_reason = "STALE_STATE_VERSION"
                    notification.updated_at = now
        return incoming, True


def publish_current_final_decision(symbol: str, decision: dict) -> tuple[dict, bool]:
    """Compatibility alias for the atomic canonical publisher."""
    return atomic_publish_canonical_snapshot(symbol, decision)


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
        "conflict_classification": {
            key: counts.get(key, 0) for key in (
                "TIMEFRAME_DIVERGENCE", "BIAS_TRANSITION",
                "DATA_VERSION_MISMATCH", "STALE_ENGINE_RESULT",
                "DATA_DEGRADED_CONDITION", "SCORE_NEAR_TIE",
                "CANONICAL_INVARIANT_VIOLATION", "TRUE_ENGINE_CONFLICT")
        },
    }
