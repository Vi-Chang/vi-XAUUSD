"""Last-moment safety validation for real-time actionable trade statements."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Candle, CurrentFinalDecision, DecisionConflictAudit, LivePrice
from app.db.session import db_session


@dataclass(frozen=True)
class DeliverySafetyResult:
    allowed: bool
    reason: str
    snapshot: dict
    render_payload: dict


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _queue_ttl(event_type: str, action: str) -> int:
    settings = get_settings()
    if action in {"ENTER_LONG", "ENTER_SHORT"} or "ENTRY_READY" in event_type:
        return settings.entry_ready_max_queue_age_seconds
    if "APPROACH" in event_type:
        return settings.entry_approaching_max_queue_age_seconds
    return settings.update_notification_max_queue_age_seconds


def _blocked(reason: str, snapshot: dict, payload: dict) -> DeliverySafetyResult:
    return DeliverySafetyResult(False, reason, snapshot, payload)


def validate_pre_delivery(db: Session, *, symbol: str, queued_payload: dict,
                          queued_at: datetime, now: datetime | None = None) -> DeliverySafetyResult:
    """Prove that an ENTRY is still executable using delivery-time market data."""
    now = _utc(now or datetime.now(timezone.utc))
    event_type = str(queued_payload.get("event_type") or "")
    queued_action = str(queued_payload.get("finalDecision") or "")
    # Only the canonical FinalDecision may authorize an actionable entry.
    # A legacy event name containing ENTRY_READY is presentation metadata,
    # not permission to trade and therefore cannot opt itself into the gate.
    actionable_entry = queued_action in {"ENTER_LONG", "ENTER_SHORT"}
    current_row = db.execute(select(CurrentFinalDecision).where(
        CurrentFinalDecision.symbol == symbol)).scalar_one_or_none()
    if current_row is None:
        if not actionable_entry:
            return DeliverySafetyResult(
                True, "NON_ENTRY_LEGACY", {"validated_at": now.isoformat()},
                queued_payload,
            )
        return _blocked("CURRENT_DECISION_MISSING", {}, queued_payload)
    current = dict(current_row.payload or {})
    action = str(current.get("finalAction") or "WAIT")
    decision_created = _parse(current.get("decisionCreatedAt") or current.get("evaluatedAt"))
    decision_age = (now - decision_created).total_seconds() if decision_created else None
    queue_age = (now - _utc(queued_at)).total_seconds()
    tick = db.execute(select(LivePrice).where(
        LivePrice.symbol == symbol).order_by(LivePrice.quote_time.desc()).limit(1)
    ).scalar_one_or_none()
    candle = db.execute(select(Candle).where(
        Candle.symbol == symbol, Candle.timeframe == "15M", Candle.is_closed.is_(True)
    ).order_by(Candle.close_time.desc()).limit(1)).scalar_one_or_none()
    bid = float(tick.bid) if tick else None
    ask = float(tick.ask) if tick else None
    delivery_price = ask if action == "ENTER_LONG" else bid if action == "ENTER_SHORT" else (
        float(tick.mid) if tick else None)
    decision_price = _number(current.get("currentPrice"))
    atr = _number(current.get("atr15")) or 0.0
    price_drift = (abs(delivery_price - decision_price)
                   if delivery_price is not None and decision_price is not None else None)
    source_candle = _parse(current.get("sourceCandleCloseTime"))
    latest_candle = _utc(candle.close_time) if candle else None
    candle_age = ((now - source_candle).total_seconds() if source_candle else None)
    snapshot = {
        "decision_price": decision_price, "delivery_price": delivery_price,
        "delivery_bid": bid, "delivery_ask": ask, "price_drift": price_drift,
        "decision_age_seconds": decision_age, "queue_age_seconds": queue_age,
        "candle_age_seconds": candle_age,
        "latest_tick_timestamp": _utc(tick.quote_time).isoformat() if tick else "",
        "latest_closed_candle_time": latest_candle.isoformat() if latest_candle else "",
        "validated_at": now.isoformat(),
    }
    queued_decision_id = str(queued_payload.get("decisionId") or "")
    queued_version = int(queued_payload.get("decisionVersion") or 0)
    if (actionable_entry and (
            queued_decision_id != current_row.decision_id
            or queued_version != current_row.decision_version)):
        return _blocked("SUPERSEDED_DECISION", snapshot, current)
    if not actionable_entry or action not in {"ENTER_LONG", "ENTER_SHORT"}:
        # Non-entry notifications still render from the latest current decision.
        render = {**queued_payload, "finalDecision": action,
                  "currentPrice": delivery_price or decision_price,
                  "humanSummary": current.get("humanSummary")}
        return DeliverySafetyResult(True, "NON_ENTRY_CURRENT", snapshot, render)
    settings = get_settings()
    if tick is None or (now - _utc(tick.quote_time)).total_seconds() > settings.delivery_tick_max_age_seconds:
        return _blocked("LATEST_TICK_STALE", snapshot, current)
    if latest_candle is None or source_candle is None:
        return _blocked("CANDLE_DATA_MISSING", snapshot, current)
    if latest_candle > source_candle:
        return _blocked("NEW_CLOSED_CANDLE_REQUIRES_REEVALUATION", snapshot, current)
    if (now - latest_candle).total_seconds() > settings.delivery_closed_candle_max_age_seconds:
        return _blocked("LATEST_CLOSED_CANDLE_STALE", snapshot, current)
    if (int(queued_payload.get("scenarioVersion") or 0)
            != int(current.get("selectedScenarioVersion") or 0)):
        return _blocked("SCENARIO_SUPERSEDED", snapshot, current)
    zone = current.get("entryZone") or {}
    low, high = _number(zone.get("low")), _number(zone.get("high"))
    chase = _number(current.get("chaseLimit"))
    stop = _number(current.get("invalidationPrice"))
    targets = [_number(value) for value in current.get("targets") or []]
    target = next((value for value in targets if value is not None), None)
    if None in {delivery_price, low, high, chase, stop, target}:
        return _blocked("ENTRY_PLAN_INCOMPLETE", snapshot, current)
    assert delivery_price is not None and low is not None and high is not None
    assert chase is not None and stop is not None and target is not None
    if action == "ENTER_LONG" and high > chase:
        return _blocked("INVALID_ENTRY_PLAN", snapshot, current)
    if action == "ENTER_SHORT" and low < chase:
        return _blocked("INVALID_ENTRY_PLAN", snapshot, current)
    if not low <= delivery_price <= high:
        return _blocked("ENTRY_PRICE_OUT_OF_RANGE", snapshot, current)
    if ((action == "ENTER_LONG" and delivery_price > chase)
            or (action == "ENTER_SHORT" and delivery_price < chase)):
        return _blocked("CHASE_LIMIT_EXCEEDED", snapshot, current)
    if ((action == "ENTER_LONG" and delivery_price <= stop)
            or (action == "ENTER_SHORT" and delivery_price >= stop)):
        return _blocked("INVALIDATION_ALREADY_TRIGGERED", snapshot, current)
    if ((action == "ENTER_LONG" and delivery_price >= target)
            or (action == "ENTER_SHORT" and delivery_price <= target)):
        return _blocked("TARGET_ALREADY_REACHED", snapshot, current)
    # Classify a concrete market invalidation before the age fallback. This
    # preserves the actionable root cause for delayed messages (for example,
    # the price has already left the approved entry zone after 14 minutes).
    if queue_age > _queue_ttl(event_type, action):
        return _blocked("NOTIFICATION_TOO_OLD", snapshot, current)
    if decision_age is None or decision_age > settings.entry_ready_max_decision_age_seconds:
        return _blocked("STALE_DECISION", snapshot, current)
    valid_until = _parse(current.get("entryReadyValidUntil") or current.get("validUntil"))
    if valid_until is None or now > valid_until:
        return _blocked("ENTRY_READY_EXPIRED", snapshot, current)
    max_drift = max(atr * settings.delivery_price_drift_atr_ratio,
                    settings.delivery_price_drift_min_delta)
    if price_drift is None or price_drift > max_drift:
        return _blocked("PRICE_DRIFT_REQUIRES_REEVALUATION", snapshot, current)
    if tick.spread > max(settings.gate_spread_max_abs,
                         settings.gate_spread_max_atr15_mult * atr):
        return _blocked("SPREAD_TOO_HIGH", snapshot, current)
    from app.engines.scenario_safety import calculate_risk_reward
    rr_details = calculate_risk_reward(
        "LONG" if action == "ENTER_LONG" else "SHORT",
        evaluation_entry_price=delivery_price, stop_loss=stop, target_price=target,
        spread=tick.spread, slippage=settings.estimated_slippage_abs)
    effective_rr = float(rr_details["ratio"] or 0.0)
    snapshot["effective_entry"] = delivery_price
    snapshot["effective_stop"] = stop
    snapshot["effective_target"] = target
    snapshot["effective_rr"] = round(effective_rr, 3)
    if effective_rr < settings.decision_assistant_min_rr:
        return _blocked("RR_REVALIDATION_FAILED", snapshot, current)
    if str(current.get("riskGate") or "") != "ENTRY_READY":
        return _blocked("RISK_GATE_NOT_READY", snapshot, current)
    render = {**queued_payload, "finalDecision": action,
              "currentState": current.get("state"), "currentPrice": delivery_price,
              "entryZone": current.get("entryZone"), "chaseLimit": chase,
              "stopLoss": stop, "targets": current.get("targets") or [],
              "effectiveRR": round(effective_rr, 2),
              "qualityScore": current.get("qualityScore"),
              "qualityGrade": current.get("qualityGrade"),
              "humanSummary": current.get("humanSummary")}
    return DeliverySafetyResult(True, "PASS", snapshot, render)


def audit_delivery_block(symbol: str, decision_id: str, reason: str,
                         snapshot: dict) -> None:
    with db_session() as db:
        db.add(DecisionConflictAudit(
            symbol=symbol, conflict_type=reason,
            severity="P0" if reason in {"ENTRY_PRICE_OUT_OF_RANGE", "STALE_DECISION",
                "SUPERSEDED_DECISION", "NOTIFICATION_TOO_OLD"} else "P1",
            decision_id=decision_id, payload=snapshot,
            created_at=datetime.now(timezone.utc)))


def transition_blocked_entry(symbol: str, current: dict, reason: str,
                             snapshot: dict) -> dict:
    """Remove actionable permission immediately; a later engine cycle may create a new plan."""
    from app.services.current_decision_store import publish_current_final_decision

    safe = blocked_entry_payload(current, reason, snapshot)
    canonical, _ = publish_current_final_decision(symbol, safe)
    return canonical


def blocked_entry_payload(current: dict, reason: str, snapshot: dict) -> dict:
    """Pure fail-closed projection shared by reads and persisted transitions."""
    safe = dict(current)
    missed = reason in {"ENTRY_PRICE_OUT_OF_RANGE", "CHASE_LIMIT_EXCEEDED",
                        "PRICE_DRIFT_REQUIRES_REEVALUATION", "NOTIFICATION_TOO_OLD"}
    safe.update({
        "finalAction": "WAIT" if missed else "NO_TRADE",
        "state": "WAIT_RETEST" if missed else "NO_TRADE", "canEnter": False,
        "primaryReason": "OVEREXTENDED" if missed else "SYSTEM_DECISION_CONFLICT",
        "riskGate": "ENTRY_MISSED" if missed else "INTERNAL_CONFLICT",
        "humanSummary": ("原本進場點已錯過，現在不要追；接下來等待回踩。"
                         if missed else "系統無法確認現在仍可進場，本次訊號已取消。"),
        "deliveryBlockReason": reason, "deliveryValidation": snapshot,
        "evaluatedAt": snapshot.get("validated_at"), "events": [],
        "decisionSignature": hashlib.sha256(
            f"{current.get('decisionSignature')}|DELIVERY_BLOCK|{reason}".encode()
        ).hexdigest()[:24],
    })
    return safe


def get_delivery_safe_current_decision(symbol: str = "XAUUSD") -> dict:
    """Current API/dashboard gate. It never returns a stale actionable ENTRY."""
    with db_session() as db:
        row = db.execute(select(CurrentFinalDecision).where(
            CurrentFinalDecision.symbol == symbol)).scalar_one_or_none()
        if row is None:
            return {}
        current = dict(row.payload or {})
        synthetic = {
            "decisionId": row.decision_id, "decisionVersion": row.decision_version,
            "scenarioVersion": row.scenario_version,
            "event_type": current.get("finalAction"),
        }
        result = validate_pre_delivery(
            db, symbol=symbol, queued_payload=synthetic,
            queued_at=_parse(current.get("decisionCreatedAt")) or row.updated_at)
    if result.allowed:
        return {**current, "deliveryValidation": result.snapshot}
    # GET/dashboard reads must stay side-effect free. The outbox persists the
    # audit and state transition when it actually blocks a delivery.
    return blocked_entry_payload(current, result.reason, result.snapshot)
