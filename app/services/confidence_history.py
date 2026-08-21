"""Idempotent confidence-grade migration for persisted analysis history."""

from __future__ import annotations

from sqlalchemy import or_, select

from app.db.models import AnalysisRun
from app.db.session import db_session
from app.engines.confidence import GRADING_VERSION, get_confidence_grade


def _legacy_score(row: AnalysisRun, payload: dict) -> tuple[int | None, str]:
    decision = payload.get("decision") or payload.get("market_decision") or {}
    for key in ("signal_score", "signalScore", "evidence_score", "evidenceScore"):
        value = decision.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if key.startswith("evidence") and value == 0 and row.confidence_grade in ("X", "U"):
                return None, "舊紀錄因資料不足使用占位 0，沒有有效訊號分數"
            return max(0, min(100, round(float(value)))), ""
    if row.signal_score is not None:
        return max(0, min(100, int(row.signal_score))), ""
    if row.evidence_score != 0 or row.confidence_grade not in ("X", "U"):
        return max(0, min(100, int(row.evidence_score))), ""
    return None, "歷史紀錄未保存有效訊號分數"


def _trade_fields(row: AnalysisRun, payload: dict) -> tuple[str, bool, str]:
    decision = payload.get("decision") or payload.get("market_decision") or {}
    if decision.get("trade_status"):
        return (
            str(decision["trade_status"]),
            bool(decision.get("can_enter")),
            str(decision.get("blocked_reason") or ""),
        )
    trace = payload.get("decision_trace") or {}
    blocks = list(trace.get("blockingReasons") or [])
    lifecycle = str(trace.get("lifecycleStatus") or "")
    if "RISK_REWARD_TOO_LOW" in blocks:
        return "BLOCKED_RR", False, "賺賠比未達門檻"
    if lifecycle == "MISSED_ENTRY_WAIT_RETEST":
        return "MISSED_ENTRY", False, "原進場區已錯過，等待回踩"
    if lifecycle in ("EXPIRED", "INVALIDATED"):
        return "INVALIDATED", False, "原劇本已失效"
    if row.decision_action in ("LONG", "SHORT"):
        return "READY", True, ""
    return "WAIT_CONFIRMATION", False, str(decision.get("reason") or "等待確認")


def backfill_confidence_history(*, limit: int = 10_000) -> int:
    """Upgrade old rows without changing their original numeric evidence score."""
    changed = 0
    with db_session() as db:
        rows = db.execute(
            select(AnalysisRun)
            .where(or_(AnalysisRun.grading_version.is_(None),
                       AnalysisRun.grading_version != GRADING_VERSION))
            .order_by(AnalysisRun.id)
            .limit(limit)
        ).scalars().all()
        for row in rows:
            payload = dict(row.result_json or {})
            score, missing_reason = _legacy_score(row, payload)
            trade_status, can_enter, blocked_reason = _trade_fields(row, payload)
            grade = get_confidence_grade(score)
            row.signal_score = score
            row.confidence_grade = grade
            row.grading_version = GRADING_VERSION
            row.trade_status = trade_status
            row.can_enter = can_enter
            row.blocked_reason = blocked_reason
            for key in ("decision", "market_decision"):
                decision = dict(payload.get(key) or {})
                if not decision and key == "market_decision":
                    continue
                decision.update({
                    "signal_score": score,
                    "confidence_grade": grade,
                    "grading_version": GRADING_VERSION,
                    "trade_status": trade_status,
                    "can_enter": can_enter,
                    "blocked_reason": blocked_reason,
                    "missing_score_reason": missing_reason,
                })
                payload[key] = decision
            row.result_json = payload
            changed += 1
    return changed
